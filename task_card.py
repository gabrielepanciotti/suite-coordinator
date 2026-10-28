# task_card.py
"""Render the Telegram reminder card for an inbound Scheduler ``task.due``
event (M5, issue task-6). Pure/testable: no Telegram network calls, no bot.py
state — only ``telegram``'s (data-only) keyboard classes, ``task_routing`` and
``cb_pack.pack_cb``. ``bot.py`` imports this module for ``_on_task_due`` and
the ``tk:*`` callback handler; see ``cb_pack.py`` for why ``pack_cb`` lives in
its own leaf module rather than being imported from ``bot``.

Event shape (from Scheduler, see task-6 brief):
``{"type":"task.due","scheduled_task_id":int,"task_id":int,
"user_telegram_chat_id":int,"name":str,"description":str,"category":str,
"start_at":ISO8601,"end_at":ISO8601,"duration_min":int,"phase":"pre"}``
"""

from __future__ import annotations

import datetime as _dt

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

import task_routing
from cb_pack import pack_cb

try:
    # Cheap in-memory lookup (PROJECTS is a tuple built once at import time) —
    # used only for a friendlier "Apri con <label>" button text. Optional: if
    # projects.py can't be imported for any reason the button falls back to
    # the bare project key, never crashes the card.
    import projects as _projects
except Exception:  # pragma: no cover - defensive only
    _projects = None


def _hhmm(start_at: str | None) -> str | None:
    """Parse ``start_at`` (ISO 8601, may carry a timezone offset) into
    ``HH:MM``. Returns ``None`` on anything missing or malformed — never
    raises, so a bad Scheduler payload never breaks card rendering."""
    if not start_at:
        return None
    try:
        return _dt.datetime.fromisoformat(str(start_at)).strftime("%H:%M")
    except ValueError:
        return None


def render_text(evt: dict) -> str:
    """ "⏰ Tra poco: «{name}» — ore {hh:mm}" (time omitted if unparsable)."""
    name = evt.get("name") or "Task"
    hhmm = _hhmm(evt.get("start_at"))
    text = f"⏰ Tra poco: «{name}»"
    if hhmm:
        text += f" — ore {hhmm}"
    return text


def _specialist_label(category: str | None) -> str:
    """Human-friendly label for the "Apri" button: the specialist project's
    display label if the registry has it loaded on this host, else the raw
    project key (still a valid, if less pretty, button caption)."""
    project_key, _agent = task_routing.resolve_specialist(category)
    if _projects is not None:
        proj = _projects.resolve_project(project_key)
        if proj is not None:
            return proj.label
    return project_key


def render_keyboard(evt: dict) -> InlineKeyboardMarkup:
    """Apri (specialist) / Fatto / Posticipa buttons, callback_data keyed on
    ``scheduled_task_id`` (``tk:open|done|pp:{st_id}``, see cb_pack.pack_cb)."""
    st_id = str(evt.get("scheduled_task_id"))
    label = _specialist_label(evt.get("category"))
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    f"▶️ Apri con {label}", callback_data=pack_cb("tk", "open", st_id)
                )
            ],
            [
                InlineKeyboardButton("✅ Fatto", callback_data=pack_cb("tk", "done", st_id)),
                InlineKeyboardButton("⏰ Posticipa", callback_data=pack_cb("tk", "pp", st_id)),
            ],
        ]
    )
