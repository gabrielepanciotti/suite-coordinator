"""Pre-gate "dati" per i watcher taskgen — subprocess deterministico.

pre_gate=="temporal" (o altro) -> no-op True: la cadenza cron È il gate.
pre_gate=="dati" -> esegue watcher.gate_cmd; exit 0 = sveglia il nodo.
Fail-closed: assenza cmd / launch-failure / timeout / exit!=0 -> False
(identico a gate.passes_gate). Nessun import di bot.py."""
from __future__ import annotations

import asyncio
import logging

from taskgen_config import Watcher

logger = logging.getLogger("coordinator.taskgen_gate")

GATE_TIMEOUT_SEC = 30.0


async def passes_pre_gate(watcher: Watcher, *, workspace: str | None) -> bool:
    if watcher.pre_gate != "dati":
        return True
    command = (watcher.gate_cmd or "").strip()
    if not command:
        logger.info("taskgen gate CLOSED for %s: pre_gate=dati but no gate_cmd", watcher.key)
        return False
    try:
        proc = await asyncio.create_subprocess_shell(
            command,
            cwd=workspace,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except Exception:
        logger.exception("taskgen gate for %s failed to launch (%r); skipping", watcher.key, command)
        return False
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=GATE_TIMEOUT_SEC)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        logger.error("taskgen gate for %s timed out; skipping", watcher.key)
        return False
    reason = (stderr or stdout or b"").decode("utf-8", "replace").strip()
    if proc.returncode == 0:
        logger.info("taskgen gate OPEN for %s: %s", watcher.key, reason or "(exit 0)")
        return True
    logger.info("taskgen gate CLOSED for %s (exit %s): %s", watcher.key, proc.returncode, reason)
    return False
