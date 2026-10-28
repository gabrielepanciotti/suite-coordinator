# migrate_protocol.py
"""Typed command/verdict bus protocol for cross-host migrations.

Pure logic — no I/O. The coordinator dispatches a `migrate` command to each
db-owner agent per phase; the agent replies with a `migrate_verdict` in the
message context (read via conv fetch, since the SSE event omits context).
A missing/malformed verdict parses to None → the coordinator treats it as a
failure, never a pass. A verdict with ``blocked: true`` signals that the agent
needs a human decision before it can proceed (GDPR/irreversible/auth gate).
"""
from __future__ import annotations

PHASES = ("prepare", "verify", "commit")


def build_command(mig_id: int, phase: str, rev: str, args: dict | None = None) -> dict:
    return {"migrate": {"id": mig_id, "phase": phase, "rev": rev,
                        "args": dict(args or {})}}


def parse_verdict(context, mig_id: int, phase: str, agent: str) -> dict | None:
    """Extract a migrate_verdict matching (id, phase, agent). None if absent/
    malformed/mismatched. Returns a dict always including a ``blocked`` key;
    a blocked verdict has ``ok=False`` and carries ``decision_needed``."""
    if not isinstance(context, dict):
        return None
    v = context.get("migrate_verdict")
    if not isinstance(v, dict):
        return None
    try:
        vid = int(v.get("id", -1))
    except (TypeError, ValueError):
        return None
    if vid != mig_id or v.get("phase") != phase or v.get("agent") != agent:
        return None
    if v.get("blocked"):
        return {"ok": False, "blocked": True, "summary": str(v.get("summary", "")),
                "decision_needed": str(v.get("decision_needed", "")), "agent": agent}
    return {"ok": bool(v.get("ok")), "blocked": False, "summary": str(v.get("summary", "")),
            "agent": agent}
