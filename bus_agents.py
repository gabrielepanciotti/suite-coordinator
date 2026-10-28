# bus_agents.py
"""The bus identities this device listens on — computed from Life, not written.

Until #81 they were ``BOT_LOCAL_AGENTS``: 41 hand-written ``tenant/name:key``
lines on dev-linux, a copy of the Life graph that drifted the way copies do.
It still said ``call-briefing-dev-linux`` after the node became
``callBriefing``, and it never learnt about ``pensaDigitaleBot`` — so the
conversations sent to it reached nobody, without a single error.

Life now computes the list once (Life 00bc71d, ``suite_core.instances.identity``):
for every instance in the device registry, every graph node with a path on this
host is ``<graph id>-<device suffix>`` on the bus tenant of its instance. This
module asks that function, and nothing else, so a rename in the graph reaches
the bot on its next boot.

Three values of ``HUB_LOCAL_AGENTS`` (the compose maps ``BOT_LOCAL_AGENTS`` on it):

* unset or ``auto`` — the computed list (the normal case);
* empty — no local agents at all. Deliberate on the PC, where the worker owns
  those identities and a second listener would answer every message twice;
* a list — an explicit override, kept as is, with a WARNING naming every
  difference from the computed list, so an override cannot rot in silence.

Life is reached through its checkout mounted in the container
(``LIFE_FRAMEWORK_ROOT``, default ``/workspaces/life``), never vendored: the
calculation has one owner. If it is not reachable the answer is "unavailable",
said at ERROR — an empty list that looks like "this device hosts nothing" is
exactly the silent failure this module exists to remove.
"""

from __future__ import annotations

import logging
import os
import re
import socket
import sys
from dataclasses import dataclass, field
from pathlib import Path

import agent_nodes

logger = logging.getLogger("coordinator.bus_agents")

AUTO = "auto"
DEFAULT_FRAMEWORK_ROOT = "/workspaces/life"


class DerivationUnavailable(RuntimeError):
    """Life's calculation could not run here (no checkout, no registry, ...)."""


@dataclass(frozen=True)
class DerivedAgent:
    tenant: str
    instance_id: str
    graph_id: str
    name: str
    path: str
    former: tuple[str, ...] = field(default_factory=tuple)

    @property
    def pair(self) -> str:
        """The ``HUB_LOCAL_AGENTS`` spelling: ``tenant/name:graphId``."""
        return f"{self.tenant}/{self.name}:{self.graph_id}"


def framework_root(env=None) -> Path:
    env = os.environ if env is None else env
    return Path(env.get("LIFE_FRAMEWORK_ROOT") or DEFAULT_FRAMEWORK_ROOT)


def _import_local_agents(root: Path):
    """``suite_core.instances.identity.local_agents`` from the Life checkout at ``root``."""
    shared = root / "packages" / "shared"
    if not (shared / "suite_core" / "instances" / "identity.py").is_file():
        raise DerivationUnavailable(
            f"{shared}/suite_core/instances/identity.py assente: Life non montato "
            "o piu' vecchio di 00bc71d"
        )
    if str(shared) not in sys.path:
        sys.path.append(str(shared))
    try:
        from suite_core.instances.identity import local_agents
    except Exception as exc:  # an import error inside Life is still "unavailable"
        raise DerivationUnavailable(f"import di suite_core fallito: {exc}") from exc
    return local_agents


def derive(env=None) -> tuple[list[DerivedAgent], list[str]]:
    """Ask Life which identities this device hosts. Raises DerivationUnavailable."""
    env = os.environ if env is None else env
    root = framework_root(env)
    local_agents = _import_local_agents(root)
    # Inside the container the hostname is a container id: the graph's `paths`
    # are keyed by the host's name, which LIFE_DEVICE carries.
    host = (env.get("LIFE_DEVICE") or "").strip() or socket.gethostname()
    try:
        agents, problems = local_agents(host=host, env=env, root=root)
    except Exception as exc:
        raise DerivationUnavailable(f"calcolo di Life fallito: {exc}") from exc
    out = [
        DerivedAgent(
            a.tenant, a.instance_id, a.graph_id, a.name, a.path, tuple(a.former)
        )
        for a in agents
    ]
    # Life#157 §3.5: an ``agent`` node has no listener and no key — its door is
    # the Coordinator. Checked on the host path and on the container mount.
    skipped = [
        a.graph_id
        for a in out
        if agent_nodes.is_agent_dir(a.path)
        or agent_nodes.is_agent_dir(f"/workspaces/{a.graph_id}")
    ]
    if skipped:
        logger.info("nodi agent senza presenza sul bus: %s", ", ".join(skipped))
        out = [a for a in out if a.graph_id not in skipped]
    if not out:
        raise DerivationUnavailable(
            f"nessun agente per l'host {host!r} ({'; '.join(problems) or 'registro vuoto'})"
        )
    return out, list(problems)


_CACHE: dict[str, object] = {}


def derived(env=None) -> tuple[list[DerivedAgent], list[str]] | None:
    """Cached :func:`derive` for this process; None when unavailable (logged once)."""
    if "result" not in _CACHE:
        try:
            _CACHE["result"] = derive(env)
        except DerivationUnavailable as exc:
            _CACHE["result"] = None
            _CACHE["error"] = str(exc)
    return _CACHE["result"]  # type: ignore[return-value]


def _compose_instance(instance_id: str, env=None) -> dict[str, dict]:
    """Compose one instance's graph: framework catalog + its ``life.graph.json``.

    Raises when Life is not mounted, the instance is not registered, or its
    manifest is unreadable. The caller (``instance_graph``) turns that into a
    named ``None``: one broken instance must not hide the others.
    """
    env = os.environ if env is None else env
    root = framework_root(env)
    _import_local_agents(root)  # puts Life on sys.path, or raises
    import json as _json

    from suite_core.instances.identity import GRAPH_REL
    from suite_core.instances.store import load_registry
    from suite_core.nodes.graph_compose import compose_graph

    catalog = _json.loads((root / GRAPH_REL).read_text(encoding="utf-8"))
    registered, _ = load_registry(env)
    inst = registered.get(instance_id)
    if inst is None:
        raise DerivationUnavailable(
            f"istanza {instance_id!r} non registrata su questo device"
        )
    manifest = _json.loads(
        (Path(inst.path) / "life.graph.json").read_text(encoding="utf-8")
    )
    nodes = compose_graph(catalog, manifest).get("nodes") or {}
    return {
        gid: {**node, "id": gid}
        for gid, node in nodes.items()
        if isinstance(gid, str) and isinstance(node, dict)
    }


def instance_graph(instance_id: str, env=None) -> dict[str, dict] | None:
    """The composed graph of one instance (#78), or ``None`` when unavailable.

    This is the operator's own answer to "which nodes does *this* instance
    have": the framework catalog plus that instance's manifest, composed the
    same way Life does it. It needs no second backend and no network, so an
    operator serves every registered instance with the checkouts it already
    mounts. The result is cached, and a failure is remembered as ``None`` with
    the reason logged — a named degradation, never a silent empty graph.
    """
    key = ("instance_graph", instance_id)
    if key in _CACHE:
        return _CACHE[key]  # type: ignore[return-value]
    try:
        nodes = _compose_instance(instance_id, env)
    except Exception as exc:
        logger.warning("graph of instance %s unavailable: %s", instance_id, exc)
        nodes = None
    _CACHE[key] = nodes
    return nodes


def graphs(env=None) -> dict[str, dict[str, dict]]:
    """``{instance: composed graph}`` for every registered, readable instance."""
    env = os.environ if env is None else env
    if "graphs" in _CACHE:
        return _CACHE["graphs"]  # type: ignore[return-value]
    out: dict[str, dict[str, dict]] = {}
    try:
        root = framework_root(env)
        _import_local_agents(root)  # puts Life on sys.path, or raises
        from suite_core.instances.store import load_registry

        registered, _ = load_registry(env)
        for iid in registered:
            nodes = instance_graph(iid, env)
            if nodes:
                out[iid] = nodes
    except Exception as exc:
        logger.warning("instance graphs unavailable: %s", exc)
        out = {}
    _CACHE["graphs"] = out
    return out


def instance_for_node(node_id: str, env=None) -> str | None:
    """Which registered instance hosts ``node_id``, or ``None`` if not unique.

    A node can live in several instance graphs (infra nodes, e.g. ``scheduler``).
    Then the device default wins when it is one of the owners — the same rule
    Life applies to bus identities — and an ambiguous result with no default
    stays ``None``: never a guess.
    """
    wanted = (node_id or "").lower()
    if not wanted:
        return None
    owners = [
        iid
        for iid, nodes in graphs(env).items()
        if wanted in {gid.lower() for gid in nodes}
    ]
    if len(owners) == 1:
        return owners[0]
    if not owners:
        return None
    import instances

    try:
        default = instances.default_instance_id(env)
    except instances.InstanceError:
        default = None
    if default in owners:
        return default
    return None


def graph_nodes(env=None) -> dict[str, set[str]]:
    """``{instance: graph ids with a repo on some device}`` — every device, not just this one.

    The local list above answers "what runs here"; this answers "which instance is
    this node of" for nodes that run elsewhere (#83, F3). Same sources (registry +
    each instance's composed graph, via Life), no names invented. Empty when Life
    is not reachable: the caller then knows only the local nodes, as before.
    """
    env = os.environ if env is None else env
    if "graph_nodes" in _CACHE:
        return _CACHE["graph_nodes"]  # type: ignore[return-value]
    out = {
        iid: {
            gid
            for gid, node in nodes.items()
            if node.get("paths") and node.get("bus") is not False
        }
        for iid, nodes in graphs(env).items()
    }
    _CACHE["graph_nodes"] = out
    return out


def derive_error() -> str:
    return str(_CACHE.get("error", ""))


def reset_cache() -> None:
    _CACHE.clear()


def _pairs(raw: str) -> dict[str, str]:
    """``{tenant/name: project}`` from a ``HUB_LOCAL_AGENTS`` string."""
    out: dict[str, str] = {}
    for part in raw.split(","):
        part = part.strip()
        if ":" in part:
            k, v = part.split(":", 1)
            out[k.strip()] = v.strip()
    return out


def divergence(explicit: str, computed: list[DerivedAgent]) -> list[str]:
    """Human-readable differences between an override and the computed list."""
    have = _pairs(explicit)
    want = {f"{a.tenant}/{a.name}": a.graph_id for a in computed}
    # An unqualified override entry listens on every tenant: compare on the name.
    have_names = {k.split("/", 1)[-1]: (k, v) for k, v in have.items()}
    diffs: list[str] = []
    for key, gid in sorted(want.items()):
        name = key.split("/", 1)[1]
        if name not in have_names:
            diffs.append(f"manca {key}")
        elif have_names[name][1] != gid:
            diffs.append(f"{name}: progetto {have_names[name][1]!r}, dal grafo {gid!r}")
    want_names = {k.split("/", 1)[1] for k in want}
    diffs.extend(f"in piu' {k}" for k in sorted(have) if k.split("/", 1)[-1] not in want_names)
    return diffs


def local_agents_spec(raw: str | None, env=None) -> tuple[str, str]:
    """The ``HUB_LOCAL_AGENTS`` string to use, and where it came from.

    Returns ``(spec, source)`` with source ``"grafo"``, ``"override"``,
    ``"vuoto"`` or ``"non disponibile"``.
    """
    value = None if raw is None else raw.strip()
    if value == "":
        return "", "vuoto"
    if value is not None and value.lower() != AUTO:
        result = derived(env)
        if result is not None:
            diffs = divergence(value, result[0])
            if diffs:
                logger.warning(
                    "BOT_LOCAL_AGENTS e' un override esplicito e diverge dal grafo "
                    "(%d differenze): %s — toglilo dal .env per usare il calcolo",
                    len(diffs),
                    "; ".join(diffs),
                )
        return value, "override"
    result = derived(env)
    if result is None:
        logger.error(
            "agenti locali NON calcolabili (%s): nessun listener sul bus. "
            "Monta Life (WS_LIFE) e ~/.life, oppure scrivi BOT_LOCAL_AGENTS",
            derive_error(),
        )
        return "", "non disponibile"
    agents, problems = result
    for problem in problems:
        logger.info("instances agents: %s", problem)
    return ",".join(a.pair for a in agents), "grafo"


def kebab(graph_id: str) -> str:
    """``acmeAuth`` -> ``acme-auth``: the spelling the bus used before Network#42."""
    return re.sub(r"(?<!^)(?=[A-Z])", "-", graph_id).lower()


def former_names(agent: DerivedAgent, suffix: str) -> list[str]:
    """Bus names this identity may still carry: ``formerIds`` first, then the kebab form."""
    bases = [*agent.former, kebab(agent.graph_id)]
    names = [f"{b}-{suffix}" for b in bases]
    return [n for n in dict.fromkeys(names) if n != agent.name]


def suffix_of(agent: DerivedAgent) -> str:
    return agent.name[len(agent.graph_id) + 1 :]
