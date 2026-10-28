# a2a_client.py
"""A2A (Agent2Agent) JSON-RPC client for the Network bus.

Talks the A2A standard surface exposed by Network at POST /a2a/agents/{name}:
message/send, message/stream, tasks/get, tasks/cancel, plus Agent Card
discovery. Additive sibling of hub_client.HubClient (which speaks the bus's
custom REST API); both share the same bus credentials and build their auth
headers through hub_client.auth_headers (per-agent X-Agent-Key when configured,
shared tenant X-Api-Key as the logged legacy fallback).

A2A vocabulary mapping (server side, api/a2a on Network):
  Task        <-> conversation   (task.id == task.contextId == conversation id)
  Message     <-> message        (role user = caller, agent = anyone else)
  TextPart    <-> message body ; DataPart <-> message context
  task state working/completed/canceled <-> conversation open/resolved/canceled
"""
from __future__ import annotations

import json
import logging
from typing import AsyncIterator

import httpx

import hub_client  # reuse parse_sse for the streaming variant + auth_headers

logger = logging.getLogger("coordinator.a2a_client")


class A2AError(RuntimeError):
    """A JSON-RPC error returned by the A2A endpoint (code + message)."""

    def __init__(self, code: int, message: str, data: object = None):
        super().__init__(f"A2A error {code}: {message}")
        self.code = code
        self.message = message
        self.data = data


def _text_message(text: str, *, task_id: int | None, data: dict | None,
                  subject: str | None, priority: str | None) -> dict:
    """Build an A2A Message object (camelCase, as the server expects)."""
    parts: list[dict] = [{"kind": "text", "text": text}]
    if data:
        parts.append({"kind": "data", "data": data})
    msg: dict = {"role": "user", "parts": parts, "messageId": _new_id()}
    if task_id is not None:
        msg["taskId"] = str(task_id)
        msg["contextId"] = str(task_id)
    meta: dict = {}
    if subject:
        meta["subject"] = subject
    if priority:
        meta["priority"] = priority
    if meta:
        msg["metadata"] = meta
    return msg


_COUNTER = {"n": 0}


def _new_id() -> str:
    """Monotonic, process-local id for JSON-RPC envelopes / message ids.

    Avoids importing uuid/time for what only needs to be unique within a
    request; the server echoes the JSON-RPC id back for correlation.
    """
    _COUNTER["n"] += 1
    return f"co-{_COUNTER['n']}"


class A2AClient:
    def __init__(
        self, base_url: str, api_key: str, agent_id: str, agent_key: str = ""
    ):
        self.base_url = base_url.rstrip("/")
        self.headers = hub_client.auth_headers(api_key, agent_id, agent_key)

    def _url(self, target: str) -> str:
        return f"{self.base_url}/a2a/agents/{target}"

    async def _rpc(self, target: str, method: str, params: dict) -> dict:
        """POST one JSON-RPC call to agent `target`; return `result` or raise."""
        payload = {"jsonrpc": "2.0", "id": _new_id(), "method": method, "params": params}
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(self._url(target), headers=self.headers, json=payload)
            r.raise_for_status()
            body = r.json()
        if "error" in body and body["error"] is not None:
            err = body["error"]
            raise A2AError(err.get("code", -32603), err.get("message", "error"),
                           err.get("data"))
        return body.get("result", {})

    # --- task interface --------------------------------------------------
    async def send_message(
        self, target: str, text: str, *, task_id: int | None = None,
        data: dict | None = None, subject: str | None = None,
        priority: str | None = None,
    ) -> dict:
        """message/send. No task_id -> start a new task addressed to `target`;
        task_id present -> append to that existing task. Returns the Task object."""
        msg = _text_message(text, task_id=task_id, data=data,
                            subject=subject, priority=priority)
        return await self._rpc(target, "message/send", {"message": msg})

    async def get_task(self, target: str, task_id: int) -> dict:
        """tasks/get. `target` must be a participant agent of the task."""
        return await self._rpc(target, "tasks/get", {"id": str(task_id)})

    async def cancel_task(self, target: str, task_id: int) -> dict:
        """tasks/cancel. Maps the conversation to status 'canceled'."""
        return await self._rpc(target, "tasks/cancel", {"id": str(task_id)})

    # --- push notification config ---------------------------------------
    async def set_push_config(
        self, target: str, url: str, *, token: str | None = None,
        config_id: str | None = None, task_id: int | None = None,
    ) -> dict:
        """tasks/pushNotificationConfig/set. task_id omitted -> agent-wide
        (global inbox). The config is stored for the calling agent; `target`
        only needs to resolve (use the caller's own name)."""
        cfg: dict = {"url": url}
        if config_id:
            cfg["id"] = config_id
        if token:
            cfg["token"] = token
        params: dict = {"pushNotificationConfig": cfg}
        if task_id is not None:
            params["taskId"] = str(task_id)
        return await self._rpc(target, "tasks/pushNotificationConfig/set", params)

    async def delete_push_config(self, target: str, config_id: str) -> dict:
        return await self._rpc(
            target, "tasks/pushNotificationConfig/delete", {"configId": config_id}
        )

    # --- discovery -------------------------------------------------------
    async def get_agent_card(self, target: str) -> dict:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.get(
                f"{self._url(target)}/.well-known/agent-card.json", headers=self.headers
            )
            r.raise_for_status()
            return r.json()

    async def list_agent_cards(self) -> list[dict]:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.get(f"{self.base_url}/a2a/agents", headers=self.headers)
            r.raise_for_status()
            return r.json().get("agents", [])

    # --- streaming (optional) -------------------------------------------
    async def stream_message(
        self, target: str, text: str, *, task_id: int | None = None,
        data: dict | None = None, subject: str | None = None,
        priority: str | None = None,
    ) -> AsyncIterator[dict]:
        """message/stream: send then yield JSON-RPC results (initial Task, then
        TaskStatusUpdateEvent objects) until a `final` update arrives."""
        msg = _text_message(text, task_id=task_id, data=data,
                            subject=subject, priority=priority)
        payload = {"jsonrpc": "2.0", "id": _new_id(), "method": "message/stream",
                   "params": {"message": msg}}
        timeout = httpx.Timeout(connect=10.0, read=90.0, write=30.0, pool=30.0)
        async with httpx.AsyncClient(timeout=timeout) as client:
            async with client.stream(
                "POST", self._url(target), headers=self.headers, json=payload
            ) as resp:
                resp.raise_for_status()
                async for _evt, data_dict, _eid in hub_client.parse_sse(resp.aiter_lines()):
                    result = data_dict.get("result", data_dict)
                    yield result
                    status = (result or {}).get("status") or {}
                    if result.get("final") or status.get("state") in (
                        "completed", "canceled", "failed", "rejected"
                    ):
                        break
