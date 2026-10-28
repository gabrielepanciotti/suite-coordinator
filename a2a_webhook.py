# a2a_webhook.py
"""A2A push-notification receiver for the Coordinator.

Network POSTs task updates here when this node registers an agent-level push
config (the global inbox). We validate the shared token, then hand the event to
the same dispatcher the SSE listener uses — scheduled as a background task so
the HTTP response returns immediately (a wake can take minutes; Network's
webhook timeout is seconds). Mounted on api.app (port :8018). Imports hub, never
bot (acyclic).
"""
from __future__ import annotations

import asyncio
import logging
import secrets

from fastapi import APIRouter, Header, HTTPException, Request

import hub

logger = logging.getLogger("coordinator.a2a_webhook")
router = APIRouter()

# Strong refs to in-flight dispatch tasks (else create_task results may be GC'd).
_tasks: set = set()


def _token_ok(expected: str, got: str | None) -> bool:
    if not expected:
        return True  # no token configured -> accept (trusted LAN deployment)
    return bool(got) and secrets.compare_digest(expected, got)


@router.post("/a2a/webhook/{tenant}/{agent}")
async def a2a_webhook(
    tenant: str,
    agent: str,
    request: Request,
    x_a2a_notification_token: str | None = Header(default=None),
):
    cfg = hub._S.get("config")
    if not cfg or not cfg.a2a_webhook_enabled:
        raise HTTPException(status_code=404, detail="A2A webhook disabled")
    if not _token_ok(cfg.a2a_webhook_token, x_a2a_notification_token):
        raise HTTPException(status_code=401, detail="bad notification token")
    try:
        evt = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="invalid JSON")

    task = asyncio.create_task(_safe_dispatch(tenant, agent, evt))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return {"ok": True}


async def _safe_dispatch(tenant: str, agent: str, evt: dict) -> None:
    try:
        await hub.dispatch_webhook_event(tenant, agent, evt)
    except Exception:
        logger.exception("a2a webhook dispatch failed for %s/%s", tenant, agent)
