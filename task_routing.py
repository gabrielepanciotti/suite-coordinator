"""Category -> specialist routing + seed-prompt building for M5 task reminders.

Pure helpers (no Telegram/httpx/bot imports) so they unit-test in isolation.
`resolve_specialist` maps a scheduled task's ``category`` to the (project_key,
agent_name) tuple that ``bot._bind_topic``/``projects.resolve_project`` expect
downstream. Project keys are the real, mixed-case registry keys from
``projects.py`` (e.g. ``personalChef``, ``scheduler``); the agent name is the
literal string ``"generale"`` used throughout ``bot.py`` for the synthetic
Generale persona (see ``projects.GENERALE.name``).
"""

from __future__ import annotations

import manifest
import projects

# Seed mapping (M5 spec): category -> project key. Extend as new specialist
# nodes come online. Matching is case-insensitive (see resolve_specialist).
CATEGORY_NODE_MAP: dict[str, str] = {
    "cucina": "personalChef",
    "funghi": "mycorise",
}

# Fallback node when the category is missing or unmapped: Scheduler, the
# general-purpose personal-life node (real key from projects.py).
FALLBACK_PROJECT_KEY = "scheduler"
FALLBACK_AGENT_NAME = "generale"


def resolve_specialist(category: str | None) -> tuple[str, str]:
    """Resolve a task category to (project_key, agent_name).

    Case-insensitive on ``category``. Unmapped or missing categories fall back
    to the Scheduler node's Generale agent.
    """
    if category:
        project_key = CATEGORY_NODE_MAP.get(category.strip().lower())
        if project_key:
            return project_key, FALLBACK_AGENT_NAME
    return FALLBACK_PROJECT_KEY, FALLBACK_AGENT_NAME


def resolve_card_target(evt: dict, mode: str) -> tuple[int, int] | None:
    """Resolve where the task.due card should land, given the profile's ``mode``.

    Pure (imports only ``manifest``/``projects``; no Telegram). Modes:

    - ``"dm"`` -> the task owner's DM ``(user_telegram_chat_id, 0)``.
    - ``"project_group"`` -> the resolved specialist node's own Telegram group
      topic; falls back to the owner's DM when the node has no group configured
      or the project can't be resolved.
    - ``"coordinator_topic"`` (or any unknown mode) -> ``None``, signalling the
      caller to resolve/auto-create the shared Coordinator hub topic.
    """
    if mode == "dm":
        return evt["user_telegram_chat_id"], 0
    if mode == "project_group":
        project_key, _agent = resolve_specialist(evt.get("category"))
        project = projects.resolve_project(project_key)
        tg = manifest.telegram_of(project) if project is not None else None
        if tg is not None and tg.group_chat_id:
            return tg.group_chat_id, tg.group_thread_id
        return evt["user_telegram_chat_id"], 0
    return None


def _hhmm_of(start_at: str | None) -> str | None:
    """Extract ``HH:MM`` from an ISO 8601 timestamp; ``None`` if unparsable."""
    if not start_at or "T" not in start_at:
        return None
    time_part = start_at.split("T", 1)[1]
    return time_part[:5] if len(time_part) >= 5 else None


def build_seed(event: dict) -> str:
    """Compose the compact prompt handed to the specialist session (spec §3.5).

    Any missing event field is gracefully omitted — this never raises.
    """
    name = event.get("name") or "Task"
    category = event.get("category")
    duration_min = event.get("duration_min")
    hhmm = _hhmm_of(event.get("start_at"))
    description = event.get("description")

    details: list[str] = []
    if category:
        details.append(str(category))
    if duration_min is not None:
        details.append(f"{duration_min} min")
    if hhmm:
        details.append(f"ore {hhmm}")
    details_str = f" ({', '.join(details)})" if details else ""

    seed = (
        f"L'utente sta per iniziare questo task pianificato. **{name}**{details_str}."
    )
    if description:
        seed += f" {description}."
    seed += " Assistilo a farlo; se ti serve altro contesto chiama la tua agent-API /context."
    return seed
