# gate.py
"""Deterministic pre-check for notifications.

A notification may declare a ``gate`` shell command in ``params["gate"]``. The
scheduler runs it (in the project workspace) *before* invoking the content
provider; a non-zero exit means "skip this run" — the provider, and any LLM
call behind ``provider="agent"``, never runs. This keeps expensive/noisy
notifications (e.g. an LLM briefing) from firing when there is nothing to say.

Design:
- absent gate -> proceed (back-compat with every existing notification).
- exit 0 -> proceed;  exit != 0 -> skip (the gate's normal "no" signal).
- timeout / launch failure -> **skip** (fail-closed) and log at error level:
  the feature exists to suppress unwanted firing, so an unevaluable gate
  defaults to the quiet side. A daily cron self-heals next run, and inside a
  real departure window the gate is evaluated repeatedly.

Subprocess only — no bot imports, keeping the import graph acyclic.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

import projects
from notifications import Notification

logger = logging.getLogger("coordinator.gate")

GATE_TIMEOUT_SEC = 30.0


def _workspace_for(notif: Notification) -> str | None:
    """Project workspace to run the gate in (cwd), or None if unresolvable."""
    proj = projects.resolve_project(notif.params.get("project", ""))
    return proj.workspace if proj is not None else None


DETAIL_MAX_CHARS = 200


@dataclass(frozen=True)
class GateVerdict:
    """Why a gate opened or closed.

    ``detail`` exists because "the gate said no" and "the gate could not run"
    produce the same silence. travel-briefing-partenza closed every day for
    weeks with a perfectly healthy-looking "skipped by gate": its gate script
    had moved out of the repo, so `python3 …/trip_gate.py` exited 2 with "No
    such file or directory" — a broken gate wearing the face of "no trip today".
    Carrying the reason into the outcome makes the difference visible in
    `POST /{key}/test` instead of only in a log line nobody reads.
    """

    passed: bool
    detail: str = ""


def _short(text: str) -> str:
    """One line, bounded — the detail travels into API responses."""
    flat = " ".join(text.split())
    return flat if len(flat) <= DETAIL_MAX_CHARS else flat[: DETAIL_MAX_CHARS - 1] + "…"


async def evaluate_gate(notif: Notification) -> GateVerdict:
    """Run the notification's gate command and report the verdict with its reason."""
    command = (notif.params.get("gate") or "").strip()
    if not command:
        return GateVerdict(True)
    cwd = _workspace_for(notif)
    try:
        proc = await asyncio.create_subprocess_shell(
            command,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except Exception as exc:
        logger.exception("gate for %s failed to launch (%r); skipping", notif.key, command)
        return GateVerdict(False, _short(f"gate non eseguibile: {exc}"))
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=GATE_TIMEOUT_SEC)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        logger.error("gate for %s timed out after %ss; skipping", notif.key, GATE_TIMEOUT_SEC)
        return GateVerdict(False, _short(f"gate scaduto dopo {GATE_TIMEOUT_SEC}s"))
    reason = (stderr or stdout or b"").decode("utf-8", "replace").strip()
    if proc.returncode == 0:
        logger.info("gate OPEN for %s: %s", notif.key, reason or "(exit 0)")
        return GateVerdict(True, _short(reason))
    logger.info("gate CLOSED for %s (exit %s): %s", notif.key, proc.returncode, reason)
    return GateVerdict(False, _short(f"exit {proc.returncode}: {reason}" if reason
                                     else f"exit {proc.returncode}"))


async def passes_gate(notif: Notification) -> bool:
    """Back-compat wrapper: True -> proceed, False -> skip."""
    return (await evaluate_gate(notif)).passed
