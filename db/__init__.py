"""Coordinator node database layer (additive, optional).

The node has historically persisted all state as JSON files under ``data/``
(notifications.json, notif_runs.json, state.json, conversations.jsonl, ...).
Those files remain the source of truth and keep working untouched.

This package adds an OPTIONAL Postgres-backed mirror for the bridge domain
(scheduled notifications + their run history), wired via ``DATABASE_URL``.
It is fully reversible: if ``DATABASE_URL`` is unset, nothing here is used.
"""

from db.models import Base, Notification, NotifRun  # noqa: F401
from db.session import get_engine, get_sessionmaker, session_scope  # noqa: F401
