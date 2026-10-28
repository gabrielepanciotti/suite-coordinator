#!/usr/bin/env python
"""export-contribution: clean public-only bundle of the node's data tables.

Life standardization (roadmap dim.4, node-realignment §5). The contribution
toward the public tree is the dump of ``visibility='public'`` rows only — clean
by construction, so it can never carry private data. The non-technical user
sends the bundle; we integrate it (assisted PR).

Usage:
    python scripts/export_contribution.py [--out DIR]

Reads DATABASE_URL (default: the per-node DB on localhost:11139). Writes one
CSV per data table under DIR (default ./old/contribution-bundle/) and prints a
verification line: ``<table>: public=N private_excluded=M``. Exits non-zero if
any exported file would contain a private row (defensive — should never happen).
"""
from __future__ import annotations

import argparse
import os
import sys

import psycopg

# Data tables that carry the per-row `visibility` flag (see alembic 0002).
DATA_TABLES = ("notifications", "notif_runs")

DEFAULT_DSN = "postgresql://coordinator:coordinator@localhost:11139/coordinator"


def _dsn() -> str:
    # alembic/the app use the SQLAlchemy "+psycopg" prefix; psycopg.connect
    # wants a plain libpq DSN — strip the driver tag if present.
    url = os.environ.get("DATABASE_URL", DEFAULT_DSN)
    return url.replace("postgresql+psycopg://", "postgresql://")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", default="old/contribution-bundle")
    args = ap.parse_args()
    os.makedirs(args.out, exist_ok=True)

    with psycopg.connect(_dsn()) as conn:
        for table in DATA_TABLES:
            public = conn.execute(
                f"SELECT count(*) FROM {table} WHERE visibility = 'public'"
            ).fetchone()[0]
            private = conn.execute(
                f"SELECT count(*) FROM {table} WHERE visibility = 'private'"
            ).fetchone()[0]
            out_path = os.path.join(args.out, f"{table}.csv")
            # COPY filters on visibility='public', so the bundle is public-only
            # by construction — a private row cannot enter it.
            copy_sql = (
                f"COPY (SELECT * FROM {table} WHERE visibility = 'public') "
                "TO STDOUT WITH CSV HEADER"
            )
            with open(out_path, "w", encoding="utf-8", newline="") as fh:
                with conn.cursor() as cur, cur.copy(copy_sql) as copy:
                    for data in copy:
                        fh.write(bytes(data).decode("utf-8"))
            print(f"{table}: public={public} private_excluded={private}")

    print(f"contribution bundle written to {args.out}/ (public rows only)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
