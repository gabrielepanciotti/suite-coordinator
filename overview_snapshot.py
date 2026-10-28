# overview_snapshot.py
"""Snapshot builder + push for the Osservatorio integration (Opzione A, §3.3).

Collects scheduled-notification data from notif_store + APScheduler, POSTs a
snapshot to Network's /api/overview/schedule-snapshot, and applies any pause
directives returned in the response.

This module is pure-logic: no bot imports, no Telegram. The only I/O is:
  - notif_store.load()          (reads notifications.json)
  - scheduler._S["sched"]       (APScheduler instance, optional)
  - httpx.AsyncClient.post()    (HTTP push)
  - overview_store.*            (pause persistence)

References:
  spec: docs/specs/2026-06-10-ecosistema-notturno/31-osservatorio-spec.md §3.3
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import TYPE_CHECKING

import httpx

import notif_store
import overview_store
from hub_client import auth_headers

if TYPE_CHECKING:
    from apscheduler.schedulers.asyncio import AsyncIOScheduler

logger = logging.getLogger("coordinator.hub")

SNAPSHOT_ENABLED_ENV = "OVERVIEW_SNAPSHOT_ENABLED"
SNAPSHOT_INTERVAL_S = 15 * 60  # 15 minutes


def _snapshot_enabled() -> bool:
    val = os.environ.get(SNAPSHOT_ENABLED_ENV, "1").strip()
    return val not in ("0", "false", "no", "off")


def _project_of(notif) -> str:
    """Best-effort project key for a Notification.

    Manifest-originated notifications carry ``params["project"]``; seed/UI ones
    may not. Fall back to the part before the first '-' in the key (e.g.
    "personalchef-buongiorno" -> "personalchef", "personalChef-report" -> "personalChef").
    """
    project = (notif.params or {}).get("project", "")
    if project:
        return project
    # Derive from key: everything before the first '-'
    parts = notif.key.split("-", 1)
    return parts[0]


def _next_run_iso(sched: "AsyncIOScheduler | None", key: str) -> str | None:
    """Return next_run_time of the APScheduler job *key* as ISO8601 UTC, or None."""
    if sched is None:
        return None
    try:
        job = sched.get_job(key)
        if job is None:
            return None
        nrt = job.next_run_time
        if nrt is None:
            return None
        # APScheduler returns a timezone-aware datetime; normalise to UTC
        nrt_utc = nrt.astimezone(timezone.utc)
        return nrt_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
    except Exception:
        logger.debug("could not read next_run_time for job %s", key, exc_info=True)
        return None


def build_snapshot(sched: "AsyncIOScheduler | None" = None) -> dict:
    """Build the payload for POST /api/overview/schedule-snapshot.

    Returns::

        {
            "projects": [
                {
                    "project": "<key>",
                    "notifications": [
                        {
                            "key": str,
                            "label": str,
                            "next_run_at": ISO8601|null,
                            "paused": bool
                        }
                    ]
                }
            ]
        }
    """
    notifications = notif_store.load()

    # Group by project
    by_project: dict[str, list[dict]] = {}
    for notif in notifications:
        project = _project_of(notif)
        entry: dict = {
            "key": notif.key,
            "label": notif.title,
            "next_run_at": _next_run_iso(sched, notif.key),
            "paused": not notif.enabled,
        }
        by_project.setdefault(project, []).append(entry)

    projects_list = [
        {"project": project, "notifications": notifs}
        for project, notifs in sorted(by_project.items())
    ]
    return {"projects": projects_list}


async def push_snapshot(
    hub_url: str,
    api_key: str,
    agent_id: str,
    sched: "AsyncIOScheduler | None" = None,
    agent_key: str = "",
) -> bool:
    """POST the current snapshot to Network and apply any pause directives.

    Returns True on success. On any HTTP / connection error, logs a warning and
    returns False — never raises (fail-safe contract).
    """
    if not _snapshot_enabled():
        return True  # disabled: no-op, not an error

    payload = build_snapshot(sched)
    # This endpoint used to be the one place a per-agent key could not reach: it
    # depended on the bus's get_tenant_context, which read ONLY a tenant key, so
    # sending an agent key here 422'd every 15 minutes. The bus replaced that
    # dependency with get_tenant_scope (Network 1c54bfa), which resolves the
    # tenant from whichever credential it is given, so the exception is over —
    # auth_headers() now covers this call like every other one, and falls back
    # to the tenant key (loudly) only where no agent key is configured yet.
    if not api_key and not agent_key:
        logger.warning(
            "overview snapshot: skipped — no credential configured for agent "
            "'%s' (neither a per-agent key nor a tenant key).",
            agent_id,
        )
        return False
    headers = auth_headers(api_key, agent_id, agent_key)
    url = f"{hub_url.rstrip('/')}/api/overview/schedule-snapshot"

    try:
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.post(url, headers=headers, json=payload)
            resp.raise_for_status()
            data = resp.json()
    except httpx.HTTPStatusError as exc:
        logger.warning(
            "overview snapshot: HTTP %s from %s — will retry later",
            exc.response.status_code,
            url,
        )
        return False
    except Exception as exc:
        logger.warning("overview snapshot: push failed (%s) — will retry later", exc)
        return False

    # Apply pause directives from the response
    projects_resp = data.get("projects", []) if isinstance(data, dict) else []
    if projects_resp:
        try:
            overview_store.apply_pauses(projects_resp)
        except Exception:
            logger.exception("overview snapshot: apply_pauses failed")

    logger.debug("overview snapshot pushed (%d projects)", len(payload["projects"]))
    return True
