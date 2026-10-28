"""Pick which node of a cluster a free-text message is about.

The Coordinator already had this shape one level lower: ``resolve_agent_by_intent``
scores the *agents* inside one project by the keywords they declare, and lets the
bound default win on a tie so a weak signal never silently overrides an explicit
choice. This module lifts the same idea one floor up — from "which agent inside a
node" to "which node inside a cluster" — because a bot rooted at a cluster reads
one room for a whole subtree, and asking the person to name the sub-node first is
asking them to do the routing by hand.

Two rules carry the whole design:

1. **A link outranks a sentence.** Domains are unambiguous evidence: a message
   containing a link to a rental portal is about rentals, whatever else it says.
   Words are shared property — "casa" belongs to every node of a Home cluster —
   so they rank candidates but never settle a tie by themselves.

2. **Ambiguity is answered with a question, never with a guess.** No match, or a
   tie between siblings, returns ``Ambiguous`` and the caller shows buttons. A
   wrong route taken silently is indistinguishable from a right one: the message
   simply lands somewhere and nobody learns it went astray. That is the same
   failure mode as a mute room, one layer up.

A candidate that the graph marks ``exists: false`` is a *planned* node, not a
usable one. It is never returned as a destination; the walk stops at the nearest
built ancestor and the caller is told the specialised node is not there yet — the
honest answer, instead of routing into a repo that does not exist.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass

import manifest
import projects

logger = logging.getLogger("coordinator.node_routing")

# A domain match is worth more than any realistic pile of words. Not infinity:
# two nodes that both claim the same domain must still read as a tie (and so ask)
# rather than let declaration order decide.
DOMAIN_WEIGHT = 100
KEYWORD_WEIGHT = 1

_WORD_RE = re.compile(r"\w+", re.UNICODE)
# Deliberately loose: it has to find the host inside a message written by a human,
# not validate a URL. Telegram sends bare "immobiliare.it/annunci/123" all the time.
_URL_RE = re.compile(r"(?:https?://)?(?:www\.)?([a-z0-9][a-z0-9.-]*\.[a-z]{2,})(?:/\S*)?", re.I)


@dataclass(frozen=True)
class Candidate:
    """One node that could receive the message, with why."""

    node_id: str
    score: int
    reasons: tuple[str, ...] = ()


@dataclass(frozen=True)
class Decision:
    """Where the message goes — or why it cannot be decided.

    ``kind`` is one of:
      ``"routed"``     — one clear winner, in ``node_id``.
      ``"ambiguous"``  — several plausible ones, in ``candidates``: ask.
      ``"root"``       — nothing matched: the message stays where it landed.
      ``"unbuilt"``    — the best match is a node that does not exist yet;
                         ``node_id`` holds the nearest built ancestor and
                         ``planned`` the node that was actually meant.
    """

    kind: str
    node_id: str
    candidates: tuple[Candidate, ...] = ()
    planned: str = ""
    # Set only when a department agent made the choice (see reparto_routing).
    reparto: str = ""
    motivo: str = ""

    @property
    def needs_question(self) -> bool:
        return self.kind == "ambiguous"


def hosts_in(text: str) -> tuple[str, ...]:
    """Every host-looking token in the message, lower-cased, de-duplicated."""
    out = []
    for match in _URL_RE.finditer(text or ""):
        host = match.group(1).lower().rstrip(".")
        if host not in out:
            out.append(host)
    return tuple(out)


def _domain_hit(declared: str, host: str) -> bool:
    """``immobiliare.it`` matches ``www.immobiliare.it`` and any subdomain of it.

    Anchored on a dot boundary so ``casa.it`` does not swallow ``nocasa.it``.
    """
    return host == declared or host.endswith("." + declared)


def descendants(root_id: str, nodes: dict[str, dict]) -> list[str]:
    """Every node under ``root_id``, depth-first, root excluded. Cycle-safe."""
    out: list[str] = []
    seen = {root_id}
    stack = list(reversed((nodes.get(root_id) or {}).get("children") or []))
    while stack:
        node_id = stack.pop()
        if node_id in seen or node_id not in nodes:
            continue
        seen.add(node_id)
        out.append(node_id)
        stack.extend(reversed((nodes.get(node_id) or {}).get("children") or []))
    return out


def _nearest_built_ancestor(node_id: str, root_id: str, nodes: dict[str, dict]) -> str:
    """Walk up from ``node_id`` to the first node that exists. Root is the floor."""
    parent_of = {
        child: parent
        for parent, node in nodes.items()
        for child in (node.get("children") or [])
    }
    current = parent_of.get(node_id)
    while current and current != root_id:
        if (nodes.get(current) or {}).get("exists"):
            return current
        current = parent_of.get(current)
    return root_id


def score_candidates(text: str, root_id: str, nodes: dict[str, dict]) -> list[Candidate]:
    """Score every descendant of ``root_id`` against the message. Highest first."""
    lowered = (text or "").lower()
    words = set(_WORD_RE.findall(lowered))
    found_hosts = hosts_in(lowered)

    scored: list[Candidate] = []
    for node_id in descendants(root_id, nodes):
        project = projects.resolve_project(node_id)
        if project is None:
            # No workspace => no manifest => nothing declared. Not an error: most
            # of the graph is like this. It simply cannot be a destination.
            continue
        rule = manifest.routing_of(project)
        if rule.is_empty:
            continue
        score, reasons = 0, []
        for declared in rule.domains:
            if any(_domain_hit(declared, host) for host in found_hosts):
                score += DOMAIN_WEIGHT
                reasons.append(declared)
        for keyword in rule.keywords:
            hit = keyword in lowered if " " in keyword else keyword in words
            if hit:
                score += KEYWORD_WEIGHT
                reasons.append(keyword)
        if score:
            scored.append(Candidate(node_id=node_id, score=score, reasons=tuple(reasons)))

    scored.sort(key=lambda c: (-c.score, c.node_id))
    return scored


def route(text: str, root_id: str, nodes: dict[str, dict]) -> Decision:
    """Where a message dropped in ``root_id``'s room should go."""
    if not nodes:
        # The graph is the map. Without it we cannot even list the candidates —
        # and "no candidate matched" would be a lie, so say the true thing.
        logger.warning(
            "node_routing: nessuna mappa dei nodi, il messaggio resta su %r", root_id
        )
        return Decision(kind="root", node_id=root_id)

    scored = score_candidates(text, root_id, nodes)
    if not scored:
        return Decision(kind="root", node_id=root_id)

    best = scored[0]
    tied = [c for c in scored if c.score == best.score]
    if len(tied) > 1:
        return Decision(kind="ambiguous", node_id=root_id, candidates=tuple(tied))

    if not (nodes.get(best.node_id) or {}).get("exists"):
        fallback = _nearest_built_ancestor(best.node_id, root_id, nodes)
        return Decision(kind="unbuilt", node_id=fallback, planned=best.node_id)

    return Decision(kind="routed", node_id=best.node_id, candidates=(best,))
