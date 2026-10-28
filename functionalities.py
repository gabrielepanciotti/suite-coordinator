# functionalities.py
"""Human-readable functionality layer over a project's raw agents.

A *Functionality* is a friendly Telegram menu entry bound to one primary agent
(plus optional companions and an opening ``seed`` prompt). Each project
self-describes its functionalities in ``<workspace>/.claude/functionalities.toml``;
the Coordinator discovers them generically, mirroring ``projects.agents_of``.

Pure logic — no Telegram imports — so it unit-tests like ``projects.py``.
"""
from __future__ import annotations

import logging
import os
import tomllib
from dataclasses import dataclass

from projects import Project

logger = logging.getLogger("coordinator.functionalities")


@dataclass(frozen=True)
class Functionality:
    key: str
    label: str
    agent: str                       # primary agent slug
    companions: tuple[str, ...] = ()
    seed: str = ""


# cache: workspace -> (toml_mtime, functionalities)
_CACHE: dict[str, tuple[float, list["Functionality"]]] = {}


def _toml_path(project: Project) -> str:
    return os.path.join(project.workspace, ".claude", "functionalities.toml")


def functionalities_of(project: Project) -> list[Functionality]:
    """Functionalities declared in the project's ``functionalities.toml``.

    Returns ``[]`` if the file is absent, unreadable, or malformed (logged).
    Cached per workspace, invalidated on the file mtime.
    """
    path = _toml_path(project)
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return []
    cached = _CACHE.get(project.workspace)
    if cached and cached[0] == mtime:
        return cached[1]
    try:
        with open(path, "rb") as fh:
            data = tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        logger.warning("functionalities.toml unreadable for %s: %s", project.key, exc)
        return []
    out: list[Functionality] = []
    for i, entry in enumerate(data.get("functionality", [])):
        key = str(entry.get("key", "")).strip()
        label = str(entry.get("label", "")).strip()
        agent = str(entry.get("agent", "")).strip()
        if not (key and label and agent):
            logger.warning(
                "functionality #%d in %s missing key/label/agent — skipped",
                i, project.key,
            )
            continue
        companions = tuple(
            str(c).strip() for c in entry.get("companions", []) if str(c).strip()
        )
        seed = str(entry.get("seed", ""))
        out.append(Functionality(key, label, agent, companions, seed))
    _CACHE[project.workspace] = (mtime, out)
    return out


def has_functionalities(project: Project) -> bool:
    """True when the project declares at least one valid functionality."""
    return bool(functionalities_of(project))


def resolve_functionality(project: Project, key: str | None) -> Functionality | None:
    """Lookup a functionality by key (case-insensitive), or ``None``."""
    if not key or not key.strip():
        return None
    k = key.strip().lower()
    for f in functionalities_of(project):
        if f.key.lower() == k:
            return f
    return None
