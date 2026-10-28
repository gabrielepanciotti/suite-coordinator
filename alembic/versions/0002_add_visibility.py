"""add per-row visibility flag (+ origin) to data tables

Life standardization (roadmap dim.4, node-realignment §5): every data table
carries a per-row ``visibility`` column (``public``|``private``, default
``private``), decided at archival time, plus an optional ``origin``
(``seed``|``user``). The contribution bundle is the dump of ``public`` rows
only — clean by construction. Same flag the brain already uses
(``brain_chunks.visibility``), extended to the node's data tables.

Additive & reversible. Postgres-only DDL (the per-node DB is Postgres); on
SQLite the CHECK/index still apply harmlessly.

Revision ID: 0002
Revises: 0001
Create Date: 2026-06-24
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLES = ("notifications", "notif_runs")


def upgrade() -> None:
    for table in _TABLES:
        op.add_column(
            table,
            sa.Column(
                "visibility",
                sa.String(length=16),
                nullable=False,
                server_default=sa.text("'private'"),
            ),
        )
        op.create_check_constraint(
            f"ck_{table}_visibility",
            table,
            "visibility IN ('public', 'private')",
        )
        op.create_index(f"ix_{table}_visibility", table, ["visibility"])
        op.add_column(
            table,
            sa.Column("origin", sa.String(length=16), nullable=True),
        )


def downgrade() -> None:
    for table in _TABLES:
        op.drop_column(table, "origin")
        op.drop_index(f"ix_{table}_visibility", table_name=table)
        op.drop_constraint(f"ck_{table}_visibility", table, type_="check")
        op.drop_column(table, "visibility")
