#!/usr/bin/env python3
"""CLI to take/refresh/release/check a live-session claim on a bus agent.

Thin wrapper over hub_store's existing claim/release/is_claimed. The interactive
session runs on the host and writes the claim file under <repo>/data/claims,
which the worker container reads via the /app/data bind-mount.
"""
from __future__ import annotations

import argparse
import os
import socket
import sys

# Default the claims dir to <repo>/data/claims BEFORE importing hub_store, which
# reads HUB_CLAIMS_DIR at import time.
_REPO = os.path.dirname(os.path.abspath(__file__))
os.environ.setdefault("HUB_CLAIMS_DIR", os.path.join(_REPO, "data", "claims"))

import hub_store  # noqa: E402
import local_defaults  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Live-session agent claim")
    p.add_argument("action", choices=["claim", "release", "status"])
    p.add_argument("agent")
    p.add_argument("--tenant", default=local_defaults.get("HUB_CLAIM_TENANT", "default"))
    args = p.parse_args(argv)

    if args.action == "claim":
        hub_store.claim(args.tenant, args.agent, host=os.environ.get("HUB_CLAIM_HOST") or socket.gethostname())
        return 0
    if args.action == "release":
        hub_store.release(args.tenant, args.agent)
        return 0
    return 0 if hub_store.is_claimed(args.tenant, args.agent) else 1


if __name__ == "__main__":
    sys.exit(main())
