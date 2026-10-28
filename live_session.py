"""Persistent (warm) Claude Code sessions for the Coordinator bot.

``bot.run_claude`` spawns a fresh ``claude -p`` per Telegram message: every turn
pays the CLI's full cold-start (~node boot + skill/agent discovery) and resumes
the conversation from disk via ``--resume``. This module is the *warm*
alternative: it keeps ONE long-lived ``claude -p --input-format stream-json``
process per conversation ``key`` and feeds successive user turns over its stdin,
so the second and later turns skip the cold start and reuse the model's already
loaded in-process context.

Protocol (verified against the Claude Code docs — cli-reference and
"Streaming Input"/streaming-vs-single-mode):
  * Start ``claude -p --input-format stream-json --output-format stream-json
    --verbose`` (``-p`` + ``--input-format stream-json`` = streaming-input print
    mode; ``--verbose`` is required for stream-json output). The process does NOT
    exit after one turn: it blocks reading the next user message from stdin.
  * ONE user turn = one JSON line written to stdin:
        {"type":"user","message":{"role":"user","content":[{"type":"text",
         "text":"..."}]},"parent_tool_use_id":null}\\n
  * Read stdout line-by-line; each line is one stream-json event. The turn is
    COMPLETE when an event with ``type=="result"`` arrives; the process then
    waits for the next stdin line. ``session_id`` appears on the ``init`` system
    event and stays stable for the life of the process.
  * stdin must stay OPEN between turns. Closing it (or killing the process) ends
    the session. An EOF on stdout mid-turn means the process died.

This is a best-effort fast path. ANY anomaly (spawn failure, dead process,
protocol violation, EOF mid-turn, idle/max timeout) raises
``LiveSessionUnavailable`` so the caller transparently falls back to the cold
``run_claude``. Persona / ``--resume`` apply ONLY at process start: a reused
process already holds its context, so they are passed on first spawn only.

Dependency-injected: bot.py wires in its own constants and stream helpers, so
this module imports nothing from bot.py (no circular import) and reads no
``os.environ`` itself.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field


class LiveSessionUnavailable(Exception):
    """Raised when the live path cannot serve this turn; caller must fall back
    to the cold ``run_claude``."""


@dataclass
class _Live:
    """One warm process plus its per-key serialization lock and bookkeeping."""

    proc: asyncio.subprocess.Process
    stderr_task: asyncio.Task
    session_id: str | None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    last_used: float = field(default_factory=time.monotonic)
    # (model, effort) this process was spawned with. --model/--effort bind at
    # spawn time, so a turn asking for a different pair cannot reuse this
    # process: run_turn respawns it, resuming its transcript so nothing is lost.
    overrides: tuple[str | None, str | None] = (None, None)


def _encode_turn(prompt: str) -> bytes:
    """Serialize one user turn to a single stream-json stdin line."""
    msg = {
        "type": "user",
        "message": {"role": "user", "content": [{"type": "text", "text": prompt}]},
        "parent_tool_use_id": None,
    }
    return (json.dumps(msg, ensure_ascii=False) + "\n").encode("utf-8")


class LiveSessionManager:
    """Registry of warm ``claude`` processes keyed by conversation ``key``.

    Concurrent ``run_turn`` calls for DIFFERENT keys run in parallel; calls for
    the SAME key are serialized by a per-key lock so two messages never
    interleave on one stdin/stdout. Config and stream helpers are injected.
    """

    def __init__(
        self,
        *,
        claude_bin: str,
        model: str | None,
        setting_sources: str | None,
        strict_mcp: bool,
        mcp_config: str | None = None,
        idle_ttl_sec: float,
        max_timeout_sec: float,
        idle_event_timeout_sec: float,
        stream_limit: int,
        max_live: int = 0,
        parse_stream_line=None,
        result_from_event=None,
        event_progress=None,
        usage_from_event=None,
        result_factory=None,
        gate=None,
        logger=None,
    ) -> None:
        self._bin = claude_bin
        self._model = model
        self._sources = setting_sources
        self._strict_mcp = strict_mcp
        self._mcp_config = mcp_config
        self._idle_ttl = idle_ttl_sec
        self._max_timeout = max_timeout_sec
        self._idle_event_timeout = idle_event_timeout_sec
        self._stream_limit = stream_limit
        self._max_live = max_live
        self._parse = parse_stream_line
        self._result_from_event = result_from_event
        self._event_progress = event_progress
        # Cost accounting (issue #28), injected like everything else so this
        # module keeps knowing nothing about claude_runner: `usage_from_event`
        # reads the run's spend out of the final event, `result_factory` decides
        # what a turn returns. Both default to today's behaviour — no
        # measurement, a plain ``(text, session_id)`` tuple — because a warm
        # session is also used by tests that never asked for a ledger.
        self._usage_from_event = usage_from_event
        self._result_factory = result_factory or (lambda text, sid, usage: (text, sid))
        self._gate = gate
        self._log = logger or logging.getLogger(__name__)
        self._live: dict[tuple, _Live] = {}
        # Guards the registry dict itself (cheap, never held across awaits on I/O).
        self._reg_lock = asyncio.Lock()

    # -- spawn --------------------------------------------------------------
    def _argv(
        self,
        persona: str | None,
        resume_id: str | None,
        model: str | None = None,
        effort: str | None = None,
    ) -> list[str]:
        argv = [
            self._bin,
            "-p",
            "--input-format",
            "stream-json",
            "--output-format",
            "stream-json",
            "--verbose",
            "--permission-mode",
            "bypassPermissions",
        ]
        eff_model = model or self._model
        if eff_model:
            argv += ["--model", eff_model]
        if effort:
            argv += ["--effort", effort]
        if self._sources:
            argv += ["--setting-sources", self._sources]
        if self._mcp_config:
            # Single remote suite-mcp + strict: load ONLY this server, ignoring the
            # per-node stdio .mcp.json that can't run in-container.
            argv += ["--mcp-config", self._mcp_config, "--strict-mcp-config"]
        elif self._strict_mcp:
            argv.append("--strict-mcp-config")
        # persona / resume bind the in-process context: first spawn ONLY.
        if persona:
            argv += ["--append-system-prompt", persona]
        if resume_id:
            argv += ["--resume", resume_id]
        return argv

    async def _spawn(
        self,
        key: tuple,
        *,
        workspace: str,
        persona: str | None,
        resume_id: str | None,
        model: str | None = None,
        effort: str | None = None,
    ) -> _Live:
        argv = self._argv(persona, resume_id, model, effort)
        self._log.info(
            "live spawn: cwd=%s model=%s effort=%s resume=%s",
            workspace,
            model or self._model or "<default>",
            effort or "<default>",
            resume_id or "<new>",
        )
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                cwd=workspace,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                limit=self._stream_limit,
            )
        except BaseException as exc:  # spawn failed -> not a live path
            raise LiveSessionUnavailable(f"spawn failed: {exc!r}") from exc
        # Drain stderr concurrently so a chatty process can't deadlock on a full pipe.
        stderr_task = asyncio.create_task(proc.stderr.read())
        return _Live(
            proc=proc,
            stderr_task=stderr_task,
            session_id=resume_id,
            overrides=(model, effort),
        )

    # -- teardown -----------------------------------------------------------
    async def _kill(self, live: _Live) -> None:
        try:
            if live.proc.stdin and not live.proc.stdin.is_closing():
                live.proc.stdin.close()
        except Exception:
            pass
        try:
            live.proc.kill()
        except ProcessLookupError:
            pass
        except Exception:
            pass
        try:
            await live.proc.wait()
        except Exception:
            pass
        live.stderr_task.cancel()

    async def _drop(self, key: tuple, live: _Live) -> None:
        """Remove a key from the registry (if still the same object) and kill it."""
        async with self._reg_lock:
            if self._live.get(key) is live:
                del self._live[key]
        await self._kill(live)

    def _evict_lru_locked(self) -> None:
        """Caller holds ``_reg_lock``. Retire least-recently-used IDLE sessions
        until there is room for one more under ``max_live`` (0 = unlimited). Each
        warm process is a ~150 MB node; without this cap a busy bot would keep one
        alive per conversation until the TTL sweep and could OOM a small host.
        Mid-turn (locked) sessions are never evicted — if every session is busy we
        briefly allow going over the cap rather than kill in-flight work."""
        if self._max_live <= 0:
            return
        while len(self._live) >= self._max_live:
            idle = [(k, v) for k, v in self._live.items() if not v.lock.locked()]
            if not idle:
                break
            k, v = min(idle, key=lambda kv: kv[1].last_used)
            del self._live[k]
            asyncio.create_task(self._kill(v))
            self._log.info("live: LRU-evicted idle session (cap=%d)", self._max_live)

    # -- one turn -----------------------------------------------------------
    async def run_turn(
        self,
        key: tuple,
        *,
        prompt: str,
        workspace: str,
        persona: str | None,
        resume_id: str | None,
        on_progress=None,
        model: str | None = None,
        effort: str | None = None,
    ) -> tuple[str, str | None]:
        """Run one user turn on the warm process for ``key`` (spawning it on the
        first turn), returning ``(reply_text, session_id)``. Raises
        ``LiveSessionUnavailable`` on any failure so the caller falls back to the
        cold ``run_claude``.

        A turn that asks for a different ``(model, effort)`` than the warm
        process was spawned with retires that process and spawns a replacement
        resuming the same transcript — the flags only bind at spawn time, so
        reusing the warm one would silently ignore the user's choice."""
        # Get-or-create the _Live, then take its per-key lock so same-key turns
        # serialize. Spawn happens under the registry lock so two first-turns for
        # one key don't race into two processes.
        want = (model, effort)
        async with self._reg_lock:
            live = self._live.get(key)
            stale_overrides = live is not None and live.overrides != want
            if live is None or live.proc.returncode is not None or stale_overrides:
                if live is not None:  # stale/dead/re-configured entry: replace it
                    if stale_overrides and live.session_id:
                        # keep the conversation: resume what the old process built
                        resume_id = live.session_id
                        self._log.info(
                            "live: overrides %s -> %s, respawning on session %s",
                            live.overrides,
                            want,
                            live.session_id,
                        )
                    self._live.pop(key, None)
                    asyncio.create_task(self._kill(live))
                self._evict_lru_locked()  # bound warm-process RAM (LRU)
                live = await self._spawn(
                    key,
                    workspace=workspace,
                    persona=persona,
                    resume_id=resume_id,
                    model=model,
                    effort=effort,
                )
                self._live[key] = live

        async with live.lock:
            # Re-check liveness now that we hold the per-key lock.
            if live.proc.returncode is not None or self._live.get(key) is not live:
                await self._drop(key, live)
                raise LiveSessionUnavailable("process died before turn")

            acquired = False
            try:
                if self._gate is not None:
                    await self._gate.acquire()
                    acquired = True
                reply, sid, usage = await self._pump(live, prompt, on_progress)
                live.last_used = time.monotonic()
                if sid:
                    live.session_id = sid
                return self._result_factory(reply, live.session_id, usage)
            except LiveSessionUnavailable:
                await self._drop(key, live)
                raise
            except BaseException as exc:  # any anomaly -> kill + fall back
                await self._drop(key, live)
                raise LiveSessionUnavailable(f"turn failed: {exc!r}") from exc
            finally:
                if acquired and self._gate is not None:
                    self._gate.release()

    async def _pump(
        self, live: _Live, prompt: str, on_progress
    ) -> tuple[str, str | None, object | None]:
        """Write the turn, stream events until ``result``, return (text, sid, usage).

        ``usage`` is ``None`` unless a ``usage_from_event`` reader was injected.
        A turn that never reaches its ``result`` event raises instead of
        returning, so an interrupted live turn is measured by the cold fallback
        that follows it, not counted twice here."""
        proc = live.proc
        if proc.stdin is None or proc.stdout is None:
            raise LiveSessionUnavailable("missing pipes")
        proc.stdin.write(_encode_turn(prompt))
        await proc.stdin.drain()

        session_id = live.session_id
        deadline = time.monotonic() + self._max_timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise LiveSessionUnavailable(f"max timeout {self._max_timeout}s")
            try:
                raw = await asyncio.wait_for(
                    proc.stdout.readline(),
                    timeout=min(self._idle_event_timeout, remaining),
                )
            except asyncio.TimeoutError as exc:
                raise LiveSessionUnavailable(
                    f"idle {self._idle_event_timeout}s, no event"
                ) from exc
            except ValueError:
                # Line exceeded stream_limit: skip it, keep streaming this turn.
                self._log.warning("live stream line over %d bytes, skipped", self._stream_limit)
                continue
            if not raw:  # EOF mid-turn -> the process died
                raise LiveSessionUnavailable("EOF: process died mid-turn")
            ev = self._parse(raw.decode(errors="replace"))
            if ev is None:
                continue
            sid = ev.get("session_id")
            if sid:
                session_id = sid
            if ev.get("type") == "result":
                usage = (
                    self._usage_from_event(ev)
                    if self._usage_from_event is not None
                    else None
                )
                return self._result_from_event(ev), session_id, usage
            if on_progress is not None:
                label = self._event_progress(ev)
                if label:
                    try:
                        await on_progress(label)
                    except Exception:
                        pass

    # -- lifecycle ----------------------------------------------------------
    async def evict_idle(self) -> int:
        """Close processes idle longer than ``idle_ttl_sec``. Returns count closed."""
        now = time.monotonic()
        async with self._reg_lock:
            stale = [
                (k, v) for k, v in self._live.items() if now - v.last_used > self._idle_ttl
            ]
            for k, _ in stale:
                del self._live[k]
        for _, v in stale:
            await self._kill(v)
        return len(stale)

    async def close(self, key=None) -> None:
        """Close one key's process, or all if ``key`` is None. Idempotent."""
        async with self._reg_lock:
            if key is None:
                victims = list(self._live.values())
                self._live.clear()
            else:
                v = self._live.pop(key, None)
                victims = [v] if v is not None else []
        for v in victims:
            await self._kill(v)
