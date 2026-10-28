# node_menu.py
"""Menu navigation over Life's node graph.

The seven dedicated Telegram surfaces (Coordinator, Chef, Travel, Home, Health,
Scheduler, Mycorise) are the only bots that exist. Every other node is reached
by *navigating* inside one of them: entering a cluster offers its children, and
picking a leaf opens a session in that node's own workspace.

This module is the pure, offline half of that: given the node graph as a plain
``{node_id: node}`` dict it answers "what does the menu at X look like" and
"where does Back go". It never talks to the network — the graph is fetched by
``life_registry_client.load_graph_nodes()`` (live API, disk-cache fallback),
which is the single source of truth this reuses rather than duplicates.

Node shape used here (Life's ``/api/graph``): ``id``, ``label``, ``kind``,
``children`` (list of node ids), ``exists`` (bool), ``commercial`` (bool).
"""
from __future__ import annotations

from dataclasses import dataclass

# A node is offered in a menu only when it is real on this network and not a
# commercial (non-personal) node — the same filter load_projects_from_life
# applies when it builds the Coordinator project registry.
MAX_DEPTH = 12  # cycle guard; the graph is a tree, this is belt-and-braces


@dataclass(frozen=True)
class MenuEntry:
    """One tappable row of a menu."""

    node_id: str
    label: str
    kind: str
    is_cluster: bool  # has visible children -> tapping navigates deeper

    @property
    def is_leaf(self) -> bool:
        return not self.is_cluster


@dataclass(frozen=True)
class Menu:
    """The menu rendered when the user is "at" ``node_id``."""

    node_id: str
    label: str
    kind: str
    entries: tuple[MenuEntry, ...]
    parent_id: str | None  # None at the root of the walk -> no Back button
    breadcrumb: tuple[str, ...]  # labels from the root down to this node

    @property
    def is_leaf(self) -> bool:
        """No children to offer: the caller should open a session here."""
        return not self.entries


def is_visible(node: dict | None) -> bool:
    """Whether a node may appear in a menu at all."""
    if not isinstance(node, dict):
        return False
    return bool(node.get("exists")) and not node.get("commercial")


def label_of(node_id: str, nodes: dict[str, dict]) -> str:
    node = nodes.get(node_id)
    if isinstance(node, dict) and str(node.get("label") or "").strip():
        return str(node["label"]).strip()
    return node_id


def visible_children(node_id: str, nodes: dict[str, dict]) -> list[str]:
    """Child ids of ``node_id`` that exist in the graph and are visible."""
    node = nodes.get(node_id)
    if not isinstance(node, dict):
        return []
    raw = node.get("children")
    if not isinstance(raw, list):
        return []
    seen: set[str] = set()
    out: list[str] = []
    for child_id in raw:
        if not isinstance(child_id, str) or child_id in seen or child_id == node_id:
            continue
        seen.add(child_id)
        if is_visible(nodes.get(child_id)):
            out.append(child_id)
    return out


def parent_of(node_id: str, nodes: dict[str, dict]) -> str | None:
    """The node whose ``children`` contains ``node_id``, or None.

    Deterministic on a malformed graph with several claimed parents: the
    lowest id wins, so the Back button never depends on dict ordering.
    """
    parents = [
        pid
        for pid, node in nodes.items()
        if pid != node_id
        and isinstance(node, dict)
        and isinstance(node.get("children"), list)
        and node_id in node["children"]
    ]
    return min(parents) if parents else None


def path_to(node_id: str, nodes: dict[str, dict]) -> tuple[str, ...]:
    """Ids from the graph root down to ``node_id`` (inclusive), cycle-safe."""
    chain = [node_id]
    seen = {node_id}
    current = node_id
    for _ in range(MAX_DEPTH):
        parent = parent_of(current, nodes)
        if parent is None or parent in seen:
            break
        chain.append(parent)
        seen.add(parent)
        current = parent
    return tuple(reversed(chain))


def build_menu(
    node_id: str,
    nodes: dict[str, dict],
    *,
    root_id: str | None = None,
) -> Menu | None:
    """The menu at ``node_id``, or None when that node is not in the graph.

    ``root_id`` bounds the walk: a bot dedicated to Health must not let Back
    escape above Health into the rest of the network. At the root (or when the
    parent is outside the subtree) ``parent_id`` is None and the caller renders
    no Back button.
    """
    node = nodes.get(node_id)
    if not isinstance(node, dict):
        return None

    entries = tuple(
        MenuEntry(
            node_id=child_id,
            label=label_of(child_id, nodes),
            kind=str(nodes.get(child_id, {}).get("kind") or ""),
            is_cluster=bool(visible_children(child_id, nodes)),
        )
        for child_id in visible_children(node_id, nodes)
    )

    parent_id = None if root_id is not None and node_id == root_id else parent_of(node_id, nodes)
    if parent_id is not None and root_id is not None:
        # Never hand out a Back that leaves the bot's own subtree.
        if root_id not in path_to(parent_id, nodes) and parent_id != root_id:
            parent_id = None

    full_path = path_to(node_id, nodes)
    if root_id is not None and root_id in full_path:
        full_path = full_path[full_path.index(root_id) :]

    return Menu(
        node_id=node_id,
        label=label_of(node_id, nodes),
        kind=str(node.get("kind") or ""),
        entries=entries,
        parent_id=parent_id,
        breadcrumb=tuple(label_of(n, nodes) for n in full_path),
    )


def resolve_target(node_id: str, nodes: dict[str, dict]) -> str:
    """What tapping ``node_id`` means: "menu" (has children) or "session"."""
    return "menu" if visible_children(node_id, nodes) else "session"
