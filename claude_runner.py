"""claude -p spawner, extracted from bot.py so headless workers can reuse it.

Telegram-independent: depends only on stdlib + env-driven CLAUDE_* constants and
the shared concurrency gate. bot.py, hub.py (via DI), scheduler.py and providers.py
all run claude through this module's run_claude.
"""

from __future__ import annotations

import asyncio
import functools
import contextvars
import json
import logging
import os
import re
import time
from datetime import UTC, datetime

from llm_usage import (
    OUTCOME_ERROR,
    OUTCOME_OK,
    OUTCOME_TIMEOUT,
    RunUsage,
)

logger = logging.getLogger("coordinator.claude")

CLAUDE_BIN = os.environ.get("CLAUDE_BIN", "claude")
# --- Lean per-message boot (latency) ----------------------------------------
# Each Telegram message spawns a fresh ``claude -p``, so every message pays the
# CLI's full cold-start. These trim that cost without changing what the bot can
# do: project skills/agents under the workspace's .claude still load, and OAuth
# is unaffected (credentials are not a setting source, so excluding "user" here
# keeps subscription auth working — verified empirically).
#   CLAUDE_MODEL           faster default model for chat turns (vs the global Opus default)
#   CLAUDE_SETTING_SOURCES sources to load; omitting "user" skips the global hooks
#                          (git pull --rebase, gsd, ...) and plugins (superpowers, ...)
#                          that otherwise run on every single message
#   CLAUDE_STRICT_MCP      load zero MCP servers (the bot's turns need none)
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "sonnet").strip()
CLAUDE_SETTING_SOURCES = os.environ.get(
    "CLAUDE_SETTING_SOURCES", "project,local"
).strip()
CLAUDE_STRICT_MCP = os.environ.get("CLAUDE_STRICT_MCP", "1").strip().lower() not in (
    "0",
    "false",
    "no",
    "",
)
# Remote suite-mcp (db/brain/graph) exposed over HTTP on the shared docker
# network (see $SRV_ROOT/Life/tooling/suite-mcp). When SUITE_MCP_URL is set we hand
# claude a one-server --mcp-config pointing at it AND --strict-mcp-config so ONLY
# that server loads — the per-node .mcp.json defines suite-mcp as a stdio command
# that can't run in this container, so strict avoids its "command undefined"
# error. Empty -> fall back to the plain CLAUDE_STRICT_MCP behaviour.
SUITE_MCP_URL = os.environ.get("SUITE_MCP_URL", "").strip()
SUITE_MCP_CONFIG = (
    json.dumps({"mcpServers": {"suite-mcp": {"type": "http", "url": SUITE_MCP_URL}}})
    if SUITE_MCP_URL
    else None
)
CLAUDE_TIMEOUT = int(os.environ.get("CLAUDE_TIMEOUT_SEC", "300"))
# Idle (inactivity) watchdog: kill a session only if NO stream event arrives for
# this long. It resets on every event, so a long-but-healthy task (Playwright,
# web research) keeps running while a *genuinely hung* tool still dies. Defaults
# to the old total-timeout value.
CLAUDE_IDLE_TIMEOUT = int(
    os.environ.get("CLAUDE_IDLE_TIMEOUT_SEC", str(CLAUDE_TIMEOUT))
)
# Longer silence window while a tool call is IN FLIGHT (a tool_use without its
# tool_result yet): stream-json says nothing while a build/test/download runs,
# so the plain idle window would kill a healthy long tool at 300s (ComUnity#189).
# Still bounded: a tool that never returns dies here, and CLAUDE_MAX_TIMEOUT
# caps the whole run anyway. Never below the plain idle window.
CLAUDE_TOOL_IDLE_TIMEOUT = int(
    os.environ.get("CLAUDE_TOOL_IDLE_TIMEOUT_SEC", "1200")
)
# Absolute backstop on total wall-clock, regardless of activity.
CLAUDE_MAX_TIMEOUT = int(os.environ.get("CLAUDE_MAX_TIMEOUT_SEC", "3600"))
# Watchdog on the FIRST token of a RESUMED run, much shorter than the idle one.
#
# A resumed session that is going to work says something almost immediately; one
# that is dead (born in another project, blocked on a permission nobody can
# hear) says nothing at all, and the full idle timeout then burns 300s of pure
# silence before the retry-with-a-fresh-session even begins. Measured on
# 2026-09-06: a Telegram turn took 21 minutes, 5 of which were exactly this.
# It only applies until the first token: after that the run is alive and the
# normal idle watchdog takes over, so a long healthy task is never cut short.
CLAUDE_FIRST_TOKEN_TIMEOUT = int(
    os.environ.get("CLAUDE_FIRST_TOKEN_TIMEOUT_SEC", "45")
)
# Max bytes for a single stream-json line (a big tool_result can be large).
CLAUDE_STREAM_LIMIT = int(
    os.environ.get("CLAUDE_STREAM_LIMIT_BYTES", str(8 * 1024 * 1024))
)
# Max simultaneous `claude -p` spawns. Each spawn is a fresh ``node`` process
# (~150 MB resident); without a bound, a burst of Telegram messages (or several
# notifications/cron jobs firing together) fans out into N of them and can OOM a
# small host. Extra messages await a free slot instead. Default 3; on the mini PC
# set CLAUDE_MAX_CONCURRENCY=2. The inference itself is remote (subscription), so
# this caps local RAM/CPU, not throughput of a single message.
CLAUDE_MAX_CONCURRENCY = max(1, int(os.environ.get("CLAUDE_MAX_CONCURRENCY", "3")))

# --- Backend switch (global for now; per-project/agent/session is a follow-up) ---
# AI_BACKEND picks which CLI every bot on this process spawns. Default "claude"
# keeps today's behaviour byte-for-byte; "opencode" routes through OpenCode
# (verified against DeepSeek V4 Flash on 2026-08-09 — schema below is empirical,
# not from opencode's docs, which don't specify it).
AI_BACKEND = os.environ.get("AI_BACKEND", "claude").strip().lower()
OPENCODE_BIN = os.environ.get("OPENCODE_BIN", "opencode")
OPENCODE_MODEL = os.environ.get("OPENCODE_MODEL", "deepseek/deepseek-v4-flash").strip()

# --- Host execution (issue #46) ----------------------------------------------
# A bus-woken session must be the session a terminal would open on this device:
# same user/HOME, the REAL repo path (Claude Code keys project memory by cwd),
# the user's gh/ssh credentials, the device's default model and settings. None
# of that holds inside this container, so when HOST_EXEC_SSH is set the run is
# delegated to the host over ssh, where scripts/host_claude_exec.py (a FORCED
# command on that key) starts claude as the host user in the host path.
#   HOST_EXEC_SSH          user@host of the host sshd; empty = feature off
#   HOST_EXEC_KEY          private key whose authorized_keys line forces the wrapper
#   HOST_EXEC_KNOWN_HOSTS  writable known_hosts (the mounted ~/.ssh is root-owned)
HOST_EXEC_SSH = os.environ.get("HOST_EXEC_SSH", "").strip()
HOST_EXEC_KEY = os.environ.get("HOST_EXEC_KEY", "/home/app/.ssh/host_exec_key").strip()
HOST_EXEC_KNOWN_HOSTS = os.environ.get(
    "HOST_EXEC_KNOWN_HOSTS", "/app/data/host_exec_known_hosts"
).strip()
# Only bus coordinates cross into the host run: the wrapper refuses anything else.
HOST_EXEC_ENV_PREFIX = "HUB_"


def host_exec_enabled() -> bool:
    """Is delegation of bus wakes to the host configured on this process?"""
    return bool(HOST_EXEC_SSH)


# --- Transport failures ------------------------------------------------------
# A run that died before the model ever answered: spawn failure, non-zero exit,
# CLI-level error, or a stream that closed without the backend's final event.
# Callers that judge "did the agent answer?" (the hub wake loop) must NOT count
# one of these as a non-answer: it is an infrastructure fault and deserves a
# retry with backoff, not an escalation that gags the conversation (issue #29).
# Every runner failure path already prefixes its text with this marker; a normal
# reply never starts with it.
TRANSPORT_ERROR_PREFIX = "❌"


def is_transport_error(text: str) -> bool:
    """True when `text` is a runner-level failure rather than an agent reply."""
    return text.lstrip().startswith(TRANSPORT_ERROR_PREFIX)


# A rejected resume comes in two kinds that need opposite handling. When the
# session itself is gone (wiped store, expired id, a session born in another
# project) only a fresh one can answer. When the ENGINE hiccups (a 5xx, a rate
# limit, a timeout) the session is fine: starting fresh would throw away the
# whole conversation for a blip — which is what happened on every "Unexpected
# server error" until 2026-10-09.
_SESSION_GONE_MARKERS = (
    "session not found",
    " muta per ",  # our own "sessione <id> muta per Ns" (see _run_opencode)
    "no conversation found",
    "invalid session",
    "unknown session",
    "session does not exist",
    "notfounderror",
)
_TRANSIENT_MARKERS = (
    "unexpected server error",
    "server error",
    "internal error",
    "service unavailable",
    "bad gateway",
    "gateway timeout",
    "overloaded",
    "rate limit",
    "rate_limit",
    "too many requests",
    "timed out",
    "timeout",
    "econnreset",
    "connection reset",
    "connection refused",
    "socket hang up",
)
# A status code only counts in an HTTP/status context ("HTTP 503", "status:
# 429", "error 502", "503 Service ..."): a bare number anywhere in stderr
# (a line number, a size, a pid) must not make an error look transient.
_HTTP_5XX_OR_429 = re.compile(
    r"(?:\bhttp(?:/\d(?:\.\d)?)?|\bstatus(?:\s*code)?|\bcode|\berror)\s*[:=]?\s*(?:5\d\d|429)\b"
    r"|\b(?:5\d\d|429)\s+(?:internal|bad gateway|service|gateway|too many)"
)

# Seconds before re-running a resume that failed on a transient engine error.
RESUME_TRANSIENT_BACKOFF_SEC = float(os.environ.get("RESUME_TRANSIENT_BACKOFF_SEC", "3"))


def classify_resume_failure(text: str) -> str:
    """``"session"`` | ``"transient"`` | ``"other"`` for a runner failure text.

    ``session`` wins over ``transient``: "session not found (404)" is about the
    session, whatever the transport said. Anything unrecognised is ``other``,
    which keeps the historical behaviour (fresh session).
    """
    low = (text or "").lower()
    if any(m in low for m in _SESSION_GONE_MARKERS):
        return "session"
    if any(m in low for m in _TRANSIENT_MARKERS) or _HTTP_5XX_OR_429.search(low):
        return "transient"
    return "other"


# Come si presenta una CLI non autenticata. La riga esatta e' `Not logged in ·
# Please run /login`; si cercano due frammenti indipendenti perche' il
# separatore e la punteggiatura sono cambiati fra le versioni, e perche' una
# sola stringa troppo specifica e' come non controllare affatto.
_IMPRONTE_NON_AUTENTICATO = ("not logged in", "please run /login", "run `claude` login")


def is_auth_error(text: str) -> bool:
    """True quando il fallimento e' "la CLI non e' autenticata".

    Distinto da un guasto di trasporto, e il motivo non e' tassonomico: sono
    opposti in tutto cio' che conta. Un trasporto che cade si riprende da se',
    quindi si ritenta; un token scaduto non si ripara ritentando, e nemmeno
    aspettando. E soprattutto non riguarda *questa* conversazione ma ogni
    agente di questo processo — dirlo conversazione per conversazione e'
    ripetere mille volte una notizia sola, nell'unico posto che nessuno
    rilegge (issue #34: tre mesi di silenzio, poi tre escalation in due
    minuti).
    """
    if not is_transport_error(text):
        return False
    minuscolo = text.lower()
    return any(impronta in minuscolo for impronta in _IMPRONTE_NON_AUTENTICATO)


# --- Result carrier ----------------------------------------------------------
class RunResult(tuple):
    """``(text, session_id)`` with the run's measured cost riding along.

    A tuple subclass, not a third element: ``text, sid = await run_claude(...)``
    is the contract of ~15 call sites plus every injected fake in the test
    suite, and widening it to a 3-tuple would break all of them at once for the
    benefit of the three that care about the money. Callers who do care read
    ``result.usage`` (or ``llm_usage.usage_of(result)``, which also tolerates
    the plain tuples the fakes return).
    """

    def __new__(cls, text: str, session_id: str | None, usage: RunUsage | None = None):
        obj = super().__new__(cls, (text, session_id))
        obj.usage = usage
        return obj

    @property
    def text(self) -> str:
        return self[0]

    @property
    def session_id(self) -> str | None:
        return self[1]


def _elapsed_ms(t0: float) -> int:
    return int((time.monotonic() - t0) * 1000)


def usage_from_result_event(
    ev: dict, *, model: str, started_at: datetime, fallback_duration_ms: int | None = None
) -> RunUsage:
    """Read claude's ``result`` event as a ledger row.

    The model is taken from ``modelUsage`` when present: that key is the model
    that actually served the turn, whereas ``--model sonnet`` is only what was
    asked for. Observed beats declared — it is the whole point of the ledger's
    ``/api/llm/models`` view.
    """
    usage = ev.get("usage") or {}
    model_usage = ev.get("modelUsage") or {}
    served = next(iter(model_usage), None) if isinstance(model_usage, dict) else None
    failed = bool(ev.get("is_error")) or ev.get("subtype") not in (None, "success")
    cost = ev.get("total_cost_usd")
    return RunUsage(
        engine="claude",
        model=served or model,
        outcome=OUTCOME_ERROR if failed else OUTCOME_OK,
        started_at=started_at,
        duration_ms=ev.get("duration_ms") or fallback_duration_ms,
        input_tokens=usage.get("input_tokens"),
        output_tokens=usage.get("output_tokens"),
        cache_read_tokens=usage.get("cache_read_input_tokens"),
        cache_write_tokens=usage.get("cache_creation_input_tokens"),
        cost_usd=float(cost) if isinstance(cost, (int, float)) else None,
    )


class _OpencodeMeter:
    """Running total over an opencode stream's ``step_finish`` events.

    opencode reports per step, not per run: a run with three tool steps emits
    three costs, and only their sum is what the run cost. Absent any step (a run
    cut short) the totals stay ``None`` rather than 0 — nothing was measured.
    """

    def __init__(self) -> None:
        self.steps = 0
        self.cost = 0.0
        self.input = self.output = self.reasoning = 0
        self.cache_read = self.cache_write = 0

    def add(self, ev: dict) -> None:
        # opencode's schema is empirical here (see the module note above): the
        # numbers have been seen both at the top level and nested under "part",
        # depending on version, so read whichever carries them rather than
        # betting the whole ledger on one shape.
        src = ev.get("part") if isinstance(ev.get("part"), dict) else {}
        if "tokens" not in src and "cost" not in src:
            src = ev
        self.steps += 1
        cost = src.get("cost")
        if isinstance(cost, (int, float)):
            self.cost += float(cost)
        tokens = src.get("tokens") or {}
        cache = tokens.get("cache") or {}
        self.input += int(tokens.get("input") or 0)
        self.output += int(tokens.get("output") or 0)
        self.reasoning += int(tokens.get("reasoning") or 0)
        self.cache_read += int(cache.get("read") or 0)
        self.cache_write += int(cache.get("write") or 0)

    def usage(
        self, *, model: str, outcome: str, started_at: datetime, duration_ms: int
    ) -> RunUsage:
        measured = self.steps > 0
        return RunUsage(
            engine="opencode",
            model=model,
            outcome=outcome,
            started_at=started_at,
            duration_ms=duration_ms,
            input_tokens=self.input if measured else None,
            output_tokens=self.output if measured else None,
            reasoning_tokens=self.reasoning if measured else None,
            cache_read_tokens=self.cache_read if measured else None,
            cache_write_tokens=self.cache_write if measured else None,
            # A run that died mid-flight spent real money that cannot be
            # totalled: the partial sum would understate it, and 0 would invert
            # the ranking outright. Only a finished run reports a cost.
            cost_usd=self.cost if (measured and outcome == OUTCOME_OK) else None,
        )


def _interrupted_usage(
    *, engine: str, model: str, started_at: datetime, duration_ms: int, outcome: str
) -> RunUsage:
    """A run killed or failed before its final event: spent, unknowable."""
    return RunUsage(
        engine=engine,
        model=model,
        outcome=outcome,
        started_at=started_at,
        duration_ms=duration_ms,
        cost_usd=None,
    )


def _parse_stream_line(raw: str) -> dict | None:
    """Parse one ``--output-format stream-json`` line into an event dict.

    Each line is a standalone JSON object; blank lines and partial flushes are
    tolerated by returning ``None``.
    """
    raw = raw.strip()
    if not raw:
        return None
    try:
        ev = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return ev if isinstance(ev, dict) else None


def _event_progress(ev: dict) -> str | None:
    """Short human label for a stream event (the Telegram "typing" note), or
    ``None`` when the event isn't worth surfacing."""
    t = ev.get("type")
    if t == "assistant":
        for b in (ev.get("message") or {}).get("content") or []:
            if isinstance(b, dict) and b.get("type") == "tool_use":
                return f"🔧 {b.get('name', 'tool')}"
        return "✍️ scrivo la risposta…"
    if t == "user":  # a tool_result is coming back into the model
        return "📥 elaboro un risultato…"
    return None


def _track_tool_calls(ev: dict, pending: set[str]) -> None:
    """Keep ``pending`` = ids of tool_use blocks not yet answered by a tool_result."""
    content = (ev.get("message") or {}).get("content")
    if not isinstance(content, list):
        return
    for b in content:
        if not isinstance(b, dict):
            continue
        if ev.get("type") == "assistant" and b.get("type") == "tool_use" and b.get("id"):
            pending.add(b["id"])
        elif ev.get("type") == "user" and b.get("type") == "tool_result":
            pending.discard(b.get("tool_use_id"))


def _idle_window(pending: set[str]) -> float:
    """Silence allowed before the watchdog fires: longer while a tool runs."""
    return max(CLAUDE_IDLE_TIMEOUT, CLAUDE_TOOL_IDLE_TIMEOUT) if pending else CLAUDE_IDLE_TIMEOUT


def _result_from_event(ev: dict) -> str:
    """Final reply text from a stream-json ``result`` event (error-aware)."""
    if ev.get("is_error") or ev.get("subtype") not in (None, "success"):
        sub = ev.get("subtype") or "error"
        return (ev.get("result") or "").strip() or f"❌ claude: {sub}"
    return (ev.get("result") or "").strip() or "(risposta vuota)"


# --- Claude invocation -------------------------------------------------------
_claude_sem: asyncio.Semaphore | None = None


def _claude_gate() -> asyncio.Semaphore:
    """Concurrency gate for ``claude -p`` spawns, created lazily in the loop.

    Bounds simultaneous heavy subprocesses to ``CLAUDE_MAX_CONCURRENCY`` so a
    burst can't fan out into one ~150 MB ``node`` process per message and OOM a
    small host. Built on first use (inside the running loop) and reused.
    """
    global _claude_sem
    if _claude_sem is None:
        _claude_sem = asyncio.Semaphore(CLAUDE_MAX_CONCURRENCY)
    return _claude_sem


TRANSIENT_HINT = (
    "\n\nErrore del motore, non della richiesta: nessuna azione è stata "
    "completata. Riprova tra qualche minuto; se si ripete, rimandare lo stesso "
    "messaggio non aiuta: serve guardare i log del server opencode."
)


def explain_transient(result):
    """Append a plain-Italian hint to a final, still-failing transient error.

    The raw ``Unexpected server error`` told the user nothing and invited the
    same retry three times in a row (ComUnity#190). The prefix is untouched, so
    ``is_transport_error`` and the session id keep working.
    """
    text = result[0]
    if (
        is_transport_error(text)
        and classify_resume_failure(text) == "transient"
        and TRANSIENT_HINT not in text
    ):
        return RunResult(text + TRANSIENT_HINT, result[1], getattr(result, "usage", None))
    return result


def _explaining(fn):
    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        return explain_transient(await fn(*args, **kwargs))

    return wrapper


@_explaining
async def run_claude(
    prompt: str,
    *,
    workspace: str,
    persona: str | None,
    resume_id: str | None = None,
    on_progress=None,
    extra_env: dict | None = None,
    model: str | None = None,
    effort: str | None = None,
    backend: str | None = None,
    host_cwd: str | None = None,
    max_timeout: int | None = None,
    on_session=None,
) -> RunResult:
    """Dispatch to a backend (``claude`` or ``opencode``).

    ``AI_BACKEND`` is the process-wide default (every bot/wake shares it
    unless told otherwise); ``backend`` is a PER-CALL override so a single
    (chat, topic) card can point at a different CLI than the rest of the
    deployment -- the /modello catalog (Step 2026-09-01) uses this to let a
    card pick "Claude (abbonamento)" while every other card stays on
    opencode/DeepSeek, without a second Coordinator process. Callers never
    need to know which backend actually ran: both sides of the dispatch
    return the same ``(text, session_id)`` contract.

    A ``resume_id`` the backend rejects (the session expired, or its store was
    wiped) degrades to a fresh session **within this same call**: the CLI dies in
    a couple of seconds without ever reaching a model, so counting that as an
    attempt burns half the caller's retry budget on a transport fault (#29).

    The returned :class:`RunResult` still unpacks as ``(text, session_id)``; its
    ``usage`` is that of the run whose text is being returned. When a rejected
    resume forces a second run, the first one's measurement is dropped rather
    than summed: it died before reaching a model, so its cost is 0 in all but
    name and attributing a second row to it would inflate the run count.

    ``host_cwd`` (claude backend only, and only when :func:`host_exec_enabled`)
    runs the session ON THE HOST in that real path instead of in ``workspace``
    inside the container — issue #46. Ignored otherwise.

    ``max_timeout`` (seconds) replaces ``CLAUDE_MAX_TIMEOUT`` for THIS call only:
    a bus wake can ask for a longer budget (``context.max_minutes``, #70).

    ``on_session(session_id)`` is called (sync) as soon as the stream names the
    session — its first event, not the end of the run — so a caller can expose
    a session that is still working (TicketManager#10).
    """
    # opencode has no closed tool list: a read-only session is claude or nothing.
    use_opencode = (backend or AI_BACKEND) == "opencode" and not closed_session()
    backend_fn = _run_opencode if use_opencode else _run_claude_cli
    host_kw = (
        {"host_cwd": host_cwd}
        if host_cwd and not use_opencode and host_exec_enabled() and not closed_session()
        else {}
    )
    result = await backend_fn(
        prompt,
        workspace=workspace,
        persona=persona,
        resume_id=resume_id,
        on_progress=on_progress,
        on_session=on_session,
        extra_env=extra_env,
        model=model,
        effort=effort,
        max_timeout=max_timeout,
        **host_kw,
    )
    if resume_id and is_transport_error(result[0]):
        if classify_resume_failure(result[0]) == "transient":
            # The engine stumbled, the session is fine: one more try on the
            # SAME session after a short pause. If it fails again the error is
            # returned with the session still attached, so the next message
            # resumes the conversation instead of starting from zero.
            logger.warning(
                "runner: resume %s hit a transient error (%s) — retrying the same "
                "session in %.0fs",
                resume_id,
                result[0].replace("\n", " ")[:160],
                RESUME_TRANSIENT_BACKOFF_SEC,
            )
            await asyncio.sleep(RESUME_TRANSIENT_BACKOFF_SEC)
            result = await backend_fn(
                prompt,
                workspace=workspace,
                persona=persona,
                resume_id=resume_id,
                on_progress=on_progress,
                on_session=on_session,
                extra_env=extra_env,
                model=model,
                effort=effort,
                max_timeout=max_timeout,
                **host_kw,
            )
            if not is_transport_error(result[0]):
                return result
            # Still failing on the same session: whatever the cause, a session
            # that fails twice in a row may be the problem itself. One fresh
            # run, never a loop on a broken session.
            logger.warning(
                "runner: resume %s failed again (%s) — falling back to a fresh session",
                resume_id,
                result[0].replace("\n", " ")[:160],
            )
            return await backend_fn(
                prompt,
                workspace=workspace,
                persona=persona,
                resume_id=None,
                on_progress=on_progress,
                on_session=on_session,
                extra_env=extra_env,
                model=model,
                effort=effort,
                max_timeout=max_timeout,
                **host_kw,
            )
        logger.warning(
            "runner: resume %s rejected (%s) — retrying with a fresh session",
            resume_id,
            result[0].replace("\n", " ")[:160],
        )
        result = await backend_fn(
            prompt,
            workspace=workspace,
            persona=persona,
            resume_id=None,
            on_progress=on_progress,
            on_session=on_session,
            extra_env=extra_env,
            model=model,
            effort=effort,
            max_timeout=max_timeout,
            **host_kw,
        )
    return result


# Remote command sent with every host-exec ssh. With the forced command in place
# (authorized_keys ``command=``) sshd ignores it. Without it, sshd would start a
# login shell that reads our stdin (the JSON request: prompt, system prompt,
# backticks) AS A SCRIPT: pieces of the text run as commands (ComUnity#199).
# This fixed string never reads stdin: it fails closed, with a clear message.
HOST_EXEC_FAIL_CLOSED = (
    "echo 'host-exec: forced command (authorized_keys command=) missing; "
    "request refused' >&2; exit 97"
)


def host_exec_command() -> list[str]:
    """The ssh invocation that reaches the host wrapper (forced command)."""
    return [
        "ssh",
        "-T",
        "-i",
        HOST_EXEC_KEY,
        "-o",
        "BatchMode=yes",
        "-o",
        "IdentitiesOnly=yes",
        "-o",
        "StrictHostKeyChecking=accept-new",
        "-o",
        f"UserKnownHostsFile={HOST_EXEC_KNOWN_HOSTS}",
        "-o",
        "ServerAliveInterval=30",
        HOST_EXEC_SSH,
        HOST_EXEC_FAIL_CLOSED,
    ]


def host_exec_request(
    args: list[str], *, host_cwd: str, extra_env: dict | None
) -> bytes:
    """The single JSON line the wrapper reads before starting claude."""
    env = {
        k: v
        for k, v in (extra_env or {}).items()
        if str(k).startswith(HOST_EXEC_ENV_PREFIX) and v is not None
    }
    req = {"cwd": host_cwd, "args": args, "env": env}
    profile = WAKE_PROFILE.get()
    if profile:
        # A profile NAME, never a path: the wrapper resolves it against the
        # instance's own llm-routing.json on the host (#91).
        req["profile"] = profile
    return (json.dumps(req) + "\n").encode()


# Subscription profile of a bus wake (#91): ``{"instance": <host path>, "name":
# <profilo>}`` from the instance's LLM queue. Set by the wake around its run, read
# by host_exec_request, like SESSION_TOOLS: no flag threaded through every call.
WAKE_PROFILE: contextvars.ContextVar[dict | None] = contextvars.ContextVar(
    "wake_profile", default=None
)


# Read-only session (#89): a sender who is neither admin nor owner never gets an
# agent with tools. The bot sets this around the turn; the runner reads it, so
# every dispatch path (cold, resume retries, fresh fallback) stays read-only
# without threading a flag through each call.
SESSION_READONLY: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "session_readonly", default=False
)

# Closed tool list of an ``agent`` node (Life#157 §3.4): the manifest's allowlist,
# never bypassPermissions. Set by whoever runs a turn in such a node's folder; like
# SESSION_READONLY it keeps the run in the container, cold, on claude.
SESSION_TOOLS: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "session_tools", default=None
)


def closed_session() -> bool:
    """The run has a closed tool list: read-only (#89) or an agent node (Life#157)."""
    return SESSION_READONLY.get() or bool(SESSION_TOOLS.get())


# Built-in tools a read-only session keeps. No Bash, Edit, Write, Agent, Skill,
# WebFetch, MCP (strict config with no servers).
READONLY_TOOLS = "Read,Grep,Glob"
# The container holds credentials a Read tool could still open: deny them by path
# ("//" = absolute). Best-effort on Grep/Glob, so the tool list stays the real fence.
READONLY_DENY = (
    "Read(//m365/**)",
    "Read(//secrets/**)",
    "Read(//proc/**)",
    "Read(${LIFE_HOME}/**)",
    "Read(//home/app/.ssh/**)",
    "Read(//home/app/.gitauth/**)",
    "Read(//home/app/.config/gh/**)",
    "Read(//home/app/.claude/.credentials.json)",
    "Read(//home/app/.claude.json)",
    "Read(//app/.env*)",
    "Read(**/.env*)",
    "Read(**/*.token)",
    "Read(**/*.key)",
    "Read(**/secrets/**)",
)


def claude_args(
    prompt: str,
    *,
    persona: str | None,
    resume_id: str | None,
    model: str | None,
    effort: str | None,
    terminal_parity: bool,
    readonly: bool = False,
    tools: str | None = None,
) -> tuple[list[str], str | None]:
    """``(argv after the binary, effective model)`` for a ``claude -p`` run.

    ``terminal_parity`` (host runs, #46) drops every "lean boot" override: no
    forced model, no --setting-sources, no strict/remote MCP. The session then
    loads exactly what a terminal on that device loads — its default model,
    user hooks, plugins, MCP servers. Only an EXPLICIT per-call ``model`` still
    applies, because someone asked for it.

    ``tools`` (an agent node's allowlist, Life#157) closes the tool list like
    ``readonly`` does; ``readonly`` still wins, being the stricter of the two.
    """
    closed = readonly or bool(tools)
    args = [
        "-p",
        # A leading "-" would be parsed as a flag by claude itself, and the host
        # bridge refuses such a value (Life#153 M6).
        " " + prompt if prompt.startswith("-") else prompt,
        "--permission-mode",
        # Never bypass with a sender's text inside: read-only gets the default
        # mode plus a closed tool list (#89).
        "default" if closed else "bypassPermissions",
        "--output-format",
        "stream-json",
        "--verbose",
    ]
    if closed:
        args += [
            "--tools",
            READONLY_TOOLS if readonly else tools,
            "--disallowedTools",
            *READONLY_DENY,
            "--disable-slash-commands",
            "--strict-mcp-config",
        ]
    eff_model = model if terminal_parity else (model or CLAUDE_MODEL)
    if eff_model:
        args += ["--model", eff_model]
    if effort:
        args += ["--effort", effort]
    if not terminal_parity:
        # Lean per-turn boot: faster model, no global user hooks/plugins, no MCP.
        if CLAUDE_SETTING_SOURCES:
            args += ["--setting-sources", CLAUDE_SETTING_SOURCES]
        if closed:
            pass  # no MCP at all: strict config above, no --mcp-config
        elif SUITE_MCP_CONFIG:
            args += ["--mcp-config", SUITE_MCP_CONFIG, "--strict-mcp-config"]
        elif CLAUDE_STRICT_MCP:
            args.append("--strict-mcp-config")
    if persona:
        args += ["--append-system-prompt", persona]
    if resume_id:
        args += ["--resume", resume_id]
    return args, eff_model


def _announce_session(on_session, session_id: str) -> None:
    """Tell the caller which session the run is on; never breaks the run."""
    if on_session is None:
        return
    try:
        on_session(session_id)
    except Exception as exc:
        logger.warning("runner: on_session(%s) failed: %s", session_id, exc)


async def _run_claude_cli(
    prompt: str,
    *,
    workspace: str,
    persona: str | None,
    resume_id: str | None = None,
    on_progress=None,
    extra_env: dict | None = None,
    model: str | None = None,
    effort: str | None = None,
    host_cwd: str | None = None,
    max_timeout: int | None = None,
    on_session=None,
) -> RunResult:
    """Run ``claude -p`` (subscription auth) in ``workspace``, streaming events,
    and return (text, session_id|None).

    Uses ``--output-format stream-json`` and reads one event per line, so the
    watchdog is an **inactivity** timeout (reset on every event) rather than a
    total cap: a long-but-healthy task finishes, only a genuinely hung one is
    killed. ``session_id`` is captured from the first event, so even a killed
    session is returned and stays resumable. Optional ``on_progress`` is awaited
    with a short label as tools run.

    ``model`` / ``effort`` override the env defaults for THIS call only (the
    Telegram ``opus/high:`` prefix and the sticky ``/modello`` topic default both
    land here). ``effort`` has no env default: unset means no ``--effort`` flag,
    letting the CLI choose.
    """
    max_s = max_timeout or CLAUDE_MAX_TIMEOUT
    readonly = SESSION_READONLY.get()
    tools = SESSION_TOOLS.get()
    # A closed run (read-only, or an agent node) never leaves the container jail.
    on_host = bool(host_cwd) and not (readonly or tools)
    args, eff_model = claude_args(
        prompt,
        persona=persona,
        resume_id=resume_id,
        model=model,
        effort=effort,
        terminal_parity=on_host,
        readonly=readonly,
        tools=tools,
    )
    logger.info(
        "claude%s: cwd=%s model=%s effort=%s sources=%s idle=%ss max=%ss resume=%s",
        "[host]" if on_host else "",
        host_cwd if on_host else workspace,
        eff_model or "<default>",
        effort or "<default>",
        "<terminal>" if on_host else (CLAUDE_SETTING_SOURCES or "<all>"),
        CLAUDE_IDLE_TIMEOUT,
        max_s,
        resume_id or "<new>",
    )
    # Bound simultaneous spawns (see _claude_gate): acquired before spawn,
    # released in the finally below so every return path frees the slot. The
    # guard releases if the spawn itself fails (before we reach that finally).
    gate = _claude_gate()
    await gate.acquire()
    try:
        if on_host:
            # stdin stays OPEN for the whole run: the wrapper kills claude on
            # EOF, which is how a kill here reaches the host process (#46).
            proc = await asyncio.create_subprocess_exec(
                *host_exec_command(),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                limit=CLAUDE_STREAM_LIMIT,
            )
            proc.stdin.write(host_exec_request(args, host_cwd=host_cwd, extra_env=extra_env))
            await proc.stdin.drain()
        else:
            proc = await asyncio.create_subprocess_exec(
                CLAUDE_BIN,
                *args,
                cwd=workspace,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                limit=CLAUDE_STREAM_LIMIT,
                # Per-wake bus credentials (HUB_URL/HUB_API_KEY/HUB_AGENT_ID): the
                # coordinator KNOWS the live bus + the woken agent's identity, so it
                # injects them here. /hub-send prefers these over the project .env,
                # which can drift (stale HUB_URL/key) and silently post to a dead bus.
                env=({**os.environ, **extra_env} if extra_env else None),
            )
    except BaseException:
        gate.release()
        raise
    # Drain stderr concurrently so a chatty process can't deadlock on a full pipe.
    stderr_task = asyncio.create_task(proc.stderr.read())

    async def _kill() -> None:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        try:
            await proc.wait()
        except Exception:
            pass

    announced: str | None = None
    session_id = resume_id
    result_text: str | None = None
    run_usage: RunUsage | None = None
    started_at = datetime.now(UTC)
    t0 = time.monotonic()
    deadline = t0 + max_s
    pending_tools: set[str] = set()

    def _killed(outcome: str) -> RunUsage:
        return _interrupted_usage(
            engine="claude",
            model=eff_model or "<default>",
            started_at=started_at,
            duration_ms=_elapsed_ms(t0),
            outcome=outcome,
        )

    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                await _kill()
                return RunResult(
                    result_text
                    or f"⏱ Interrotto: superato il limite massimo di {max_s}s.",
                    session_id,
                    run_usage or _killed(OUTCOME_TIMEOUT),
                )
            try:
                idle_s = _idle_window(pending_tools)
                raw = await asyncio.wait_for(
                    proc.stdout.readline(),
                    timeout=min(idle_s, remaining),
                )
            except asyncio.TimeoutError:
                await _kill()
                if time.monotonic() >= deadline:
                    return RunResult(
                        f"⏱ Interrotto: superato il limite massimo di {max_s}s.",
                        session_id,
                        _killed(OUTCOME_TIMEOUT),
                    )
                hint = " — riprendi con /riprendi" if session_id else ""
                return RunResult(
                    f"⏱ Nessuna attività per {idle_s:g}s: probabile "
                    f"blocco, sessione interrotta.{hint}",
                    session_id,
                    _killed(OUTCOME_TIMEOUT),
                )
            except ValueError:
                # Line exceeded CLAUDE_STREAM_LIMIT: skip it, keep streaming.
                logger.warning(
                    "stream line over %d bytes, skipped", CLAUDE_STREAM_LIMIT
                )
                continue
            if not raw:  # EOF: process finished
                break
            ev = _parse_stream_line(raw.decode(errors="replace"))
            if ev is None:
                continue
            _track_tool_calls(ev, pending_tools)
            sid = ev.get("session_id")
            if sid:
                session_id = sid
                if sid != announced:
                    announced = sid
                    _announce_session(on_session, sid)
            if ev.get("type") == "result":
                result_text = _result_from_event(ev)
                run_usage = usage_from_result_event(
                    ev,
                    model=eff_model or "<default>",
                    started_at=started_at,
                    fallback_duration_ms=_elapsed_ms(t0),
                )
            elif on_progress is not None:
                label = _event_progress(ev)
                if label:
                    try:
                        await on_progress(label)
                    except Exception:
                        pass
        await proc.wait()
        stderr = await stderr_task
        if proc.returncode not in (0, None) and not result_text:
            err = stderr.decode(errors="replace")[:1500]
            return RunResult(
                f"❌ claude exit={proc.returncode}\n{err}",
                session_id,
                run_usage or _killed(OUTCOME_ERROR),
            )
        return RunResult(result_text or "(risposta vuota)", session_id, run_usage)
    finally:
        if proc.returncode is None:
            await _kill()
        stdin = getattr(proc, "stdin", None)
        if on_host and stdin is not None and not stdin.is_closing():
            stdin.close()
        if not stderr_task.done():
            stderr_task.cancel()
        gate.release()


# --- OpenCode invocation -----------------------------------------------------
# ``opencode --format json`` has no documented event schema. This is the shape
# observed live against deepseek/deepseek-v4-flash on 2026-08-09: one JSON
# object per line, every line carries top-level "sessionID", and a plain reply
# emits exactly step_start -> text -> step_finish. The reply text sits in
# part.text of the "text" event(s); each event carries the FULL text so far
# for its part.id (confirmed against a single-chunk reply — not a delta), so
# events are stored keyed by part.id and overwritten, never appended.
def _opencode_progress(ev: dict) -> str | None:
    """Short progress label for an opencode stream event, or ``None`` when it
    isn't worth surfacing (text content and step bookkeeping aren't)."""
    t = ev.get("type")
    if t in ("text", "step_start", "step_finish"):
        return None
    part = ev.get("part") or {}
    return f"🔧 {part.get('type') or t}"


async def _run_opencode(
    prompt: str,
    *,
    workspace: str,
    persona: str | None,
    resume_id: str | None = None,
    on_progress=None,
    extra_env: dict | None = None,
    model: str | None = None,
    effort: str | None = None,
    max_timeout: int | None = None,
    on_session=None,
) -> RunResult:
    """Run ``opencode run`` in ``workspace``, same ``(text, session_id)``
    contract as ``_run_claude_cli``.

    No documented ``--append-system-prompt`` equivalent, so ``persona`` is
    prepended to the message itself rather than guessed as a flag that might
    not exist — the same mistake already cost a rewrite once with Codex's
    fictitious ``.agents/``.

    ``effort`` is intentionally NOT forwarded: the closest opencode flag
    (``--variant``) is unverified for DeepSeek, and a wrong value fails the
    call outright rather than degrading gracefully. Reuses the Claude gate,
    idle/max timeouts and stream limit — one shared subprocess budget
    regardless of which backend a given call ends up using.
    """
    max_s = max_timeout or CLAUDE_MAX_TIMEOUT
    message = f"{persona}\n\n---\n\n{prompt}" if persona else prompt
    # ``--dir`` + ``--auto`` are the functional equivalent of claude's
    # ``--permission-mode bypassPermissions``, and they go together (issue #30):
    # --dir pins the project opencode resolves (a run that resolved
    # projectID=global directory=/app saw the node's own folder as *external*),
    # --auto removes the whole class of failures, since `--auto` defaults to
    # false and a headless run that asks for a permission blocks on a question
    # nobody can hear.
    cmd = [OPENCODE_BIN, "run", message, "--dir", workspace, "--auto", "--format", "json"]
    # opencode models are `provider/model`; a bare alias (claude's `sonnet`) is not one
    # and makes opencode fail with "Unexpected server error".
    eff_model = model if model and "/" in model else OPENCODE_MODEL
    if eff_model:
        cmd += ["--model", eff_model]
    if resume_id:
        cmd += ["--session", resume_id]
    logger.info(
        "opencode: cwd=%s model=%s idle=%ss max=%ss resume=%s",
        workspace,
        eff_model or "<default>",
        CLAUDE_IDLE_TIMEOUT,
        max_s,
        resume_id or "<new>",
    )
    gate = _claude_gate()
    await gate.acquire()
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=workspace,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=CLAUDE_STREAM_LIMIT,
            env=({**os.environ, **extra_env} if extra_env else None),
        )
    except BaseException:
        gate.release()
        raise
    stderr_task = asyncio.create_task(proc.stderr.read())

    async def _kill() -> None:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        try:
            await proc.wait()
        except Exception:
            pass

    announced: str | None = None
    session_id = resume_id
    text_by_part: dict[str, str] = {}
    part_order: list[str] = []
    error_text: str | None = None
    # A healthy run always closes with step_finish. Its absence at EOF means the
    # run was cut short — typically a permission prompt left hanging in a
    # headless run (#30) — and until now that left NO trace in the Coordinator's
    # log at all: the only evidence was inside opencode's own log in the
    # container.
    saw_step_finish = False
    meter = _OpencodeMeter()
    started_at = datetime.now(UTC)
    t0 = time.monotonic()
    deadline = t0 + max_s

    def _measured(outcome: str) -> RunUsage:
        return meter.usage(
            model=eff_model or "<default>",
            outcome=outcome,
            started_at=started_at,
            duration_ms=_elapsed_ms(t0),
        )

    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                await _kill()
                return RunResult(
                    "".join(text_by_part[p] for p in part_order)
                    or f"⏱ Interrotto: superato il limite massimo di {max_s}s.",
                    session_id,
                    _measured(OUTCOME_TIMEOUT),
                )
            # Un resume ancora muto ha la sua sveglia, molto piu' corta: e'
            # l'unico caso in cui il silenzio non e' lavoro in corso.
            attesa = (CLAUDE_FIRST_TOKEN_TIMEOUT
                      if (resume_id and not text_by_part)
                      else CLAUDE_IDLE_TIMEOUT)
            try:
                raw = await asyncio.wait_for(
                    proc.stdout.readline(),
                    timeout=min(attesa, remaining),
                )
            except asyncio.TimeoutError:
                await _kill()
                if time.monotonic() >= deadline:
                    return RunResult(
                        f"⏱ Interrotto: superato il limite massimo di {max_s}s.",
                        session_id,
                        _measured(OUTCOME_TIMEOUT),
                    )
                # A RESUMED run that produced nothing at all before going idle
                # never reached the model in a usable way. The classic shape is
                # a session created against another project: the node's own
                # folder is then *external* and the run blocks on a permission
                # question nobody can hear (#30). `--dir` pins the process, not
                # the session, so the only way out is a fresh one. Reporting it
                # as a transport error makes run_claude retry without the resume
                # inside this same call, instead of spending one of the caller's
                # two answer attempts on a session that can only hang again.
                if resume_id and not text_by_part:
                    logger.warning(
                        "opencode: resumed session %s said nothing in %ss "
                        "(cwd=%s) — unusable, starting fresh; a session "
                        "keeps the project it was born with.",
                        resume_id,
                        CLAUDE_FIRST_TOKEN_TIMEOUT,
                        workspace,
                    )
                    return RunResult(
                        f"❌ opencode: sessione {resume_id} muta per "
                        f"{CLAUDE_FIRST_TOKEN_TIMEOUT}s (probabile permesso "
                        "in attesa su una sessione nata in un altro progetto).",
                        None,
                        _measured(OUTCOME_ERROR),
                    )
                hint = " — riprendi con /riprendi" if session_id else ""
                return RunResult(
                    f"⏱ Nessuna attività per {CLAUDE_IDLE_TIMEOUT}s: probabile "
                    f"blocco, sessione interrotta.{hint}",
                    session_id,
                    _measured(OUTCOME_TIMEOUT),
                )
            except ValueError:
                logger.warning(
                    "stream line over %d bytes, skipped", CLAUDE_STREAM_LIMIT
                )
                continue
            if not raw:  # EOF: process finished
                break
            ev = _parse_stream_line(raw.decode(errors="replace"))
            if ev is None:
                continue
            sid = ev.get("sessionID")
            if sid:
                session_id = sid
                if sid != announced:
                    announced = sid
                    _announce_session(on_session, sid)
            t = ev.get("type")
            if t == "step_finish":
                saw_step_finish = True
                meter.add(ev)
            if t == "text":
                part = ev.get("part") or {}
                pid = part.get("id")
                txt = part.get("text")
                if pid and txt is not None:
                    if pid not in text_by_part:
                        part_order.append(pid)
                    text_by_part[pid] = txt  # full-so-far, not a delta: overwrite
            elif t == "error":
                err = (ev.get("error") or {}).get("data") or {}
                error_text = (
                    err.get("message")
                    or (ev.get("error") or {}).get("name")
                    or "unknown error"
                )
            elif on_progress is not None:
                label = _opencode_progress(ev)
                if label:
                    try:
                        await on_progress(label)
                    except Exception:
                        pass
        await proc.wait()
        stderr = await stderr_task
        result_text = "".join(text_by_part[p] for p in part_order)
        if error_text:
            return RunResult(
                f"❌ opencode: {error_text}", session_id, _measured(OUTCOME_ERROR)
            )
        if proc.returncode not in (0, None) and not result_text:
            err = stderr.decode(errors="replace")[:1500]
            return RunResult(
                f"❌ opencode exit={proc.returncode}\n{err}",
                session_id,
                _measured(OUTCOME_ERROR),
            )
        if not saw_step_finish:
            err = stderr.decode(errors="replace")[:500]
            logger.warning(
                "opencode: stream closed without step-finish (exit=%s, cwd=%s, "
                "session=%s) — run cut short, likely a pending permission prompt. %s",
                proc.returncode,
                workspace,
                session_id or "<new>",
                err,
            )
            if not result_text:
                return RunResult(
                    "❌ opencode: stream chiuso senza step-finish "
                    f"(exit={proc.returncode}): run interrotto prima di rispondere.",
                    session_id,
                    _measured(OUTCOME_ERROR),
                )
        return RunResult(
            result_text or "(risposta vuota)",
            session_id,
            _measured(OUTCOME_OK if saw_step_finish else OUTCOME_ERROR),
        )
    finally:
        if proc.returncode is None:
            await _kill()
        if not stderr_task.done():
            stderr_task.cancel()
        gate.release()
