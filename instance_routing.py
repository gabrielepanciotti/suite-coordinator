# instance_routing.py
"""Which Life instance a chat binding belongs to (#83, F1).

A binding still says ``(chat, topic) -> (project, agent)``; this module answers the
floor above it — *which instance* — so the gate of ``instance_acl`` can run before a
message reaches a workspace. The answer is derived, not stored, in this order:

1. an **explicit** choice made from the bind menu (only stored when it was needed:
   a node that lives in more than one instance, e.g. an infra entity);
2. the **instance forum**: a chat declared in ``channels.telegram`` of an instance;
3. the **graph**: the instance whose graph hosts the bound node on this device
   (``bus_agents`` — Life's own computation, never a hand-written list);
4. nothing: ``None``, and the per-project checks of today decide alone.

A node found in two instances with no explicit choice is ambiguous and also
yields ``None`` — never a guess. Pure over plain data; the bot feeds it.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping

from group_register import NESSUNO, PER_PAGINA, PREFIX, Bottone

OPEN_PREFIX = "in:"
"""Menu key opening an instance's node list: ``gb:in:<id>``."""
NODE_PREFIX = "n:"
"""A node chosen inside an instance: ``gb:n:<id>:<project key>``."""
PAGE_PREFIX = "ip:"
"""Page of an instance's node list: ``gb:ip:<id>:<n>``."""


def node_index(agents: Iterable) -> dict[str, tuple[str, ...]]:
    """``graph id (lower) -> instances`` from the derived bus agents of this device."""
    out: dict[str, list[str]] = {}
    for a in agents or ():
        ids = out.setdefault(a.graph_id.lower(), [])
        if a.instance_id not in ids:
            ids.append(a.instance_id)
    return {k: tuple(sorted(v)) for k, v in out.items()}


def project_node(project) -> str:
    """The graph id a project stands for: its slug, else its key (lower)."""
    return (getattr(project, "slug", "") or project.key).lower()


def instances_of_node(node: str, nodes: Mapping[str, tuple[str, ...]]) -> tuple[str, ...]:
    return nodes.get(node.lower(), ())


def instance_of_binding(
    chat_id: int,
    node: str | None,
    *,
    explicit: str | None,
    chat_owner: str | None,
    nodes: Mapping[str, tuple[str, ...]],
) -> str | None:
    """The instance a binding belongs to, or ``None`` (undeclared or ambiguous)."""
    if explicit:
        return explicit
    if chat_owner:
        return chat_owner
    if node:
        found = instances_of_node(node, nodes)
        if len(found) == 1:
            return found[0]
    return None


def needs_explicit(node: str, nodes: Mapping[str, tuple[str, ...]]) -> bool:
    """Whether binding ``node`` must record the chosen instance (node in 2+ instances)."""
    return len(instances_of_node(node, nodes)) > 1


def group_by_instance(
    projects: Iterable[tuple[str, str, str]], nodes: Mapping[str, tuple[str, ...]]
) -> tuple[dict[str, list[tuple[str, str]]], list[tuple[str, str]]]:
    """Split ``(key, label, node)`` into ``{instance: [(key, label)]}`` and the unmapped.

    A node living in several instances appears under each of them: the choice of
    the instance is the choice the menu exists to ask.
    """
    by: dict[str, list[tuple[str, str]]] = {}
    unmapped: list[tuple[str, str]] = []
    for key, label, node in projects:
        found = instances_of_node(node, nodes)
        if not found:
            unmapped.append((key, label))
        for iid in found:
            by.setdefault(iid, []).append((key, label))
    return by, unmapped


def read_choice(dato: str) -> tuple[str, str] | None:
    """The two-level callbacks: ("apri_istanza", id) | ("pagina_istanza", "id:n")."""
    if not dato.startswith(f"{PREFIX}:"):
        return None
    resto = dato[len(PREFIX) + 1 :]
    if resto.startswith(OPEN_PREFIX):
        return ("apri_istanza", resto[len(OPEN_PREFIX):])
    if resto.startswith(PAGE_PREFIX):
        return ("pagina_istanza", resto[len(PAGE_PREFIX):])
    if resto.startswith(NODE_PREFIX):
        return ("nodo", resto[len(NODE_PREFIX):])
    return None


def node_page(
    instance_id: str,
    entries: list[tuple[str, str]],
    indice: int = 0,
    *,
    channel_key: str | None = None,
    per_pagina: int = PER_PAGINA,
) -> list[list[Bottone]]:
    """The node list of one instance: two per row, own pagination, a way back.

    ``entries`` are ``(menu key, label)`` like ``group_register.pagina``: a project
    key binds the group to that node. ``channel_key`` (``i:<id>``), when given, adds
    «this group is the instance's forum» on top.
    """
    totale = max(1, -(-len(entries) // per_pagina))
    indice = max(0, min(indice, totale - 1))
    fetta = entries[indice * per_pagina : (indice + 1) * per_pagina]
    righe: list[list[Bottone]] = []
    if channel_key:
        righe.append([Bottone(f"\U0001f9e9 Forum dell'istanza {instance_id}",
                              f"{PREFIX}:{channel_key}")])
    for i in range(0, len(fetta), 2):
        righe.append([Bottone(label, f"{PREFIX}:{key}") for key, label in fetta[i : i + 2]])
    if totale > 1:
        nav: list[Bottone] = []
        if indice > 0:
            nav.append(Bottone("◀", f"{PREFIX}:{PAGE_PREFIX}{instance_id}:{indice - 1}"))
        nav.append(Bottone(f"{indice + 1}/{totale}",
                           f"{PREFIX}:{PAGE_PREFIX}{instance_id}:{indice}"))
        if indice < totale - 1:
            nav.append(Bottone("▶", f"{PREFIX}:{PAGE_PREFIX}{instance_id}:{indice + 1}"))
        righe.append(nav)
    righe.append([Bottone("⬅ Istanze", f"{PREFIX}:p:0"), Bottone("⏭ Nessuno", NESSUNO)])
    return righe


def node_entries(instance_id: str, entries: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """``(key, label)`` of an instance's nodes as menu keys carrying the instance."""
    return [(f"{NODE_PREFIX}{instance_id}:{key}", label) for key, label in entries]


def split_node(value: str) -> tuple[str, str] | None:
    """``"<id>:<project key>"`` -> ``(id, key)``; ``None`` if malformed."""
    iid, sep, key = value.partition(":")
    return (iid, key) if sep and iid and key else None


def instance_entries(instance_ids: Iterable[str]) -> list[tuple[str, str]]:
    """Top-level menu entries, one per instance: ``(in:<id>, 🏛 <id>)``."""
    return [(f"{OPEN_PREFIX}{iid}", f"\U0001f3db {iid}") for iid in sorted(instance_ids)]
