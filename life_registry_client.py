# life_registry_client.py
"""Client for Life's registry-driven node discovery.

Coordinator's project list used to be a hardcoded tuple that drifted from
reality (e.g. a ``travelers`` entry survived long after that node was split
into four separate nodes). This module fetches the live node list from
``life_backend`` (``GET /api/graph`` + ``GET /api/ports``, both already
existing) instead, so adding/removing/renaming a Life node needs zero
Coordinator-side changes.

Fallback chain on any failure: live API -> last-good disk cache ->
caller-supplied static ``fallback`` tuple (used only when neither the API nor
a cache is available, e.g. first boot on a machine with no network route to
life_backend yet).
"""
from __future__ import annotations

import json
import logging
import os
import platform
import time
from pathlib import Path

import httpx

# From the leaf module, never from `projects`: importing the registry back from
# one of its own sources is what made issue #35 possible (a half-built module
# seen by the other side, and a silent fallback to the hardcoded list).
from project_model import Project

logger = logging.getLogger("coordinator.life_registry_client")

DEFAULT_BASE_URL = os.environ.get("LIFE_BACKEND_URL", "http://localhost:9999")

# Values of LIFE_BACKEND_URL that switch the live registry off on purpose.
# The prod workers have no life_backend that can serve it: the internal box's
# is a build without /api/graph, and the graph carries no per-device
# paths for either box, so a "working" fetch would shrink the registry to the
# couple of nodes that happen to resolve. There the static registry IS the
# registry, and trying the network first only produced an ERROR at every boot
# that read like an outage.
_OFF_VALUES = frozenset({"off", "none", "disabled"})


def registry_disabled(base_url: str) -> bool:
    """True when ``LIFE_BACKEND_URL`` says this device has no live registry."""
    return base_url.strip().lower() in _OFF_VALUES


def _default_cache_path() -> Path:
    """Where the last-good node list is kept.

    It used to go next to this module — which inside the container is a
    read-only bind mount, so every write lost to EACCES and the cache rung of
    the fallback chain was never populated. The chain then read live API ->
    (nothing) -> static list, and one transient 500 from life_backend during
    boot was enough to leave the bot on the hardcoded registry for the rest of
    its life. ``/app/data`` (the state volume) is the writable one; off-container
    there is no such directory and the module dir is fine.
    """
    state_dir = Path(os.environ.get("STATE_PATH", "/app/data/state.json")).parent
    if state_dir.is_dir():
        return state_dir / "projects.cache.json"
    return Path(__file__).parent / "projects.cache.json"


_CACHE_PATH = _default_cache_path()
_OVERRIDES_PATH = Path(__file__).parent / "projects.overrides.json"

# Third rung of the graph fallback chain: the framework's own node catalog,
# read straight off a mounted Life checkout.
#
# The live API is the preferred source and stays so. But it is only reachable
# where a life_backend runs, and on a machine where it does not (the always-on
# server that actually polls Telegram, for one) the chain used to end at an
# empty dict — which is not a degradation, it is a blank menu. Every node the
# graph marks `reachVia: "menu"` became unreachable, silently, because the code
# path logs a warning and returns {} rather than failing.
#
# The catalog answers exactly what a menu needs and nothing more: id, label,
# kind, children, exists, commercial. Per-device paths deliberately are NOT in
# it (seam rule SR-3: structure is public, values are private), which is what
# makes it safe to read here — reading it duplicates no instance state. Opening
# a session still needs a path, and that still comes from the project registry.
_CATALOG_REL = Path("shell") / "src" / "data" / "life.graph.default.json"


def catalog_path() -> Path | None:
    """Where this machine's Life node catalog is, or None if it has none.

    ``LIFE_CATALOG_PATH`` points at the file directly; otherwise it is derived
    from ``LIFE_FRAMEWORK_ROOT`` (the mounted Life checkout), which is the same
    variable life_backend itself uses for the same tree.
    """
    explicit = (os.environ.get("LIFE_CATALOG_PATH") or "").strip()
    if explicit:
        return Path(explicit)
    root = (os.environ.get("LIFE_FRAMEWORK_ROOT") or "").strip()
    return Path(root) / _CATALOG_REL if root else None


def load_catalog_nodes(path: Path | None = None) -> dict[str, dict]:
    """``{node_id: node}`` from the on-disk catalog, or ``{}`` if unavailable.

    Never raises: this is the last resort of a fallback chain, so a missing or
    malformed catalog has to behave like "no catalog", not like a crash.
    """
    path = catalog_path() if path is None else path
    if path is None or not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.error("life_registry_client: catalog unreadable (%s)", exc)
        return {}
    nodes = raw.get("nodes") if isinstance(raw, dict) else None
    if not isinstance(nodes, dict):
        return {}
    return {
        node_id: {**node, "id": node_id}
        for node_id, node in nodes.items()
        if isinstance(node_id, str) and isinstance(node, dict)
    }


def resolve_device_id() -> str:
    """This machine's identity, for resolving a node's per-device path.

    Duplicated from ``suite_core.nodes.device.resolve_device_id`` in the Life
    repo — Coordinator has no dependency on Life's Python package, and this is
    five lines, not worth a shared package for.
    """
    return os.environ.get("LIFE_DEVICE") or platform.node()


# One retry, because the first call after an idle spell is the one that fails:
# life_backend hands /api/ports a pooled Postgres connection the server has
# already dropped and answers 500, then serves the very next request fine. That
# lands squarely on our boot — the registry is built at import — and cost the
# bot its whole project list twice in a row, silently falling back to the
# hardcoded one.
_FETCH_ATTEMPTS = 2
_FETCH_RETRY_DELAY_S = 2.0


def fetch_nodes(base_url: str = DEFAULT_BASE_URL, *, timeout: float = 5.0) -> list[dict]:
    """Fetch and merge Life's graph + ports into one list of node dicts.

    Each returned dict is the graph node's fields (id/label/kind/exists/
    commercial/children/paths) plus a ``ports`` key (``{front, back, db}`` or
    ``None`` if the node has no port assignment). Raises ``httpx.HTTPError`` on
    any network/HTTP-status error — callers decide the fallback.
    """
    for attempt in range(1, _FETCH_ATTEMPTS + 1):
        try:
            return _fetch_nodes_once(base_url, timeout=timeout)
        except httpx.HTTPError as exc:
            if attempt == _FETCH_ATTEMPTS:
                raise
            logger.warning(
                "life_registry_client: fetch attempt %d failed (%s), retrying in %.0fs",
                attempt, exc, _FETCH_RETRY_DELAY_S,
            )
            time.sleep(_FETCH_RETRY_DELAY_S)
    raise AssertionError("unreachable")  # pragma: no cover


def _fetch_nodes_once(base_url: str, *, timeout: float) -> list[dict]:
    with httpx.Client(timeout=timeout) as client:
        graph = client.get(f"{base_url}/api/graph").raise_for_status().json()
        ports = client.get(f"{base_url}/api/ports").raise_for_status().json()
    nodes = graph.get("nodes", {})
    merged = []
    for node_id, node in nodes.items():
        if not isinstance(node, dict):
            continue
        merged.append({**node, "id": node_id, "ports": ports.get(node_id)})
    return merged


def load_graph_nodes(
    *,
    base_url: str = DEFAULT_BASE_URL,
    cache_path: Path = _CACHE_PATH,
    instance: str | None = None,
) -> dict[str, dict]:
    """The whole Life node graph as ``{node_id: node}``, live or from cache.

    Same source of truth and same disk cache as :func:`load_projects_from_life`
    — deliberately reusing it instead of adding a second one. Unlike that
    function this keeps *every* node, clusters included: the Telegram menu
    navigates clusters, which are never Coordinator projects.

    Fallback chain: live API -> disk cache -> on-disk catalog (see
    :func:`load_catalog_nodes`). Returns ``{}`` only when all three are
    unavailable; callers degrade rather than fail.

    With ``instance`` (#78) the graph is composed from that instance's own
    checkout (catalog + ``life.graph.json``, the same composition Life runs),
    because an operator serves several instances and each has its own nodes.
    If the composition is unavailable and the instance is the device default,
    the live backend is used as before — no behavioural change for the single-
    instance device.
    """
    if instance:
        nodes = _instance_graph_nodes(instance, base_url=base_url, cache_path=cache_path)
        if nodes is not None:
            return nodes
        logger.error(
            "life_registry_client: graph of instance %r unavailable; "
            "the caller gets no graph rather than another instance's",
            instance,
        )
        return {}
    return _default_graph_nodes(base_url=base_url, cache_path=cache_path)


def _instance_graph_nodes(
    instance: str, *, base_url: str, cache_path: Path
) -> dict[str, dict] | None:
    """One instance's composed graph; ``None`` means "look elsewhere"."""
    import bus_agents

    nodes = bus_agents.instance_graph(instance)
    if nodes:
        logger.info(
            "life_registry_client: graph of instance %r from its checkout (%d nodes)",
            instance,
            len(nodes),
        )
        return nodes
    import instances

    try:
        default = instances.default_instance_id()
    except instances.InstanceError:
        default = None
    if instance == default:
        logger.warning(
            "life_registry_client: graph of default instance %r not composable, "
            "using the live backend",
            instance,
        )
        return _default_graph_nodes(base_url=base_url, cache_path=cache_path)
    return None


def _default_graph_nodes(
    *,
    base_url: str,
    cache_path: Path,
) -> dict[str, dict]:
    """The device's default graph: live API -> cache -> on-disk catalog."""
    nodes: object = None
    if registry_disabled(base_url):
        # No live backend on purpose: no API call and no cache (which could only
        # come from a time the switch was on) - straight to the catalog.
        catalog = load_catalog_nodes()
        if not catalog:
            logger.info("life_registry_client: live registry off and no catalog")
        return catalog
    try:
        nodes = fetch_nodes(base_url)
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("life_registry_client: graph fetch failed (%s), trying cache", exc)
        if cache_path.exists():
            try:
                nodes = json.loads(cache_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as cache_exc:
                logger.error("life_registry_client: graph cache unreadable (%s)", cache_exc)
    if not isinstance(nodes, list):
        catalog = load_catalog_nodes()
        if catalog:
            logger.warning(
                "life_registry_client: serving the graph from the on-disk catalog "
                "(%d nodes) — no live backend and no cache",
                len(catalog),
            )
        else:
            logger.error("life_registry_client: no graph available (API, cache, catalog)")
        return catalog
    return {
        n["id"]: n for n in nodes if isinstance(n, dict) and isinstance(n.get("id"), str)
    }


def _read_overrides(overrides_path: Path) -> dict[str, dict]:
    if not overrides_path.exists():
        return {}
    try:
        raw = json.loads(overrides_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("life_registry_client: unreadable overrides %s: %s", overrides_path, exc)
        return {}
    return raw if isinstance(raw, dict) else {}


# Where the compose mounts every node's repo inside the container. Single
# source for both the Project.workspace it builds and the mount test in
# _servable — the two must never drift.
_WORKSPACE_ROOT = "/workspaces"


DEFAULT_KIND_ENV = "COORDINATOR_KIND"
"""Quale meta' della rete serve questo Coordinator: "work" o "personal".

Non e' una proprieta' del singolo progetto, e' una proprieta' dell'**istanza**
che il bot interroga. Un Coordinator di lavoro punta al backend di `life-work`,
il cui grafo contiene i soli nodi di lavoro piu' gli strumenti di framework: la
separazione e' gia' fatta a monte, dalla scelta dell'istanza, e qui va solo
etichettata.

Prima questo campo era `"personal"` fisso. Finche' il registro veniva dal
ripiego statico (che il `kind` ce l'ha scritto dentro) non si vedeva; nel
momento in cui il registro live subentra, ogni progetto diventerebbe personale
e il bot di lavoro mostrerebbe un menu **vuoto** — un guasto che somiglia in
tutto a "non ci sono progetti".
"""


def default_kind() -> str:
    return (os.environ.get(DEFAULT_KIND_ENV) or "").strip().lower() or "personal"


def _project_from_node(
    node: dict, *, device_id: str, override: dict, kind: str | None = None
) -> Project:
    paths = node.get("paths") or {}
    host_path = paths.get(device_id) or ""
    node_id = node["id"]
    return Project(
        key=node_id,
        label=override.get("label", node.get("label") or node_id),
        emoji=override.get("emoji", ""),
        host_path=host_path,
        workspace=f"{_WORKSPACE_ROOT}/{node_id}",
        aliases=tuple(override.get("aliases", ())),
        session_globs=tuple(override.get("session_globs", ())),
        kind=override.get("kind", kind or default_kind()),
    )


def load_projects_from_life(
    *,
    base_url: str = DEFAULT_BASE_URL,
    cache_path: Path = _CACHE_PATH,
    overrides_path: Path = _OVERRIDES_PATH,
    fallback: tuple[Project, ...] = (),
    test_mounts: bool = True,
) -> tuple[Project, ...]:
    """Build the Coordinator project registry from Life's live node list.

    ``test_mounts=False`` keeps nodes whose ``/workspaces/<id>`` is not mounted:
    the caller is going to decide their workspace itself (see
    ``projects._merge_local_into_live``) and will run :func:`drop_unmounted`
    afterwards, on the workspace that was actually assigned.
    """
    if registry_disabled(base_url):
        logger.info(
            "life_registry_client: live registry off (LIFE_BACKEND_URL=%s), "
            "serving the static registry",
            base_url.strip(),
        )
        return fallback
    try:
        nodes = fetch_nodes(base_url)
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("life_registry_client: fetch failed (%s), trying cache", exc)
        if not cache_path.exists():
            logger.error("life_registry_client: no cache available, using static fallback")
            return fallback
        try:
            nodes = json.loads(cache_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as cache_exc:
            logger.error(
                "life_registry_client: cache unreadable (%s), using static fallback", cache_exc
            )
            return fallback

    # Try to write cache, but don't fail if we can't — we have good data in memory.
    try:
        cache_path.write_text(json.dumps(nodes), encoding="utf-8")
    except OSError as exc:
        logger.warning(
            "life_registry_client: cache write failed (%s), continuing with in-memory data", exc
        )

    device_id = resolve_device_id()
    overrides = _read_overrides(overrides_path)
    # `commercial` is a Life-side label ("this node is a product"), not a
    # statement about who owns it: Cosmos, Mycorise and comunity are products
    # *and* personal nodes of this same user, with their repo sitting right
    # next to the others. Filtering on it dropped exactly those three from the
    # picker while the frozen projects.local.json still listed them — which is
    # what made the live registry look like a downgrade and kept the hand-kept
    # file alive. What a node is for is not what decides whether it can be
    # opened; `_servable` below decides that.
    active = [n for n in nodes if n.get("exists")]
    if not active:
        logger.warning("life_registry_client: 0 active nodes returned, using static fallback")
        return fallback
    active, dropped = _servable(active, test_mounts=test_mounts)
    if dropped:
        # Named, not just counted: a node missing from the menu because its
        # workspace is not mounted looks exactly like a node that was deleted.
        logger.info(
            "life_registry_client: %d node(s) left out of the registry — %s",
            len(dropped),
            "; ".join(f"{nid} ({why})" for nid, why in dropped),
        )
    if not active:
        logger.warning(
            "life_registry_client: no servable node left, using static fallback"
        )
        return fallback
    # Il `kind` si risolve UNA volta per tutta la lista: e' l'istanza a deciderlo,
    # non il singolo nodo, e leggerlo per progetto darebbe l'idea sbagliata.
    kind = default_kind()
    return tuple(
        _project_from_node(
            n, device_id=device_id, override=overrides.get(n["id"], {}), kind=kind
        )
        for n in active
    )


# What a workspace holds when it deliberately holds nothing: the compose
# mounts `${WS_<NODE>:-./.ws-placeholder}`, and that placeholder directory
# carries a single .gitkeep so git can track it.
#
# `children` is there for a subtler reason. The cluster mounts nest a child
# repo INSIDE its parent workspace (`/workspaces/home/children/<child>`), and
# Docker creates that mountpoint on the host side of whatever the parent
# resolves to — including `.ws-placeholder`. So the shared placeholder grows a
# root-owned `children/` directory and stops looking empty: every node without
# a WS_* passed the check again and came back as a button that opens nothing,
# with photos and inbox files landing in `Coordinator/.ws-placeholder`. A real
# checkout always carries more than these two names.
_PLACEHOLDER_ENTRIES = {".gitkeep", "children"}


def _has_checkout(path: str) -> bool:
    """Is there actually a repo at this workspace path?

    Existence is not the test, in two ways. A bind mount whose host side does
    not exist gets an empty directory created by Docker (that is how a WS_*
    pointing at another device's path fails); and a node with no workspace on
    this device is mounted from `.ws-placeholder`, which is not empty — it
    holds a .gitkeep. Both used to pass an `isdir` check and reach the picker
    as buttons that open nothing.
    """
    try:
        with os.scandir(path) as entries:
            return any(e.name not in _PLACEHOLDER_ENTRIES for e in entries)
    except OSError:
        return False


def _in_container() -> bool:
    """Are we running inside the bot container, where the mounts live?

    The mount test below is only meaningful there. It used to be gated on
    `/workspaces` merely existing, which is not a container marker: this dev
    box has a stray `/workspaces` of its own, so the filter ran host-side,
    dropped every node and collapsed the registry to the static fallback —
    visible as seven red tests in tests/test_life_registry_client.py.
    `/.dockerenv` is created by the engine in every container and needs no
    compose entry that someone can forget on the next host.
    """
    return os.path.exists("/.dockerenv")


def workspace_montato(workspace: str) -> bool:
    """C'e' davvero un checkout dietro questo workspace?

    Fuori dal container la domanda non ha risposta — i mount non ci sono — e
    l'unica risposta onesta e' "si'": negare tutto collasserebbe il registro,
    che e' esattamente l'incidente descritto in ``_in_container``.
    """
    if not (_in_container() and os.path.isdir(_WORKSPACE_ROOT)):
        return True
    return _has_checkout(workspace)


def drop_unmounted(projects: tuple) -> tuple[tuple, list[tuple[str, str]]]:
    """Split built projects into the ones whose workspace is really mounted.

    Same test as the mount half of :func:`_servable`, but applied to a
    *Project* instead of a graph node — because the workspace a project ends up
    with is not always ``/workspaces/<node id>``. When a device's local file
    supplements the graph (issue #35) the mount it already has under an older
    name is kept, and asking about the node id would drop a node that is in
    fact perfectly openable. The honest question is always "is THIS workspace
    mounted", and it can only be asked once the workspace is decided.
    """
    if not (_in_container() and os.path.isdir(_WORKSPACE_ROOT)):
        return tuple(projects), []
    kept, dropped = [], []
    for project in projects:
        if _has_checkout(project.workspace):
            kept.append(project)
        else:
            dropped.append((project.key, f"{project.workspace} non e' montato"))
    return tuple(kept), dropped


def _servable(
    nodes: list[dict], *, test_mounts: bool = True
) -> tuple[list[dict], list[tuple[str, str]]]:
    """Split the live nodes into the ones this device can actually open, and
    the ones it cannot — with the reason.

    `exists` is not enough. Two classes slip through it:

    * **virtual clusters** (`system`, `food`, `learn`, …) — grouping nodes with
      no repo anywhere, recognisable by `paths: null`. They are `exists: true`
      in the graph because they exist *as nodes*, not as code;
    * **nodes not cloned on this device** — real repos whose workspace holds
      nothing here (see :func:`_has_checkout`).

    Both used to reach the picker as buttons that open nothing: on the minipc
    that was 23 dead entries out of 39. The mount test is the honest one — the
    bot can only start a session where the code actually is — and it is done
    against the *workspace* (the in-container path), never the host path, which
    is a different filesystem for every device.
    """
    # The mount test only means something where the mounts are: inside the
    # container. Run host-side (tests, tooling) `/workspaces` does not exist at
    # all, and testing each node against it would drop every one of them and
    # collapse the registry to the static fallback. There, only the virtual
    # clusters are filtered.
    mounts_visible = (
        test_mounts and _in_container() and os.path.isdir(_WORKSPACE_ROOT)
    )
    servable: list[dict] = []
    dropped: list[tuple[str, str]] = []
    for n in nodes:
        node_id = n["id"]
        if not n.get("paths"):
            dropped.append((node_id, "cluster virtuale, nessun repo"))
        elif mounts_visible and not _has_checkout(f"{_WORKSPACE_ROOT}/{node_id}"):
            dropped.append((node_id, "nessun repo in questo workspace"))
        else:
            servable.append(n)
    return servable, dropped
