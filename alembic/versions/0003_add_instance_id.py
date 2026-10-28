"""add per-row instance_id (operator contract, Life §4.2)

Coordinator is an *operator*: it serves every Life instance registered on its
device (Life#78 / Coordinator##78, spec
``2026-10-14-framework-operatore-istanza-design`` §4.2). Every bridge-domain
record therefore carries the ``instance_id`` of the instance that produced it.

Additive and non-destructive: the column is ``NOT NULL`` with a server default
so existing rows are backfilled with the instance that produced them — at
migration time that is the device's default instance (the Coordinator served
exactly one until now). Never a DROP. Indexed, because reads filter by instance.

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-14
"""

from __future__ import annotations

import os
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLES = ("notifications", "notif_runs")
_FALLBACK_INSTANCE = "personal"


def _backfill_instance() -> str:
    """L'istanza che ha prodotto le righe esistenti: il default del device.

    Prova il registro del device (``instances.default_instance_id``), poi le
    variabili legacy, e solo come ultima rete un valore neutro. Il valore e'
    un singolo backfill: prima della multi-istanza il device ne serviva una.
    """
    try:
        import instances  # project root is on sys.path via alembic/env.py

        resolved = instances.default_instance_id()
        if resolved:
            return resolved
    except Exception:
        pass
    for var in ("LIFE_INSTANCE", "COORDINATOR_DEFAULT_INSTANCE"):
        value = (os.environ.get(var) or "").strip()
        if value:
            return value
    legacy_dir = (os.environ.get("LIFE_INSTANCE_DIR") or "").strip().rstrip("/\\")
    if legacy_dir:
        return os.path.basename(legacy_dir)
    return _FALLBACK_INSTANCE


def upgrade() -> None:
    backfill = _backfill_instance().replace("'", "''")
    for table in _TABLES:
        op.add_column(
            table,
            sa.Column(
                "instance_id",
                sa.String(length=128),
                nullable=False,
                server_default=sa.text(f"'{backfill}'"),
            ),
        )
        op.create_index(f"ix_{table}_instance_id", table, ["instance_id"])


def downgrade() -> None:
    for table in _TABLES:
        op.drop_index(f"ix_{table}_instance_id", table_name=table)
        op.drop_column(table, "instance_id")
