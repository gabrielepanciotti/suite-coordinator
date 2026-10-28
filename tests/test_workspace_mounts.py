"""A project whose workspace isn't mounted must be loud, not silent.

A node once lost its whole Coordinator manifest to this: `projects.local.json`
declared the workspace in camelCase, the compose file mounted it lowercase.
Docker creates the target directory regardless, so the container starts clean
and nothing logs an error — the bind simply lands next to where anyone looks.
On Windows it would have worked by accident; on the Linux minipc the group
binding, the pinned message and every capability of that node went quietly
missing.

Two guards, because they catch it at different moments:
  - the compose file itself may never carry two spellings of one path;
  - at startup, whatever the device, an unreachable workspace gets named.
"""

from __future__ import annotations

import re

import pytest
from pathlib import Path

import bot
import projects

REPO = Path(__file__).resolve().parent.parent
COMPOSE = REPO / "docker-compose.yml"

_MOUNT = re.compile(r"^\s*-\s*(?P<source>\S+?):(?P<target>/workspaces/[^:\s]+)")


def _mount_targets() -> list[str]:
    """Mount targets under /workspaces, read as text.

    Deliberately not via a YAML parser: the values carry ``${VAR:-default}``
    interpolation, and the point here is the literal path as written.
    """
    return [
        m.group("target")
        for line in COMPOSE.read_text(encoding="utf-8").splitlines()
        if (m := _MOUNT.match(line))
    ]


def test_no_two_workspace_mounts_differ_only_by_case():
    """On Linux these are two directories; on Windows/macOS they collide. Either
    way one of the two spellings is a typo nobody will see."""
    if not COMPOSE.is_file():
        pytest.skip("workspace mount spelling is checked in the excluded production compose file")
    by_lower: dict[str, set[str]] = {}
    for t in _mount_targets():
        by_lower.setdefault(t.lower(), set()).add(t)
    clashes = {k: sorted(v) for k, v in by_lower.items() if len(v) > 1}
    assert not clashes, f"mount targets differing only by case: {clashes}"


def _project(key: str, workspace: str) -> projects.Project:
    return projects.Project(
        key=key, label=key, emoji="🧪", host_path=f"/hosts/{key}", workspace=workspace
    )


def test_audit_is_quiet_when_every_workspace_exists(monkeypatch, tmp_path):
    (tmp_path / "demoNode").mkdir()
    monkeypatch.setattr(
        bot, "list_projects", lambda: [_project("demoNode", str(tmp_path / "demoNode"))]
    )
    assert bot.workspace_audit() == []


def test_audit_names_the_case_variant_that_is_actually_mounted(monkeypatch, tmp_path):
    """The real failure: the directory is there, spelled differently.

    Only reproducible where the filesystem tells the two apart. This is the one
    assertion that genuinely needs a case-sensitive FS — the bug exists precisely
    because Windows hides it — so the condition is detected rather than assumed
    from the platform name (a case-sensitive volume on Windows, or a
    case-insensitive one on macOS, are both real).
    """
    (tmp_path / "aB").mkdir()
    if (tmp_path / "ab").exists():
        pytest.skip("case-insensitive filesystem: cannot stage the mismatch here")

    (tmp_path / "demonode").mkdir()
    declared = str(tmp_path / "demoNode")
    monkeypatch.setattr(bot, "list_projects", lambda: [_project("demoNode", declared)])

    assert bot.workspace_audit() == [("demoNode", declared, "demonode")]


def test_audit_reports_a_plainly_absent_workspace_without_a_variant(monkeypatch, tmp_path):
    declared = str(tmp_path / "nowhere")
    monkeypatch.setattr(bot, "list_projects", lambda: [_project("ghost", declared)])

    assert bot.workspace_audit() == [("ghost", declared, None)]
