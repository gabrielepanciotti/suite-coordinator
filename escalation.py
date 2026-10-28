# escalation.py
"""Typed 'blocked / needs-human-decision' outcome for the agent bus.

Pure logic — no I/O. An agent posts a typed escalation block when it cannot
proceed without human go/no-go (GDPR consent, irreversible action, identity
ambiguity, etc.).  The coordinator surfaces the escalation to the human;
the human replies in free-text; interpret_decision maps the reply to a clear
"approve" | "deny" | None, guaranteeing that ambiguity is never silently
treated as approval.
"""
from __future__ import annotations

APPROVE = ("procedi", "ok", "sì", "si", "approva", "approvo", "vai", "autorizzo", "conferma")
DENY    = ("no", "annulla", "stop", "ferma", "nega", "rifiuta")


def build_block(reason: str, decision_needed: str, *, origin: str = "") -> dict:
    """Return the typed context an agent posts when it needs a human go/no-go.

    Shape: {"escalation": {"blocked": True, "reason": <str>,
                           "decision_needed": <str>, "origin": <str>}}
    All values are coerced with str().
    """
    return {"escalation": {
        "blocked": True,
        "reason": str(reason),
        "decision_needed": str(decision_needed),
        "origin": str(origin),
    }}


def parse_escalation(context) -> dict | None:
    """Return {"reason", "decision_needed", "origin"} if *context* carries a
    typed escalation, else None.

    Recognises two forms:
    1. Standalone envelope — context["escalation"] is a dict with truthy "blocked".
    2. Blocked typed verdict — context["migrate_verdict"] or
       context["pipeline_verdict"] is a dict with truthy "blocked"; the reason
       is taken from verdict["summary"], decision_needed from
       verdict.get("decision_needed", ""), and origin from the verdict key name.

    Guard: returns None for any non-dict context.  Never raises.
    """
    if not isinstance(context, dict):
        return None

    # --- 1. standalone escalation envelope -----------------------------------
    e = context.get("escalation")
    if isinstance(e, dict) and e.get("blocked"):
        return {
            "reason": str(e.get("reason", "")),
            "decision_needed": str(e.get("decision_needed", "")),
            "origin": str(e.get("origin", "")),
        }

    # --- 2. blocked flag inside a typed verdict ------------------------------
    for key in ("migrate_verdict", "pipeline_verdict"):
        v = context.get(key)
        if isinstance(v, dict) and v.get("blocked"):
            return {
                "reason": str(v.get("summary", "")),
                "decision_needed": str(v.get("decision_needed", "")),
                "origin": key,
            }

    return None


def interpret_decision(text: str) -> str | None:
    """Map a human's free-text reply to "approve" | "deny" | None.

    Rule:
    - Lowercase + strip the input.
    - Take the first whitespace-delimited token.
    - Strip surrounding punctuation (.,!?;:) from that token.
    - Match against APPROVE / DENY; anything else (or empty input) → None.

    None means ambiguous — the caller must re-ask.  This function NEVER
    guesses "approve" when in doubt.
    """
    if not text:
        return None
    stripped = text.strip()
    if not stripped:
        return None
    token = stripped.lower().split()[0].strip(".,!?;:")
    if token in APPROVE:
        return "approve"
    if token in DENY:
        return "deny"
    return None
