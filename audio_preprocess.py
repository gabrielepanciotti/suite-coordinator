"""Run a project's audio pre-processing command (e.g. lesson transcription).

Transcribing a 2-hour lesson takes about an hour on the minipc CPU: far beyond
the agent-turn watchdog in ``claude_runner``. A project therefore declares the
command in its manifest (``[telegram] audio_preprocess``) and the bot runs it
here, as a plain subprocess outside any agent turn, before handing the result
to the agent.

No shell is ever involved: the template is split once with ``shlex`` and the
``{file}`` / ``{out}`` placeholders are substituted inside each token, so a
filename with spaces, quotes or ``;`` stays a single argv element.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import shlex
from pathlib import Path

logger = logging.getLogger(__name__)

STDERR_TAIL = 2000
DEFAULT_TIMEOUT_S = int(os.environ.get("AUDIO_PREPROCESS_TIMEOUT_S", 6 * 3600))
_PLACEHOLDER = re.compile(r"\{(file|out|topic)\}")

# One transcription at a time on the box: two whisper runs in parallel would
# just double the time of both and the memory pressure. Kept per event loop
# because an asyncio.Semaphore binds to the loop that first waits on it.
_SEMAPHORES: dict[int, tuple[asyncio.AbstractEventLoop, asyncio.Semaphore]] = {}


def _semaphore() -> asyncio.Semaphore:
    loop = asyncio.get_running_loop()
    entry = _SEMAPHORES.get(id(loop))
    if entry is None or entry[0] is not loop:
        # Drop semaphores of loops that no longer run (tests open many loops).
        for key, (old, _) in list(_SEMAPHORES.items()):
            if old.is_closed():
                del _SEMAPHORES[key]
        entry = (loop, asyncio.Semaphore(1))
        _SEMAPHORES[id(loop)] = entry
    return entry[1]


def build_command(template: str, file: Path, out: Path, topic: str = "") -> list[str]:
    """argv for ``template`` with ``{file}``/``{out}``/``{topic}`` replaced in each token.

    Substitution is a single pass per token, so a filename that itself
    contains ``{out}`` is never expanded a second time.
    """
    values = {"file": str(file), "out": str(out), "topic": topic}
    return [_PLACEHOLDER.sub(lambda m: values[m.group(1)], tok) for tok in shlex.split(template)]


async def _tail(stream: asyncio.StreamReader | None) -> str:
    """Read ``stream`` to EOF keeping only the last STDERR_TAIL characters."""
    if stream is None:
        return ""
    buf = b""
    while True:
        chunk = await stream.read(8192)
        if not chunk:
            break
        buf = (buf + chunk)[-STDERR_TAIL * 4:]
    return buf.decode("utf-8", errors="replace")[-STDERR_TAIL:]


async def run_preprocess(
    cmd: list[str], cwd: Path, timeout_s: int = DEFAULT_TIMEOUT_S
) -> tuple[bool, str]:
    """Run ``cmd`` in ``cwd``. (True, stderr tail) on exit 0, else (False, reason).

    ``reason`` is the stderr tail (last 2000 chars), ``"timeout"`` when the
    process was killed for running past ``timeout_s``, or the spawn error.
    Runs are serialized: a second call waits for the first to finish.
    """
    async with _semaphore():
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=str(cwd),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as exc:
            return False, f"{type(exc).__name__}: {exc}"[-STDERR_TAIL:]
        reader = asyncio.ensure_future(_tail(proc.stderr))
        try:
            await asyncio.wait_for(proc.wait(), timeout=timeout_s)
        except TimeoutError:
            logger.warning("audio preprocess timed out after %ss: %s", timeout_s, cmd[:3])
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            await proc.wait()
            reader.cancel()
            try:
                await reader
            except (asyncio.CancelledError, Exception):
                pass
            return False, "timeout"
        tail = await reader
        if proc.returncode != 0:
            # Reason first, so the tail keeps ending on the script's last words.
            reason = exit_reason(proc.returncode)
            return False, f"{reason}\n{tail[-(STDERR_TAIL - len(reason) - 1):]}".rstrip()
        return True, tail


# SIGKILL as seen directly (-9), through a shell (137) or re-raised by a Python
# wrapper with sys.exit(-9) (247): on the minipc it has always been the OOM killer.
_KILLED = {-9, 137, 247}


def exit_reason(code: int) -> str:
    """One line for the owner DM: the stderr tail alone hid an OOM kill (2026-10-16)."""
    if code in _KILLED:
        return f"exit {code}: processo ucciso (SIGKILL, probabile memoria esaurita)"
    return f"exit {code}"
