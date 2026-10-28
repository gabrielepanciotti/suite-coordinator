# scheduler_client.py
"""HTTP client for the Scheduler Agent API (node -> person Proposals).

A Life node never writes directly into Scheduler's plan. It creates a *Proposal*
via ``POST /api/agent/proposals``; the person confirms/rejects it (Telegram DM or
web) before it materializes. Two identity headers on every request:

  X-Agent-Secret : shared server-to-server secret (``AGENT_API_SECRET``).
  X-Agent-Origin : ``node:<slug>`` (default ``node:coordinator``). Direct-write
                   endpoints answer 403 to ``node:*`` origins — proposals are the
                   only path in.

Contract: Scheduler/.claude/docs/agent-api-contract.md (Coordinator = issue #17).
Auth/HTTP style mirrors hub_client.HubClient (httpx.AsyncClient, per-call).
"""

from __future__ import annotations

import logging
import os
import re
from urllib.parse import urlencode

import httpx

logger = logging.getLogger("coordinator.scheduler_client")

DEFAULT_TIMEOUT = 15.0
VALID_KINDS = ("schedulable", "fixed_event", "preference")
_HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")  # 00:00–23:59
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")  # YYYY-MM-DD (shape only)
_ORIGIN = re.compile(r"^(user|node:[a-z0-9-]+)$")
_NODE_SLUG = re.compile(r"^[a-z0-9-]+$")  # must match Scheduler server gate (lowercase only)


class SchedulerError(Exception):
    """Base for every Scheduler Agent API failure."""


class SchedulerNotConfigured(SchedulerError):
    """SCHEDULER_API_URL / AGENT_API_SECRET missing from the environment."""


class SchedulerAuthError(SchedulerError):
    """401 — wrong shared secret."""


class SchedulerForbidden(SchedulerError):
    """403 — origin not ``node:*`` or target person inactive/inexistent."""


class SchedulerNotFound(SchedulerError):
    """404 — endpoint or person not found."""


class SchedulerConflict(SchedulerError):
    """409 — proposal already decided (idempotent replay)."""


class SchedulerValidationError(SchedulerError):
    """422 — invalid payload or ambiguous target identification."""


_STATUS_EXC = {
    401: SchedulerAuthError,
    403: SchedulerForbidden,
    404: SchedulerNotFound,
    409: SchedulerConflict,
    422: SchedulerValidationError,
}


def _detail(resp: httpx.Response) -> str:
    """Best-effort human-readable error detail from a response body."""
    try:
        data = resp.json()
    except Exception:
        return (resp.text or "").strip()[:200]
    if isinstance(data, dict):
        return str(data.get("detail") or data.get("message") or data)[:200]
    return str(data)[:200]


class SchedulerClient:
    def __init__(
        self,
        base_url: str,
        secret: str,
        origin: str = "node:coordinator",
        timeout: float = DEFAULT_TIMEOUT,
    ):
        if not base_url or not secret:
            raise SchedulerNotConfigured("SCHEDULER_API_URL / AGENT_API_SECRET not set")
        if not _ORIGIN.match(origin):
            raise SchedulerValidationError(f"invalid origin {origin!r}")
        self.base_url = base_url.rstrip("/")
        self.origin = origin
        self.headers = {"X-Agent-Secret": secret, "X-Agent-Origin": origin}
        self.timeout = timeout

    async def create_proposal(
        self,
        *,
        kind: str,
        summary: str,
        payload: dict,
        telegram_chat_id: int | None = None,
        user_id: int | None = None,
        node_slug: str | None = None,
    ) -> dict:
        """Create a node->person Proposal. Returns the ProposalRead dict (201).

        Exactly one of ``telegram_chat_id`` / ``user_id`` identifies the person.
        ``node_slug`` (taskgen callers) overrides the request's ``X-Agent-Origin``
        to ``node:{node_slug}`` for this call only, leaving ``self.origin``
        (and every other call made from this client instance) untouched — the
        client is shared across watchers with different node identities.
        Raises a typed SchedulerError subclass on any non-2xx response.
        """
        if kind not in VALID_KINDS:
            raise SchedulerValidationError(f"unknown kind {kind!r}")
        if (telegram_chat_id is None) == (user_id is None):
            raise SchedulerValidationError(
                "exactly one of telegram_chat_id / user_id is required"
            )
        summary = (summary or "").strip()
        if not (1 <= len(summary) <= 200):
            raise SchedulerValidationError("summary must be 1–200 chars")

        body: dict = {"kind": kind, "summary": summary, "payload": payload}
        if telegram_chat_id is not None:
            body["telegram_chat_id"] = int(telegram_chat_id)
        else:
            body["user_id"] = int(user_id)

        headers = self.headers
        if node_slug is not None:
            if not _NODE_SLUG.match(node_slug or ""):
                raise SchedulerValidationError(f"invalid node_slug {node_slug!r}")
            headers = {**self.headers, "X-Agent-Origin": f"node:{node_slug}"}

        return await self._post("/api/agent/proposals", body, headers=headers)

    async def create_scheduled_task(
        self,
        *,
        telegram_chat_id: int,
        name: str,
        category_id: int | None,
        start_at: str,
        duration_min: int,
        description: str | None = None,
    ) -> dict:
        """Create a one-off ScheduledTask (taskgen "trusted" branch).

        POSTs with ``X-Agent-Origin: user`` regardless of ``self.origin`` — this
        endpoint acts on the person's own plan (``forbid_node_origin`` on the
        server side), unlike proposals. Returns ``{id, notification_state,
        start_at}``. Raises a typed SchedulerError subclass on any non-2xx.
        """
        if int(duration_min) < 1:
            raise SchedulerValidationError("duration_min must be >= 1")
        body = {
            "telegram_chat_id": int(telegram_chat_id),
            "name": name,
            "category_id": category_id,
            "start_at": start_at,
            "duration_min": int(duration_min),
            "description": description,
        }
        headers = {**self.headers, "X-Agent-Origin": "user"}
        return await self._post(
            "/api/agent/scheduled-tasks", body, headers=headers
        )

    async def delete_scheduled_task(self, *, st_id: int, telegram_chat_id: int) -> None:
        """Cancel a one-off ScheduledTask created via ``create_scheduled_task``.

        DELETEs with ``X-Agent-Origin: user``. Raises a typed SchedulerError
        subclass on any non-2xx response.
        """
        headers = {**self.headers, "X-Agent-Origin": "user"}
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.delete(
                    f"{self.base_url}/api/agent/scheduled-tasks/{st_id}",
                    headers=headers,
                    params={"telegram_chat_id": int(telegram_chat_id)},
                )
        except httpx.HTTPError as exc:
            raise SchedulerError(f"Scheduler unreachable: {exc}") from exc
        if resp.status_code == 204:
            return
        exc = _STATUS_EXC.get(resp.status_code, SchedulerError)
        raise exc(f"Scheduler {resp.status_code}: {_detail(resp)}")

    async def _post(self, path: str, body: dict, headers: dict | None = None) -> dict:
        """Shared POST + status-handling helper for every Agent API write.

        Wraps transport failures (connection refused, timeout, DNS) into
        ``SchedulerError`` so callers only ever handle typed SchedulerError
        subclasses, never raw httpx, and maps non-2xx status codes via
        ``_STATUS_EXC``. ``headers`` defaults to ``self.headers`` (fixed
        instance origin); callers needing a per-call origin override pass
        their own dict (see ``create_proposal``/``create_scheduled_task``).
        """
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.post(
                    f"{self.base_url}{path}",
                    headers=headers if headers is not None else self.headers,
                    json=body,
                )
        except httpx.HTTPError as exc:
            raise SchedulerError(f"Scheduler unreachable: {exc}") from exc
        if resp.status_code in (200, 201):
            return resp.json()
        exc = _STATUS_EXC.get(resp.status_code, SchedulerError)
        raise exc(f"Scheduler {resp.status_code}: {_detail(resp)}")

    async def _get(self, path: str, headers: dict | None = None) -> object:
        """Shared GET + status-handling helper, mirroring ``_post``."""
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.get(
                    f"{self.base_url}{path}",
                    headers=headers if headers is not None else self.headers,
                )
        except httpx.HTTPError as exc:
            raise SchedulerError(f"Scheduler unreachable: {exc}") from exc
        if resp.status_code in (200, 201):
            return resp.json()
        exc = _STATUS_EXC.get(resp.status_code, SchedulerError)
        raise exc(f"Scheduler {resp.status_code}: {_detail(resp)}")

    async def list_categories(self) -> list[dict]:
        """Fetch active categories (id/name/display_name/...). Read-only."""
        headers = {**self.headers, "X-Agent-Origin": "user"}
        data = await self._get("/api/agent/categories", headers=headers)
        return list(data) if isinstance(data, list) else []

    async def list_scheduled_tasks(
        self, *, telegram_chat_id: int, frm: str, to: str
    ) -> list[dict]:
        """Fetch ScheduledTasks in a [frm, to) window. Read-only, ``X-Agent-Origin: user``."""
        headers = {**self.headers, "X-Agent-Origin": "user"}
        qs = urlencode({"from": frm, "to": to, "telegram_chat_id": int(telegram_chat_id)})
        data = await self._get(f"/api/agent/scheduled-tasks?{qs}", headers=headers)
        return list(data) if isinstance(data, list) else []

    async def reschedule_task(
        self, *, st_id: int, telegram_chat_id: int, start_at: str
    ) -> dict:
        """Reschedule a ScheduledTask to a new ``start_at``. ``X-Agent-Origin: user``."""
        headers = {**self.headers, "X-Agent-Origin": "user"}
        body = {"telegram_chat_id": int(telegram_chat_id), "start_at": start_at}
        return await self._post(
            f"/api/agent/scheduled-tasks/{st_id}/reschedule", body, headers=headers
        )

    async def start_task(self, st_id: int, telegram_chat_id: int) -> dict:
        """Mark a ScheduledTask as started. Returns ``{id, notification_state}``."""
        return await self._post(
            f"/api/agent/scheduled-tasks/{st_id}/start",
            {"telegram_chat_id": int(telegram_chat_id)},
        )

    async def complete_task(self, st_id: int, telegram_chat_id: int) -> dict:
        """Mark a ScheduledTask as completed. Returns ``{id, notification_state}``."""
        return await self._post(
            f"/api/agent/scheduled-tasks/{st_id}/complete",
            {"telegram_chat_id": int(telegram_chat_id)},
        )

    async def postpone_task(
        self, st_id: int, telegram_chat_id: int, minutes: int | None = None
    ) -> dict:
        """Postpone a ScheduledTask. Returns ``{id, notification_state, start_at}``.

        ``minutes`` is included in the body only when not None (server picks a
        default postpone window otherwise).
        """
        body: dict = {"telegram_chat_id": int(telegram_chat_id)}
        if minutes is not None:
            body["minutes"] = int(minutes)
        return await self._post(f"/api/agent/scheduled-tasks/{st_id}/postpone", body)


def client_from_env(origin: str | None = None) -> SchedulerClient:
    """Build a client from the process environment.

    Reads env at call time (not import) so tests can set/patch it and the bot
    picks up a recreate without stale module-level constants. Raises
    SchedulerNotConfigured if the URL or secret is missing.

    ``origin`` precedence: an EXPLICITLY passed value always wins (e.g.
    lifecycle callers acting on the person's behalf pass ``origin="user"``
    and must not be overridden by node-scoped env config). Only when the
    caller omits it (``origin is None`` — proposal-creation callers, #17
    unchanged) do we fall back to the env var ``SCHEDULER_AGENT_ORIGIN``,
    defaulting to ``node:coordinator`` if that's unset too.
    """
    resolved_origin = (
        origin
        if origin is not None
        else os.environ.get("SCHEDULER_AGENT_ORIGIN", "node:coordinator")
    )
    return SchedulerClient(
        base_url=os.environ.get("SCHEDULER_API_URL", ""),
        secret=os.environ.get("AGENT_API_SECRET", ""),
        origin=resolved_origin,
    )


def build_proposal(kind: str, args: list[str]) -> tuple[str, dict]:
    """Turn a Telegram slash-command's positional args into (summary, payload).

    Pure and side-effect free — the single place that knows the compact
    command syntax for each slash-friendly kind. ``schedulable`` is intentionally
    not built here: its payload (category_id, schedule mode, ...) is richer than a
    slash command should carry, so it goes through the NL path or a direct client
    call. Raises ValueError with a usage hint on bad input.
    """
    args = [a for a in (args or []) if a.strip()]

    if kind == "preference":
        # rest_window: 0 args -> reset to global; 2 args -> HH:MM window.
        if not args:
            return "Azzera la finestra di riposo", {"target": "rest_window"}
        if len(args) == 2:
            rest_start, rest_end = args
            if not (_HHMM.match(rest_start) and _HHMM.match(rest_end)):
                raise ValueError("orari in formato HH:MM (es. 23:00 07:00)")
            return (
                f"Finestra di riposo {rest_start}–{rest_end}",
                {
                    "target": "rest_window",
                    "rest_start": rest_start,
                    "rest_end": rest_end,
                },
            )
        raise ValueError(
            "uso: <rest_start> <rest_end> (HH:MM) oppure nessun arg per azzerare"
        )

    if kind == "fixed_event":
        # AvailabilityOverride: <date> [start end] -> all-day / window busy block.
        if not args:
            raise ValueError("uso: <data YYYY-MM-DD> [inizio fine] (HH:MM)")
        date = args[0]
        if not _DATE.match(date):
            raise ValueError("data in formato YYYY-MM-DD")
        payload: dict = {"date": date, "type": "busy"}
        if len(args) == 1:
            return f"Blocco tutto il giorno il {date}", payload
        if len(args) == 3:
            start_time, end_time = args[1], args[2]
            if not (_HHMM.match(start_time) and _HHMM.match(end_time)):
                raise ValueError("orari in formato HH:MM")
            payload["start_time"] = start_time
            payload["end_time"] = end_time
            return f"Blocco {date} {start_time}–{end_time}", payload
        raise ValueError("uso: <data> oppure <data> <inizio> <fine>")

    raise ValueError(f"kind {kind!r} non costruibile via comando")
