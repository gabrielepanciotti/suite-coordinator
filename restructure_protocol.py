# restructure_protocol.py
"""Typed command/verdict bus protocol for per-node agents restructuring (Wave B).

Pure logic — no I/O. The coordinator dispatches one `agents_restructure` command
to each target node; the woken persona re-runs its own suite conform, reconciles
its domain agents, registers the device-aware persona on the bus, and replies
with a `restructure_verdict` in the message context. A missing/malformed verdict
parses to None -> the coordinator treats it as a failure, never a pass. A verdict
with ``blocked: true`` signals the node needs a human decision (e.g. legacy agent
classification) before it can finish.
"""
from __future__ import annotations


def build_command(rid: int, persona_archetype: str = "node-persona") -> dict:
    return {"agents_restructure": {
        "id": rid, "target": "canonical", "persona_archetype": persona_archetype}}


def parse_verdict(context, rid: int, agent: str) -> dict | None:
    """Extract a restructure_verdict matching (id, agent). None if absent/
    malformed/mismatched. Always includes a ``blocked`` key; a blocked verdict
    has ok=False and carries ``decision_needed``."""
    if not isinstance(context, dict):
        return None
    v = context.get("restructure_verdict")
    if not isinstance(v, dict):
        return None
    try:
        vid = int(v.get("id", -1))
    except (TypeError, ValueError):
        return None
    if vid != rid or v.get("agent") != agent:
        return None
    agents = v.get("agents") if isinstance(v.get("agents"), list) else []
    removed = (v.get("removed_candidates")
               if isinstance(v.get("removed_candidates"), list) else [])
    if v.get("blocked"):
        return {"ok": False, "blocked": True, "persona_name": str(v.get("persona_name", "")),
                "agents": list(agents), "removed_candidates": list(removed),
                "summary": str(v.get("summary", "")),
                "decision_needed": str(v.get("decision_needed", "")), "agent": agent}
    return {"ok": bool(v.get("ok")), "blocked": False,
            "persona_name": str(v.get("persona_name", "")),
            "agents": list(agents), "removed_candidates": list(removed),
            "summary": str(v.get("summary", "")), "agent": agent}


def verify_manifest(verdict: dict) -> bool:
    """Inline orchestrator check (design §7): the node reported a coherent roster
    where its device-aware persona is present. Roster GET against the bus stays a
    manual rollout check (no client method exists)."""
    name = verdict.get("persona_name") or ""
    return bool(name) and name in (verdict.get("agents") or [])
