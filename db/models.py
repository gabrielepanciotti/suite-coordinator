"""SQLAlchemy 2.0 models for the Coordinator bridge domain.

Minimal schema mirroring the JSON state the node already keeps:

- ``notifications``: one row per scheduled notification (the same shape as the
  entries in ``data/notifications.json``).
- ``notif_runs``: append-only history of when each notification last ran
  (richer than ``data/notif_runs.json``, which only stores the last run ts).
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    String,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    mapped_column,
    relationship,
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Notification(Base):
    """A scheduled notification (cron-style ``schedule`` + opaque ``payload``)."""

    __tablename__ = "notifications"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    title: Mapped[str | None] = mapped_column(String(256), nullable=True)
    schedule: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # Life instance that produced this row (Coordinator#78, contract §4.2).
    # NOT NULL + indexed: an operator serves every instance of the device.
    instance_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    runs: Mapped[list["NotifRun"]] = relationship(
        back_populates="notification",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"<Notification key={self.key!r} enabled={self.enabled}>"


class NotifRun(Base):
    """One execution record for a notification."""

    __tablename__ = "notif_runs"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    notification_key: Mapped[str] = mapped_column(
        ForeignKey("notifications.key", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    ran_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="ok")
    detail: Mapped[str | None] = mapped_column(String, nullable=True)
    # Same per-instance partition as ``notifications`` (contract §4.2).
    instance_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)

    notification: Mapped["Notification"] = relationship(back_populates="runs")

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"<NotifRun key={self.notification_key!r} status={self.status!r}>"
