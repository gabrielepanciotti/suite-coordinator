# instance_link.py
"""Link a Life instance's Telegram group from the bot (#82).

The bot sees the ``chat_id`` when it is added to a group; the person only picks
"istanza <id>" in the menu. Writing ``channels.telegram`` and pushing it happen on
the host (``scripts/host_instance_link.py`` behind the host-exec key): the
instance checkouts are read-only in the container, and its git token cannot see
the personal repos.

Two halves, like the rest of the bind flow: :func:`unlinked` is what the menu
offers (pure over the registry), :func:`link_telegram` does the write.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass

import instances

logger = logging.getLogger("coordinator.instance_link")

KEY_PREFIX = "i:"
"""Menu key of an instance, next to the project keys: ``gb:i:<id>``."""

TIMEOUT_S = 90


@dataclass(frozen=True)
class LinkResult:
    ok: bool
    written: bool
    pushed: bool
    detail: str


def unlinked(env=None) -> list[str]:
    """Ids of the registered instances whose ``channels.telegram`` has no group."""
    try:
        registry, _ = instances.load_registry(env)
    except instances.InstanceError as exc:
        logger.warning("registro istanze illeggibile: %s", exc)
        return []
    return sorted(
        iid
        for iid, inst in registry.items()
        if instances.channels_of(inst, env=env).telegram_chat_id is None
    )


def menu_entries(env=None) -> list[tuple[str, str]]:
    """``(key, label)`` for the bind menu, one per unlinked instance."""
    return [(f"{KEY_PREFIX}{iid}", f"\U0001f9e9 istanza {iid}") for iid in unlinked(env)]


def telegram_agent(env=None) -> str:
    env = os.environ if env is None else env
    return (env.get("HUB_TELEGRAM_AGENT") or "telegram-owner").strip()


def request(instance_id: str, chat_id: int, agent: str) -> bytes:
    return (
        json.dumps(
            {"op": "instance-telegram", "instance": instance_id, "chatId": chat_id,
             "agent": agent}
        )
        + "\n"
    ).encode()


def parse_reply(stdout: bytes, stderr: bytes, rc: int | None) -> LinkResult:
    for line in reversed(stdout.decode(errors="replace").splitlines()):
        try:
            data = json.loads(line)
        except ValueError:
            continue
        if isinstance(data, dict) and "ok" in data:
            return LinkResult(bool(data["ok"]), bool(data.get("written")),
                              bool(data.get("pushed")), str(data.get("detail", "")))
    err = stderr.decode(errors="replace").strip()[-300:]
    return LinkResult(False, False, False, f"nessuna risposta dall'host (rc={rc}): {err}")


async def _ask_host(payload: bytes, what: str) -> LinkResult:
    """One request to the host-exec key, one JSON line back. Never raises."""
    import claude_runner

    if not claude_runner.host_exec_enabled():
        return LinkResult(
            False, False, False,
            f"HOST_EXEC_SSH non configurato su questo device: {what} va scritto "
            "sull'host (scripts/host_instance_link.py)",
        )
    try:
        proc = await asyncio.create_subprocess_exec(
            *claude_runner.host_exec_command(),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await asyncio.wait_for(proc.communicate(payload), TIMEOUT_S)
    except (OSError, asyncio.TimeoutError) as exc:
        return LinkResult(False, False, False, f"ssh verso l'host fallita: {exc}")
    return parse_reply(out, err, proc.returncode)


async def link_telegram(instance_id: str, chat_id: int, agent: str) -> LinkResult:
    """Ask the host to write ``{chatId, agent}`` and push it. Never raises."""
    return await _ask_host(request(instance_id, chat_id, agent), "il canale")


def idea_request(instance_id: str, nodo: str, testo: str, session: str, marker: str) -> bytes:
    return (
        json.dumps(
            {"op": "instance-idea", "instance": instance_id, "nodo": nodo,
             "testo": testo, "session": session, "marker": marker}
        )
        + "\n"
    ).encode()


async def queue_idea(instance_id: str, nodo: str, testo: str, session: str,
                     marker: str) -> LinkResult:
    """Append a row to the brain queue of ``instance_id`` on the host (#78).

    The queue belongs to the instance the chat belongs to, not to the one
    instance a bot happened to have mounted. ``written`` without ``pushed`` is
    still a success for the person: the row leaves with the next push.
    """
    return await _ask_host(idea_request(instance_id, nodo, testo, session, marker),
                           "la coda del brain")
