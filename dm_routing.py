# dm_routing.py
"""The direct chat with the general Coordinator: instance first, then node (#83, F2).

In a group the destination is the binding. In the DM there is no binding to lean on
the first time, and once there is one, a message may want to go elsewhere. Same two
rules as ``node_routing``, lifted one floor (spec 2026-10-15 §4.2):

1. **An explicit prefix wins**: ``acme: …`` or ``personal/personalChef: …``. Only a
   *known* instance id counts as a prefix, so ``opus/high: …`` (model_prefs) and
   ``nota: comprare il latte`` are never mistaken for one.
2. **Without a prefix, a bound DM stays where it is**: words never hijack an ongoing
   thread. An unbound DM looks for node names in the text; a name that only one
   instance hosts settles both floors.
3. **Ambiguity is a question, never a guess**: several candidates (or none) return
   buttons — instances first, then the nodes of the chosen one.

Pure: the bot passes what the profile can see and what the graph places where.
"""
from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

# A node as the DM sees it: (project key, label, extra names: slug, aliases).
Node = tuple[str, str, tuple[str, ...]]

_PREFIX = re.compile(r"^\s*([A-Za-z][\w-]*)(?:/([\w-]+))?\s*:\s*\S")
_WORD = re.compile(r"[\w-]{4,}")

CB = "dmr"
"""Callback prefix: ``dmr:i:<instance>`` (choose instance), ``dmr:n:<instance>:<key>``."""


@dataclass(frozen=True)
class Decision:
    kind: str  # keep | route | ask_instance | ask_node | denied
    instance: str | None = None
    project_key: str | None = None
    options: tuple[str, ...] = field(default_factory=tuple)  # instances or project keys


def parse_prefix(text: str, instance_ids: Sequence[str]) -> tuple[str, str | None] | None:
    """``(instance, node token or None)`` when ``text`` opens with a known instance id."""
    m = _PREFIX.match(text or "")
    if not m:
        return None
    known = {i.lower(): i for i in instance_ids}
    iid = known.get(m.group(1).lower())
    if iid is None:
        return None
    return iid, m.group(2)


def _names(node: Node) -> set[str]:
    key, label, extra = node
    words = {key.lower(), *(e.lower() for e in extra if e)}
    clean = re.sub(r"[^\w\s-]", " ", label or "").strip().lower()
    if clean:
        words.add(clean)
    return words


def _find(token: str, nodes: Sequence[Node]) -> str | None:
    t = token.lower()
    for node in nodes:
        if t in _names(node):
            return node[0]
    return None


def mentioned(text: str, nodes_by_instance: Mapping[str, Sequence[Node]]) -> list[tuple[str, str]]:
    """``(instance, key)`` of every node whose name appears as a whole word in ``text``."""
    words = {w.lower() for w in _WORD.findall(text or "")}
    low = (text or "").lower()
    out: list[tuple[str, str]] = []
    for iid, nodes in nodes_by_instance.items():
        for node in nodes:
            names = _names(node)
            hit = any(n in words for n in names if " " not in n) or any(
                re.search(rf"\b{re.escape(n)}\b", low) for n in names if " " in n
            )
            if hit and (iid, node[0]) not in out:
                out.append((iid, node[0]))
    return out


def decide(
    text: str,
    *,
    bound: bool,
    bound_instance: str | None,
    visible: Sequence[str],
    all_instances: Sequence[str],
    nodes_by_instance: Mapping[str, Sequence[Node]],
) -> Decision:
    """Where a DM message goes. ``visible``: the instances the sender's profile reaches."""
    prefix = parse_prefix(text, all_instances)
    if prefix is not None:
        iid, token = prefix
        if iid not in visible:
            return Decision("denied", instance=iid)
        nodes = nodes_by_instance.get(iid, ())
        if token:
            key = _find(token, nodes)
            if key is not None:
                return Decision("route", instance=iid, project_key=key)
            return Decision("ask_node", instance=iid, options=tuple(n[0] for n in nodes))
        if bound and bound_instance == iid:
            return Decision("keep", instance=iid)
        return Decision("ask_node", instance=iid, options=tuple(n[0] for n in nodes))

    if bound:
        return Decision("keep", instance=bound_instance)

    seen = {i: nodes_by_instance.get(i, ()) for i in visible}
    hits = mentioned(text, seen)
    if len(hits) == 1:
        iid, key = hits[0]
        return Decision("route", instance=iid, project_key=key)
    if hits:
        instances = tuple(dict.fromkeys(i for i, _ in hits))
        if len(instances) == 1:
            return Decision("ask_node", instance=instances[0], options=tuple(k for _, k in hits))
        return Decision("ask_instance", options=instances)
    if len(visible) == 1:
        only = visible[0]
        return Decision("ask_node", instance=only,
                        options=tuple(n[0] for n in nodes_by_instance.get(only, ())))
    return Decision("ask_instance", options=tuple(visible))


def read_callback(data: str) -> tuple[str, str, str | None] | None:
    """``("i", instance, None)`` | ``("n", instance, key)`` | None."""
    parts = (data or "").split(":", 3)
    if len(parts) < 3 or parts[0] != CB:
        return None
    if parts[1] == "i":
        return ("i", parts[2], None)
    if parts[1] == "n" and len(parts) == 4 and parts[3]:
        return ("n", parts[2], parts[3])
    return None
