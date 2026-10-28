# projects.py
"""Registry + discovery for the Telegram session dashboard.

A *Project* is a workspace (e.g. Cosmos). Its *Agents* are the subagents in
``<workspace>/.claude/agents/*.md`` plus a synthetic 💬 Generale (the project's
main CLAUDE.md persona). Pure logic — no Telegram imports — so it unit-tests
without a bot token.
"""

from __future__ import annotations

import dataclasses
import fnmatch
import json
import logging
import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

import life_registry_client
import local_defaults
from competenza_routing import settore_per_competenza

# `Project` lives in its own leaf module so that `life_registry_client` can use
# it without importing us back (issue #35). Re-exported here: every caller in
# the codebase says `from projects import Project`, and that stays true.
from project_model import Project

__all__ = ["Project"]

logger = logging.getLogger("coordinator.projects")


# Root holding every project on this device. Derived from this repo's own location
# (<SRV_ROOT>/Coordinator/projects.py) so no device's layout is baked in; set SRV_ROOT
# when the checkout does not sit directly under that root.
_SRV_ROOT = Path(os.getenv("SRV_ROOT") or Path(__file__).resolve().parent.parent)

# The generic ~/.claude config workspace: the only default the engine itself
# ships, because every installation has one. Everything else in the fallback
# registry is this installation's own inventory and lives in
# locale/projects.json (issue #43), read by _load_default_projects() below.
_CLAUDE_CONFIG_PROJECT = Project(
    "claude",
    "⚙️ .claude config",
    "⚙️",
    os.path.join(os.path.expanduser("~"), ".claude"),
    "/workspaces/claude-config",
    aliases=("config", "dotclaude", "claude-config"),
)

LOCALE_PROJECTS_FILE = "projects.json"


def _project_from_dict(entry: object, *, config_path: str, index: int) -> Project:
    """Build a Project from one JSON entry, raising ValueError on bad shape."""
    if not isinstance(entry, dict):
        raise ValueError(
            f"{config_path}: project #{index} must be an object, got {type(entry).__name__}"
        )
    missing = [k for k in ("key", "label", "workspace") if k not in entry]
    # The host path comes either absolute (host_path, per-device files such as
    # projects.local.json) or relative to _SRV_ROOT (host_rel, versioned files
    # such as locale/projects.json, where an absolute path would break every
    # other device).
    if "host_path" not in entry and "host_rel" not in entry:
        missing.append("host_path")
    if missing:
        raise ValueError(
            f"{config_path}: project #{index} is missing required key(s): "
            f"{', '.join(missing)}"
        )
    if "host_path" in entry:
        host_path = entry["host_path"]
    else:
        host_path = str(_SRV_ROOT / entry["host_rel"])
    return Project(
        key=entry["key"],
        label=entry["label"],
        emoji=entry.get("emoji", ""),  # optional: decorative only (title/button prefix)
        host_path=host_path,
        workspace=entry["workspace"],
        aliases=tuple(entry.get("aliases") or ()),
        session_globs=tuple(entry.get("session_globs") or ()),
        kind=entry.get("kind", "work"),
        slug=entry.get("slug", ""),
    )


def _load_default_projects(path: Path | None = None) -> tuple[Project, ...]:
    """The fallback registry: locale/projects.json plus the generic ~/.claude entry.

    This is the list the bot falls back to when Life cannot answer, and the
    *only* list the prod workers use (they run with ``LIFE_BACKEND_URL=off``),
    so it must never raise at import: a missing file is a fresh installation
    (info), a malformed one is a broken one (error) — both leave just the
    engine's own ``claude`` entry. The ``claude`` entry is appended unless the
    file already defines that key.
    """
    if path is None:
        path = local_defaults.locale_dir() / LOCALE_PROJECTS_FILE
    path_str = str(path)
    if not path.is_file():
        logger.info(
            "no local project inventory at %s: fallback registry is the ~/.claude "
            "config only",
            path_str,
        )
        return (_CLAUDE_CONFIG_PROJECT,)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, list):
            raise ValueError(f"{path_str}: must be a JSON list of project objects")
        loaded = tuple(
            _project_from_dict(entry, config_path=path_str, index=index)
            for index, entry in enumerate(raw)
        )
    except (OSError, ValueError) as exc:  # JSONDecodeError is a ValueError
        logger.error(
            "local project inventory %s is malformed (%s): fallback registry is the "
            "~/.claude config only",
            path_str,
            exc,
        )
        return (_CLAUDE_CONFIG_PROJECT,)
    if any(p.key == _CLAUDE_CONFIG_PROJECT.key for p in loaded):
        return loaded
    return (*loaded, _CLAUDE_CONFIG_PROJECT)


# Default registry — used as a fallback when no live registry / per-device
# config is available. The active registry is `PROJECTS`, built by
# `load_projects()` below. Host paths are derived from _SRV_ROOT (host_rel in
# the locale file), never hardcoded: a device whose layout differs overrides
# them in projects.local.json.
_DEFAULT_PROJECTS: tuple[Project, ...] = _load_default_projects()


_DRIVE_RE = re.compile(r"^[A-Za-z]:[\\/]")


def _windows_like(path: str) -> bool:
    return bool(_DRIVE_RE.match(path)) or "\\" in path


def _windows_norm(path: str) -> str:
    return path.replace("\\", "/").rstrip("/").casefold()


def _same_repo(a: str, b: str) -> bool:
    """Do these two host paths name the same checkout?

    Compared after ``realpath`` so a symlinked or differently-spelled route to
    the same directory still counts as one repo. An empty path matches nothing:
    a node with no path on this device (a virtual cluster, or a node not cloned
    here) must not collide with every other node that also has none.
    """
    if not a or not b:
        return False
    # A Windows host path (``X:\srv\X`` from the local file, ``X:/srv/X`` from
    # the graph) names a directory the container cannot stat, and on Linux
    # ``realpath`` treats the backslash as part of a file name: the two
    # spellings never matched and every node was listed twice.
    if _windows_like(a) or _windows_like(b):
        return _windows_norm(a) == _windows_norm(b)
    try:
        return os.path.realpath(a) == os.path.realpath(b)
    except OSError:  # pragma: no cover - realpath on a hostile path
        return False


def _merge_local_into_live(
    live: tuple[Project, ...],
    local: tuple[Project, ...],
    *,
    montato=None,
) -> tuple[Project, ...]:
    """Live registry first, local file as a supplement — merged by host path.

    Two entries pointing at the same checkout are one project under two names.
    The graph's name wins (it is the one the rest of the network uses) and the
    local one is demoted to an alias, so both spellings keep resolving. The
    *workspace* is whichever of the two is actually mounted, preferring the
    local one on a tie: the graph knows what a node is called, only this
    machine knows where it is mounted. Asking instead of assuming matters in
    both directions — a repo mounted under its old name would be dropped if we
    insisted on the graph id, and a local entry gone stale (its repo moved:
    ``offerte-pubbliche`` -> ``VetrinaOffertePubbliche``) would otherwise drag
    a perfectly live node down with it.

    Local entries matching nothing in the graph are appended unchanged. That is
    the whole point — the ``~/.claude`` config tree and the other instance's
    personal nodes are real workspaces that no Life graph reachable from here
    will ever list.
    """
    merged: list[Project] = []
    claimed: set[int] = set()
    for node in live:
        extra_aliases: list[str] = []
        workspace = node.workspace
        session_globs = node.session_globs
        etichetta = ""
        emoji = ""
        if not node.host_path:
            # The graph knows the node but not where it lives on THIS device
            # (its paths come from another instance's DB): a local entry with
            # the same key supplies the path, then merges as usual below.
            # Without it host-exec saw an empty path and stayed in-container.
            for entry in local:
                if entry.key == node.key and entry.host_path:
                    node = dataclasses.replace(node, host_path=entry.host_path)
                    break
        for index, entry in enumerate(local):
            if index in claimed or not _same_repo(node.host_path, entry.host_path):
                continue
            claimed.add(index)
            etichetta = etichetta or entry.label
            emoji = emoji or entry.emoji
            extra_aliases.append(entry.key)
            extra_aliases.extend(entry.aliases)
            # The old workspace also names the old transcript directory, so it
            # has to stay findable: keep it as a session glob or `/riprendi`
            # loses every conversation held before the rename.
            preferisci_locale = (
                bool(entry.workspace)
                and entry.workspace != node.workspace
                and (
                    montato is None
                    or montato(entry.workspace)
                    or not montato(node.workspace)
                )
            )
            if preferisci_locale:
                workspace = entry.workspace
                session_globs = tuple(
                    dict.fromkeys(
                        (*session_globs, *entry.session_globs, node.workspace)
                    )
                )
            else:
                session_globs = tuple(
                    dict.fromkeys(
                        (
                            *session_globs,
                            *entry.session_globs,
                            *([entry.workspace] if entry.workspace else []),
                        )
                    )
                )
        if extra_aliases:
            aliases = tuple(
                dict.fromkeys(
                    a for a in (*node.aliases, *extra_aliases) if a != node.key
                )
            )
            node = dataclasses.replace(
                node,
                aliases=aliases,
                workspace=workspace,
                session_globs=session_globs,
                # Etichetta ed emoji restano quelle scelte a mano: sono per il
                # menu, cioe' per una persona, mentre quelle del grafo sono
                # generate (il nodo di cui sopra si chiamava
                # "VetrinaOffertePubbliche"). Il nome canonico e' la `key`, e
                # quella viene dal grafo — e' l'unica che conta sulla rete.
                label=etichetta or node.label,
                emoji=emoji or node.emoji,
            )
        merged.append(node)
    merged.extend(entry for i, entry in enumerate(local) if i not in claimed)
    return tuple(merged)


def load_projects_config_path() -> Path:
    """Path to the per-device manual override file, honoring the env override."""
    env_path = os.environ.get("COORDINATOR_PROJECTS_CONFIG")
    return Path(env_path) if env_path else Path(__file__).parent / "projects.local.json"


def load_projects() -> tuple[Project, ...]:
    """The registry from :func:`_load_projects_base`, keyed by the bus identities (#81)."""
    return apply_derived_agents(_load_projects_base())


def apply_derived_agents(
    projects_in: tuple[Project, ...], derived=None
) -> tuple[Project, ...]:
    """Make every identity Life computed for this device resolve to a project.

    The listeners come from ``bus_agents`` (graph id + device suffix), so the
    project key a wake carries is the **graph id**. Whatever the other sources
    called the node (``offerte``, ``call-briefing``), the project pointing at
    the same checkout takes the graph id as key and keeps the old name as an
    alias — Telegram commands typed with it keep working. A node no other source
    knows (``pensaDigitaleBot`` on 2026-10-15) is added from its ``path``:
    with host execution its wake runs on the host there, so the container mount
    is not required. Without host execution an unmounted one would open
    nothing, and it stays out.
    """
    import bus_agents

    result = bus_agents.derived() if derived is None else derived
    if not result:
        return projects_in
    agents = result[0] if isinstance(result, tuple) else result
    out = list(projects_in)
    host_exec = bool(os.environ.get("HOST_EXEC_SSH", "").strip())
    for agent in agents:
        index = next(
            (i for i, p in enumerate(out) if p.key == agent.graph_id), None
        )
        if index is None:
            index = next(
                (
                    i
                    for i, p in enumerate(out)
                    if p.host_path and _same_repo(p.host_path, agent.path)
                ),
                None,
            )
        if index is not None:
            p = out[index]
            if p.key != agent.graph_id:
                aliases = tuple(
                    dict.fromkeys(
                        a
                        for a in (p.key, *([p.slug] if p.slug else []), *p.aliases)
                        if a != agent.graph_id
                    )
                )
                p = dataclasses.replace(p, key=agent.graph_id, slug="", aliases=aliases)
            if not p.host_path:
                p = dataclasses.replace(p, host_path=agent.path)
            out[index] = p
            continue
        workspace = f"/workspaces/{agent.graph_id}"
        if not host_exec and not os.path.isdir(workspace):
            logger.info(
                "agent %s: no project and %s not mounted (no host exec) — left out",
                agent.name,
                workspace,
            )
            continue
        out.append(
            Project(
                key=agent.graph_id,
                label=agent.graph_id,
                emoji="🔹",
                host_path=agent.path,
                workspace=workspace,
                aliases=tuple(agent.former),
                kind="personal" if "personal" in agent.tenant else "work",
            )
        )
    # Two entries for one key would make resolve_project's answer depend on order.
    seen: set[str] = set()
    unique = []
    for p in out:
        if p.key in seen:
            continue
        seen.add(p.key)
        unique.append(p)
    return tuple(unique)


def _load_projects_base() -> tuple[Project, ...]:
    """Load the project registry.

    Two sources, and the local one SUPPLEMENTS the live one rather than
    replacing it (issue #35). Life's graph is authoritative for the nodes it
    knows: it carries the canonical id and it updates itself. But it only ever
    describes one instance, so a handful of real workspaces can never be in it
    — the ``~/.claude`` config tree is not a Life node at all, and the personal
    nodes belong to the other instance, whose backend does not run here. The
    old all-or-nothing rule forced a choice between those two halves: keep the
    hand-written file and freeze the registry, or drop it and lose whatever
    Life does not know. The container took the second branch, and that is why
    wakes for ``claude-dev-linux`` had nowhere to land.

    Merging is done on the **host path**, not on the key, because that is the
    thing that actually identifies a repo. A local entry pointing at a repo the
    graph already describes is not a second project: it is this device's older
    name for the same one, so its key becomes an *alias* of the graph node and
    its workspace (the mount that really exists here) is kept. A local entry
    the graph does not know is appended as its own project. That gives the
    historic bus keys — ``db``, ``etl``, ``offerte`` — a resolution to
    ``dbAcme``/``etlV2``/``hubOfferte`` without a hand-written mapping table
    that would rot the day a node is renamed.

    Fallback chain for the live half is unchanged: API -> disk cache ->
    ``_DEFAULT_PROJECTS``.
    """
    global _REGISTRY_SOURCE
    config_path = load_projects_config_path()
    live: tuple[Project, ...]
    # When a local file is going to supplement the live registry, keep the
    # nodes whose `/workspaces/<id>` is not mounted: the merge may well hand
    # them the mount this device has under an older name, and the check is
    # redone below on the workspace that was actually assigned.
    supplement = config_path.is_file()
    try:
        live = life_registry_client.load_projects_from_life(
            fallback=_DEFAULT_PROJECTS, test_mounts=not supplement
        )
    except Exception:
        # A registry that FAILED to load is not a registry that came back
        # empty, and the difference is the whole of issue #35: an
        # AttributeError swallowed into `return fallback` reads, downstream,
        # as "this device really has 8 projects". Say it at ERROR, with the
        # traceback, and say which list is being served instead.
        logger.error(
            "life registry failed to load; serving the %d hardcoded "
            "_DEFAULT_PROJECTS instead - most wake events for other nodes "
            "will be dropped as 'unknown project'",
            len(_DEFAULT_PROJECTS),
            exc_info=True,
        )
        live = _DEFAULT_PROJECTS
    live_ok = live is not _DEFAULT_PROJECTS

    if config_path.is_dir():
        # Docker bind-mounts a MISSING source file as an empty directory: on a
        # device without projects.local.json the compose mount leaves one here.
        # Nobody wrote it, so it is no config — reading it would crash every
        # registry load, and with it every wake.
        logger.warning(
            "%s is a directory (docker mount of a missing file?) — ignored, "
            "no local supplement on this device",
            config_path,
        )
    if not config_path.is_file():
        _REGISTRY_SOURCE = _SOURCE_LIFE if live_ok else _SOURCE_DEFAULTS
        return live

    config_str = str(config_path)
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        logger.error("invalid project config %s: %s", config_str, exc)
        raise ValueError(f"{config_str}: invalid JSON ({exc})") from exc

    entries = raw.get("projects") if isinstance(raw, dict) else None
    if not isinstance(entries, list) or not entries:
        logger.error(
            "invalid project config %s: 'projects' must be a non-empty list", config_str
        )
        raise ValueError(f"{config_str}: 'projects' must be a non-empty list")

    projects_out: list[Project] = []
    for index, entry in enumerate(entries):
        try:
            projects_out.append(
                _project_from_dict(entry, config_path=config_str, index=index)
            )
        except ValueError as exc:
            logger.error("%s", exc)
            raise

    if not live_ok:
        # No live registry to merge into: the local file IS the registry, which
        # is the historic behaviour and the right one here — merging into the
        # hardcoded defaults would dress a failure up as a richer answer.
        _REGISTRY_SOURCE = _SOURCE_LOCAL_FILE
        return tuple(projects_out)
    _REGISTRY_SOURCE = _SOURCE_MERGED
    merged, unmounted = life_registry_client.drop_unmounted(
        _merge_local_into_live(
            live, tuple(projects_out), montato=life_registry_client.workspace_montato
        )
    )
    if unmounted:
        # Named, not counted: a project missing from the menu because nobody
        # mounted its repo looks exactly like a project that was deleted.
        logger.info(
            "%d project(s) left out of the registry — %s",
            len(unmounted),
            "; ".join(f"{key} ({why})" for key, why in unmounted),
        )
    return merged


# Which source actually produced the registry currently in memory. Recorded by
# `load_projects()` and reported by `registry_source()`, so a log line about the
# registry can name the file it really read instead of guessing (issue #35: the
# drift warning blamed projects.local.json, a file deliberately not mounted,
# sending everyone to look in the wrong place).
_SOURCE_LOCAL_FILE = "local_file"
_SOURCE_MERGED = "merged"
_SOURCE_LIFE = "life"
_SOURCE_DEFAULTS = "defaults"
_REGISTRY_SOURCE: str | None = None


def registry_source() -> str:
    """Human-readable description of where the active registry came from."""
    if _REGISTRY_SOURCE == _SOURCE_LOCAL_FILE:
        return f"{load_projects_config_path()} (override manuale)"
    if _REGISTRY_SOURCE == _SOURCE_MERGED:
        return (
            f"il registro live di Life + {load_projects_config_path()} "
            "(supplemento per i workspace che Life non conosce)"
        )
    if _REGISTRY_SOURCE == _SOURCE_LIFE:
        return "il registro live di Life"
    if _REGISTRY_SOURCE == _SOURCE_DEFAULTS:
        return "_DEFAULT_PROJECTS (ripiego: il registro non si e' caricato)"
    return "una fonte non ancora determinata (registro non caricato)"


def get_projects() -> tuple[Project, ...]:
    """The active registry, built on first use - never at import time.

    Import-time construction is what made the ordering of two imports decide
    whether this bot knew 8 projects or 30 (issue #35). Loading on demand means
    no module's import can be half-finished while we run, whatever the order.

    The result is memoised in the module global ``PROJECTS``, which stays the
    public name: `projects.PROJECTS` still works (see ``__getattr__``) and tests
    still pin it with ``monkeypatch.setattr``.
    """
    global PROJECTS
    if "PROJECTS" not in globals():
        PROJECTS = load_projects()
    return PROJECTS


def __getattr__(name: str):
    # PEP 562: only reached when the global is absent, i.e. before the first
    # load. Once set (by us or by a test's monkeypatch) the normal attribute
    # wins and this costs nothing.
    if name == "PROJECTS":
        return get_projects()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def refresh_projects() -> dict[str, list[str]]:
    """Recompute PROJECTS from Life's live registry and swap it in.

    No-op (returns an empty diff) when a manual ``projects.local.json``
    override is active — that path is static by design, so refreshing it
    would silently discard an operator's explicit choice. Returns which
    project keys were added/removed since the previous value, for logging.
    """
    global PROJECTS
    if load_projects_config_path().exists():
        return {"added": [], "removed": []}
    import life_registry_client
    new_projects = apply_derived_agents(
        life_registry_client.load_projects_from_life(fallback=_DEFAULT_PROJECTS)
    )
    old_keys = {p.key for p in get_projects()}
    new_keys = {p.key for p in new_projects}
    PROJECTS = new_projects
    return {
        "added": sorted(new_keys - old_keys),
        "removed": sorted(old_keys - new_keys),
    }


def escape_path(p: str) -> str:
    """Claude Code project-dir name: replace ``:`` ``\\`` ``/`` with ``-``."""
    return re.sub(r"[:\\/]", "-", p)


def list_projects() -> list[Project]:
    return list(get_projects())


# ---------------------------------------------------------------------------
# Life graph drift check
# ---------------------------------------------------------------------------
# `projects.local.json` is a hand-maintained, per-device registry — a different
# system from the Life fractal network's own taxonomy (`life.graph.json`, the
# node.config chain). There is no live sync between the two; this only *detects*
# drift so a newly-forged Life node doesn't silently stay unreachable from the
# notification form / bot until someone remembers to add it here by hand.
_LIFE_GRAPH_SKIP_KINDS = frozenset({"root", "cluster"})
# "system" is the System cluster's own id (kind="system", like its children
# coordinator/network/launcher) but is itself an organisational grouping, not a
# selectable project — unlike e.g. "homeResearcher", which keeps being a real
# node even after gaining children (rentFinder, buyFinder) in the taxonomy.
_LIFE_GRAPH_SKIP_IDS = frozenset({"system"})


def _default_life_graph_path() -> Path:
    # Coordinator and Life are sibling repos under $SRV_ROOT on this device (see
    # CLAUDE.md "Rete frattale Life"). Other devices without a local Life clone
    # simply won't find this path — that's fine, the check degrades to a no-op.
    return Path(__file__).resolve().parent.parent / "Life" / "life.graph.json"


def life_graph_drift(graph_path: str | Path | None = None) -> dict[str, list[str]] | None:
    """Compare this registry's keys against active nodes in Life's `life.graph.json`.

    Returns ``None`` when the graph file isn't reachable from this device (no
    local Life clone, or unreadable) — that's an expected, silent no-op, not an
    error. Otherwise returns:
    - ``missing_in_coordinator``: Life nodes marked ``exists`` that have no
      matching project key here — these are the ones actually worth flagging,
      since they can't be picked in the notification form / bot until added.
    - ``not_in_life_graph``: project keys with no matching Life node — expected
      for work instance/infra projects and any node not yet reflected in the graph;
      informational only.
    """
    path = Path(graph_path) if graph_path else Path(
        os.environ.get("LIFE_GRAPH_PATH") or _default_life_graph_path()
    )
    if not path.exists():
        return None
    try:
        graph = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("life_graph_drift: unreadable %s: %s", path, exc)
        return None

    nodes = graph.get("nodes", {})
    if not isinstance(nodes, dict):
        return None
    active_ids = {
        node_id
        for node_id, node in nodes.items()
        if isinstance(node, dict)
        and node.get("exists")
        and node.get("kind") not in _LIFE_GRAPH_SKIP_KINDS
        and node_id not in _LIFE_GRAPH_SKIP_IDS
    }
    # Confronto per PROGETTO RISOLTO, non per stringa: Life e il Coordinator
    # nominano diversamente le stesse cose (`hubOfferte` vs `offerte`,
    # `portaleSegnalazioni` vs `segnalatori`, `etlV2` vs `etl`). Un confronto
    # secco fra insiemi di stringhe segnalerebbe come "mancanti" progetti che
    # esistono, seppellendo quelli davvero assenti nel rumore. `resolve_project`
    # passa dagli alias, quindi basta che l'id di Life sia fra gli alias.
    matched: set[str] = set()
    missing: list[str] = []
    for node_id in active_ids:
        proj = resolve_project(node_id)
        if proj is None:
            missing.append(node_id)
        else:
            matched.add(proj.key)
    # I nodi del FRAMEWORK inclusi dall'istanza (coordinator, network, launcher…)
    # non stanno in `nodes`: `GET /api/graph` serve il file grezzo e non risolve
    # `includeFramework` a runtime — la composizione avviene a valle, in build.
    # Compaiono solo fra gli `overrides`, che è il modo in cui l'istanza
    # personalizza un nodo che dichiara di includere. Trattarli come assenti
    # produrrebbe tre falsi positivi permanenti.
    for over_id in graph.get("overrides", {}) or {}:
        proj = resolve_project(over_id)
        if proj is not None:
            matched.add(proj.key)

    # Solo i progetti `work`: questo grafo è l'istanza work instance di Life, e i
    # personali vivono in un'altra istanza (che su questo host non esiste).
    # Elencarli qui li farebbe sembrare deriva ogni volta, seppellendo le
    # differenze reali sotto un rumore che nessuno può risolvere.
    our_keys = {p.key for p in get_projects() if p.kind == "work"}
    return {
        "missing_in_coordinator": sorted(missing),
        "not_in_life_graph": sorted(our_keys - matched),
    }


# Top-level menu groups, in display order. ``.claude config`` lives under work.
GROUPS: tuple[tuple[str, str], ...] = (
    ("work", "💼 Lavoro"),
    ("personal", "🏠 Personali"),
)


def projects_in(kind: str) -> list[Project]:
    """Projects belonging to one menu group, preserving registry order."""
    return [p for p in get_projects() if p.kind == kind]


def resolve_project(token: str | None) -> Project | None:
    if not token:
        return None
    t = token.strip().lstrip("/").lower()
    for p in get_projects():
        # Case-insensitive on key + aliases so the canonical camelCase slug
        # (e.g. "personalChef") resolves from any casing and from its env-derived
        # value in BOT_LOCAL_AGENTS (regola d'oro Life #0: un nodo, un nome).
        if (
            t == p.key.lower()
            or (p.slug and t == p.slug.lower())
            or t in (a.lower() for a in p.aliases)
        ):
            return p
    return None


@dataclass(frozen=True)
class Agent:
    name: str
    description: str
    persona: str | None  # body of the .md; None => Generale (uses project CLAUDE.md)
    label: str | None = None  # human-readable button text (frontmatter `label:`)
    emoji: str | None = None  # frontmatter `emoji:`
    keywords: tuple[str, ...] = ()  # frontmatter `keywords:`; intent-routing triggers


_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n?", re.DOTALL)
_DESC_RE = re.compile(r"^description:\s*(.+)$", re.MULTILINE)
_LABEL_RE = re.compile(r"^label:\s*(.+)$", re.MULTILINE)
_EMOJI_RE = re.compile(r"^emoji:\s*(.+)$", re.MULTILINE)
_KEYWORDS_RE = re.compile(r"^keywords:\s*(.+)$", re.MULTILINE)

# cache: workspace -> (agents_dir_mtime, agents)
_AGENTS_CACHE: dict[str, tuple[float, list["Agent"]]] = {}

GENERALE = Agent(
    "generale",
    "Assistente principale del progetto",
    None,
    label="Generale",
    emoji="\U0001f4ac",
)


def _parse_agent_md(text: str, name: str) -> Agent:
    m = _FRONTMATTER_RE.match(text)
    label = emoji = None
    if m:
        fm, body = m.group(1), text[m.end() :]
        dm = _DESC_RE.search(fm)
        desc = dm.group(1).strip().strip('"').strip("'") if dm else name
        lm = _LABEL_RE.search(fm)
        if lm:
            label = lm.group(1).strip().strip('"').strip("'")
        em = _EMOJI_RE.search(fm)
        if em:
            emoji = em.group(1).strip().strip('"').strip("'")
        km = _KEYWORDS_RE.search(fm)
        keywords = _parse_keywords(km.group(1)) if km else ()
    else:
        desc, body = name, text
        keywords = ()
    desc = desc.split(". ")[0][:80]  # first sentence, capped
    return Agent(
        name=name,
        description=desc,
        persona=body.strip() or None,
        label=label,
        emoji=emoji,
        keywords=keywords,
    )


def _parse_keywords(raw: str) -> tuple[str, ...]:
    """Frontmatter ``keywords:`` -> normalised tuple. Accepts a comma-separated
    list or a YAML inline list ``[a, b]``; each entry lowercased and stripped."""
    raw = raw.strip().strip('"').strip("'")
    if raw.startswith("[") and raw.endswith("]"):
        raw = raw[1:-1]
    out: list[str] = []
    for part in raw.split(","):
        kw = part.strip().strip('"').strip("'").lower()
        if kw:
            out.append(kw)
    return tuple(dict.fromkeys(out))  # dedupe, keep order


def agents_of(project: Project) -> list[Agent]:
    """Generale + the project's subagents, discovered from .claude/agents/*.md.
    Cached per workspace, invalidated on the agents dir mtime."""
    agents_dir = os.path.join(project.workspace, ".claude", "agents")
    try:
        entries = sorted(fn for fn in os.listdir(agents_dir) if fn.endswith(".md"))
    except OSError:
        return [GENERALE]
    sig = os.path.getmtime(agents_dir)
    for fn in entries:
        try:
            sig = max(sig, os.path.getmtime(os.path.join(agents_dir, fn)))
        except OSError:
            continue
    cached = _AGENTS_CACHE.get(project.workspace)
    if cached and cached[0] == sig:
        return cached[1]
    found: list[Agent] = []
    for fn in entries:
        path = os.path.join(agents_dir, fn)
        try:
            with open(path, encoding="utf-8") as fh:
                text = fh.read()
        except OSError:
            continue
        found.append(_parse_agent_md(text, fn[:-3]))
    agents = [GENERALE] + found
    _AGENTS_CACHE[project.workspace] = (sig, agents)
    return agents


def resolve_agent(project: Project, name: str | None) -> Agent | None:
    if not name or not name.strip():
        return GENERALE
    n = name.strip().lower()
    for a in agents_of(project):
        if a.name.lower() == n:
            return a
    return None


_WORD_RE = re.compile(r"\w+", re.UNICODE)


def resolve_agent_by_intent(project: Project, text: str, default: Agent) -> Agent:
    """Pick the specialised agent whose ``keywords`` best match ``text``.

    The topic-bound ``default`` is the fallback: it wins on no match and on ties
    (so an explicit binding is never silently overridden by a weak signal). Only
    agents that declare ``keywords`` can be selected here; ``Generale`` never is.
    """
    if not text:
        return default
    lowered = text.lower()
    words = set(_WORD_RE.findall(lowered))

    def score(a: Agent) -> int:
        n = 0
        for kw in a.keywords:
            if " " in kw or "&" in kw:  # multi-token / non-\w keyword -> substring
                if kw in lowered:
                    n += 1
            elif kw in words:  # single token -> word-boundary match
                n += 1
        return n

    best = default
    best_score = score(default)
    for a in agents_of(project):
        if not a.keywords:
            continue
        s = score(a)
        if s > best_score:  # strict '>' => default keeps ties
            best, best_score = a, s
    if best_score > 0:
        return best
    # Nessuna keyword: chi si presenta offrendo una competenza va al settore
    # pertinente (ComUnity#178), se il progetto ha quell'agente. Solo sul
    # default Generale: una scheda legata a un agente non si scavalca.
    settore = settore_per_competenza(text) if default.name == GENERALE.name else None
    if settore:
        for a in agents_of(project):
            if a.name.lower() == settore:
                return a
    return default


def agent_button_label(a: Agent) -> str:
    """Human-readable button text: ``emoji label`` when annotated, else the slug."""
    if a.label:
        return f"{a.emoji} {a.label}" if a.emoji else a.label
    return a.name


# ---------------------------------------------------------------------------
# Task 7: Session discovery
# ---------------------------------------------------------------------------

PROJECTS_ROOT = os.environ.get(
    "CLAUDE_PROJECTS_DIR", os.path.join(os.path.expanduser("~"), ".claude", "projects")
)


@dataclass(frozen=True)
class SessionRef:
    id: str
    source: str  # "PC" | "TG"
    title: str
    agent: str | None
    mtime: float


def container_project_dir(project: Project) -> str:
    return escape_path(project.workspace)


def _candidate_dirs(project: Project) -> list[tuple[str, str]]:
    """Return (abs_dir, source) for project-dirs belonging to this project."""
    cdir = container_project_dir(project).lower()
    # A project with no host path has no PC transcripts to find — and an empty
    # prefix is a prefix of everything, so matching on it would hand every
    # session directory on the box to every project. The live registry makes
    # this the normal case, not a corner one: a node the Life graph has no
    # per-device path for arrives here with host_path "".
    hpref = escape_path(project.host_path).lower()
    globs = tuple(g.lower() for g in project.session_globs)
    out: list[tuple[str, str]] = []
    try:
        names = os.listdir(PROJECTS_ROOT)
    except OSError:
        return out
    for name in names:
        abs_dir = os.path.join(PROJECTS_ROOT, name)
        if not os.path.isdir(abs_dir):
            continue
        ln = name.lower()
        if ln == cdir:
            out.append((abs_dir, "TG"))
        elif (hpref and ln.startswith(hpref)) or any(
            fnmatch.fnmatch(ln, g) for g in globs
        ):
            out.append((abs_dir, "PC"))
    return out


def _read_session_meta(path: str) -> tuple[str | None, str | None]:
    """(title, agent) from header records; fall back to first user message."""
    title = agent = first_user = None
    try:
        with open(path, encoding="utf-8") as fh:
            for i, line in enumerate(fh):
                if i >= 200:
                    break
                if i > 60 and (title or first_user):
                    break
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                t = d.get("type")
                if t == "custom-title" and d.get("customTitle"):
                    title = d["customTitle"]
                elif t == "agent-name" and d.get("agentName"):
                    agent = d["agentName"]
                elif t == "user" and first_user is None:
                    first_user = _text_of((d.get("message") or {}).get("content"))
    except OSError:
        pass
    return (title or first_user), agent


# Harness bookkeeping that shows up as the FIRST user message of a transcript
# (a session opened with /clear, /model, …). Titling a resume button with it
# gives a list of identical "<local-command-caveat>Caveat: The messa…" rows, so
# these are skipped in favour of the first message the human actually typed.
_NOISE_PREFIXES = (
    "<local-command-caveat>",
    "<command-name>",
    "<command-message>",
    "<command-args>",
    "<local-command-stdout>",
    "<system-reminder>",
    "caveat: the messages below were generated",
)


def _is_noise(text: str | None) -> bool:
    if not text:
        return True
    return text.lstrip().lower().startswith(_NOISE_PREFIXES)


def _text_of(content) -> str | None:
    """First human-readable text block, skipping harness bookkeeping."""
    if isinstance(content, str):
        stripped = content.strip()
        return None if _is_noise(stripped) else (stripped[:60] or None)
    if isinstance(content, list):
        for b in content:
            if isinstance(b, dict) and b.get("type") == "text" and b.get("text"):
                stripped = b["text"].strip()
                if _is_noise(stripped):
                    continue
                return stripped[:60]
    return None


def sessions_of(project: Project, limit: int = 10) -> list[SessionRef]:
    seen_best: dict[str, tuple[float, str]] = {}
    for abs_dir, source in _candidate_dirs(project):
        for fn in os.listdir(abs_dir):
            if not fn.endswith(".jsonl"):
                continue
            sid = fn[:-6]
            path = os.path.join(abs_dir, fn)
            try:
                mtime = os.path.getmtime(path)
            except OSError:
                continue
            prev = seen_best.get(sid)
            # dedup: keep TG over PC; otherwise newest
            if (
                prev is None
                or (source == "TG" and prev[1] != "TG")
                or (prev[1] == source and mtime > prev[0])
            ):
                seen_best[sid] = (mtime, source)
    ranked = sorted(((m, sid, s) for sid, (m, s) in seen_best.items()), reverse=True)[
        :limit
    ]
    out: list[SessionRef] = []
    for mtime, sid, source in ranked:
        path = _find_session_file(project, sid, source)
        title, agent = _read_session_meta(path) if path else (None, None)
        out.append(
            SessionRef(
                id=sid, source=source, title=title or sid[:8], agent=agent, mtime=mtime
            )
        )
    return out


def _find_session_file(project: Project, sid: str, source: str) -> str | None:
    for abs_dir, src in _candidate_dirs(project):
        if src != source:
            continue
        p = os.path.join(abs_dir, f"{sid}.jsonl")
        if os.path.exists(p):
            return p
    return None


# ---------------------------------------------------------------------------
# Task 8: Resume preparation
# ---------------------------------------------------------------------------


def prepare_resume(project: Project, sref: SessionRef) -> str:
    """Ensure the session transcript is in the container project-dir, return its id."""
    if sref.source == "TG":
        return sref.id
    src = _find_session_file(project, sref.id, "PC")
    if not src:
        logger.warning(
            "prepare_resume: no PC transcript for session %s in project %s",
            sref.id,
            project.key,
        )
        return sref.id
    dst_dir = os.path.join(PROJECTS_ROOT, container_project_dir(project))
    os.makedirs(dst_dir, exist_ok=True)
    dst = os.path.join(dst_dir, f"{sref.id}.jsonl")
    if not os.path.exists(dst):
        shutil.copy2(src, dst)
    return sref.id
