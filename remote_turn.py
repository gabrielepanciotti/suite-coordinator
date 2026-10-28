# remote_turn.py
"""The poller is not the executor (#83, F3).

One device polls Telegram for every instance; a node's work runs where its repo is.
When the node a chat is bound to is not hosted on the polling device, the message is
handed to the node's own identity on the bus of its instance, and the reply comes back
into the chat. The executor needs nothing new: it is the ordinary bus wake that every
node already has (a session in the real repo, on its device, with its credentials).

Who executes is read from the bus, not written here: an agent ``<graph id>-<suffix>``
registered on the instance's tenant is, by construction, that node on that device
(Life derives the names, ``suite_core.instances.identity``). The order among several
executors is the instance's ladder (``executors`` in ``instance.config.json``, written
with device ids and translated to bus suffixes by :func:`ladder_suffixes`); without
one, an executor that is listening beats one that is not.

The bus is also the queue: a conversation addressed to an executor that is offline
stays unread and wakes it when it reconnects. Nothing here holds a message in memory.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class Executor:
    name: str
    device: str
    listening: bool


def ladder_suffixes(executors: Iterable, devices: Mapping | None = None) -> tuple[str, ...]:
    """``executors`` (device ids) as bus suffixes, in order.

    The bus does not always name a device by its id: the instance's catalogue says the
    bus suffix in ``devices[id].busSuffix``; absent, the suffix is the id (conv 720).
    Without this, a device whose two names differ was never ranked in the ladder.
    """
    out: list[str] = []
    for d in executors or ():
        if not isinstance(d, str) or not d.strip():
            continue
        entry = (devices or {}).get(d)
        suffix = entry.get("busSuffix") if isinstance(entry, Mapping) else None
        suffix = suffix.strip() if isinstance(suffix, str) and suffix.strip() else d
        if suffix not in out:
            out.append(suffix)
    return tuple(out)


def candidates(
    graph_id: str, agents: Iterable[Mapping], *, local_suffix: str, tenant: str | None = None
) -> list[Executor]:
    """The bus identities that are ``graph_id`` on a device other than this one.

    ``tenant`` filters on the agent's own tenant: an identity of another instance never
    receives this instance's text, whatever the listing endpoint chose to return (the
    bus answers with the caller's tenant whatever ``?tenant=`` says).
    """
    want = graph_id.lower()
    out = []
    for a in agents or ():
        name, device = str(a.get("name") or ""), str(a.get("device") or "")
        if a.get("is_active") is False or not name or not device:
            continue
        if tenant and a.get("tenant") and a.get("tenant") != tenant:
            continue
        if device == local_suffix:
            continue
        if name.lower() != f"{want}-{device.lower()}":
            continue
        out.append(Executor(name, device, bool(a.get("listening"))))
    return out


def pick(
    graph_id: str,
    agents: Iterable[Mapping],
    *,
    local_suffix: str,
    ladder: Sequence[str] = (),
    tenant: str | None = None,
) -> Executor | None:
    """The executor of ``graph_id``: ladder order, then listening, then name."""
    found = candidates(graph_id, agents, local_suffix=local_suffix, tenant=tenant)
    if not found:
        return None
    rank = {d.lower(): i for i, d in enumerate(ladder)}

    def key(e: Executor):
        return (not e.listening, rank.get(e.device.lower(), len(rank)), e.name)

    if ladder:
        # The ladder decides among live executors; an offline first step does not
        # beat a live second one (that is what a ladder is for).
        return min(found, key=key)
    return min(found, key=lambda e: (not e.listening, e.name))


def outranks(executor: Executor | None, local_suffix: str, ladder: Sequence[str]) -> bool:
    """Should a node hosted HERE still go to ``executor``? Only when it is live and the
    ladder puts its device above this one. Without a ladder a local node stays local.
    """
    if executor is None or not executor.listening:
        return False
    rank = {d.lower(): i for i, d in enumerate(ladder)}
    mine = rank.get(local_suffix.lower(), len(rank))
    return rank.get(executor.device.lower(), len(rank)) < mine


def reply_text(from_agent: str, body: str, *, limit: int = 3900) -> str:
    """What lands in the chat: who answered, then the answer (Telegram caps at 4096)."""
    body = (body or "").strip()
    if len(body) > limit:
        body = body[:limit].rstrip() + "…"
    return f"\U0001f6f0 {from_agent}\n{body}"
