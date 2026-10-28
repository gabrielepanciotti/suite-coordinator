"""initial bridge schema: notifications + notif_runs

Revision ID: 0001
Revises:
Create Date: 2026-06-22
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "notifications",
        sa.Column("key", sa.String(length=128), nullable=False),
        sa.Column("title", sa.String(length=256), nullable=True),
        sa.Column("schedule", sa.String(length=64), nullable=False),
        sa.Column(
            "payload",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("key"),
    )
    op.create_table(
        "notif_runs",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("notification_key", sa.String(length=128), nullable=False),
        sa.Column("ran_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "status",
            sa.String(length=32),
            nullable=False,
            server_default=sa.text("'ok'"),
        ),
        sa.Column("detail", sa.String(), nullable=True),
        sa.ForeignKeyConstraint(
            ["notification_key"],
            ["notifications.key"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_notif_runs_notification_key",
        "notif_runs",
        ["notification_key"],
    )


def downgrade() -> None:
    op.drop_index("ix_notif_runs_notification_key", table_name="notif_runs")
    op.drop_table("notif_runs")
    op.drop_table("notifications")
