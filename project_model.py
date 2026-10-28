# project_model.py
"""The ``Project`` record, alone in a module that imports nothing of ours.

Both ``projects`` (the registry) and ``life_registry_client`` (one of the
sources that fills it) need this type. When it lived in ``projects`` the two
modules pointed at each other, and because ``projects`` *ran* the client at
import time the cycle was not the harmless kind: whichever module Python
happened to import first decided whether the registry loaded at all. Importing
``life_registry_client`` first left it half-built at its own ``from projects
import Project`` line, so ``projects`` found no ``load_projects_from_life`` on
it and fell back to the hardcoded list — silently, and only on some boots
(issue #35).

A leaf module with no back-edge makes that ordering question disappear rather
than answering it: nothing here imports either side, so there is no cycle left
to order.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Project:
    key: str
    label: str
    emoji: str
    host_path: str  # Windows path (where PC transcripts were recorded)
    workspace: str  # container cwd for `claude -p`
    aliases: tuple[str, ...] = ()
    session_globs: tuple[str, ...] = ()  # extra case-insensitive fnmatch patterns
    # for host project-dir names
    kind: str = "work"  # top-level menu group: "work" | "personal"
    # The node's slug in the Life graph, i.e. its name on the bus (`<slug>-<device>`,
    # Network#42). Empty = the key already is the slug: always true for the live
    # registry (key = graph id), not for the static lists, whose historic keys
    # (`offerte`, `etl`, `db`) only survive on the bus as aliases (#79).
    slug: str = ""

    @property
    def bus_slug(self) -> str:
        """The name the bus knows this project by, before the device suffix."""
        return self.slug or self.key
