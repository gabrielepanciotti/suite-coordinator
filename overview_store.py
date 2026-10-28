# overview_store.py
"""Persistence for per-project notification pauses received from Network overview.

Stores a mapping {project identifier: ISO8601_UTC_str | null} in data/overview_pauses.json.
Atomic writes (temp+rename) — same pattern as hub_store.py / notif_store.py.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import UTC, datetime

from atomic_io import replace_with_retry

logger = logging.getLogger("coordinator.hub")

OVERVIEW_PAUSES_PATH = os.environ.get(
    "OVERVIEW_PAUSES_PATH", "/app/data/overview_pauses.json"
)


def _load() -> dict[str, str | None]:
    try:
        with open(OVERVIEW_PAUSES_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    return data


def _save(data: dict[str, str | None]) -> None:
    d = os.path.dirname(OVERVIEW_PAUSES_PATH) or "."
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
        replace_with_retry(tmp, OVERVIEW_PAUSES_PATH)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def set_pause(project: str, until: str | None) -> None:
    """Persist a pause deadline for *project* (ISO8601 UTC string, or None = clear)."""
    data = _load()
    if until is None:
        data.pop(project, None)
    else:
        data[project] = until
    _save(data)


def get_pause(project: str) -> str | None:
    """Return the raw ISO8601 deadline string for *project*, or None if not set."""
    return _load().get(project)


def is_paused(project: str) -> bool:
    """True iff the project has an active (future) pause deadline right now."""
    raw = _load().get(project)
    if not raw:
        return False
    try:
        until = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return until > datetime.now(tz=UTC)
    except (ValueError, TypeError):
        logger.warning("overview_store: invalid pause datetime %r for %s", raw, project)
        return False


def apply_pauses(projects_payload: list[dict]) -> None:
    """Update the pause map from a schedule-snapshot response payload.

    *projects_payload* is the ``"projects"`` list from Network's response:
    ``[{"project": "<key>", "notifications_paused_until": "<ISO8601>"|null}, ...]``
    """
    for entry in projects_payload:
        project = entry.get("project")
        until = entry.get("notifications_paused_until")
        if project:
            set_pause(project, until)
