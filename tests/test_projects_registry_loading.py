# tests/test_projects_registry_loading.py
"""How the project registry is loaded — the failure mode of issue #35.

`projects` built its registry at import time by calling `life_registry_client`,
which imported `projects` back for the `Project` type. Whichever of the two
Python imported first decided whether the bot knew 30 projects or the 8
hardcoded defaults, and the difference showed up nowhere: the AttributeError
was swallowed into a `return fallback`, so every bus wake for a node outside
those 8 was dropped as "unknown project" while the logs blamed a config file
that is not even mounted.

These tests hold the three properties that make that impossible to repeat:
no import-time load, no back-edge to `projects`, and a fallback that shouts.
"""

import json
import logging
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

import life_registry_client
import projects

REPO_ROOT = Path(__file__).resolve().parent.parent


def _run_import(order: str, config_path: Path) -> subprocess.CompletedProcess:
    """Import the two modules in `order` in a FRESH interpreter.

    A fresh one matters: import order is only observable once per process, and
    by the time this test module runs pytest has already imported both.
    """
    code = textwrap.dedent(
        f"""
        import sys
        sys.path.insert(0, {str(REPO_ROOT)!r})
        import {order}
        import projects
        print(",".join(p.key for p in projects.list_projects()))
        """
    )
    return subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        env={
            "PATH": "/usr/bin:/bin",
            "COORDINATOR_PROJECTS_CONFIG": str(config_path),
            # No live half to merge in: a reachable backend (or a stale
            # projects.cache.json left in the checkout) would otherwise make
            # this assert on the machine's real node list instead of on the
            # fixture. What is under test here is the import order, nothing else.
            "LIFE_BACKEND_URL": "http://127.0.0.1:1",
            "STATE_PATH": str(config_path.parent / "state.json"),
        },
    )


@pytest.fixture
def local_config(tmp_path: Path) -> Path:
    """The whole registry for the subprocess: no live half, so this is it."""
    path = tmp_path / "projects.local.json"
    path.write_text(
        json.dumps(
            {
                "projects": [
                    {
                        "key": "solo",
                        "label": "Solo",
                        "host_path": "/nodes/solo",
                        "workspace": "/workspaces/solo",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    return path


@pytest.mark.parametrize("order", ["life_registry_client", "projects"])
def test_registry_loads_whatever_the_import_order(order, local_config):
    """Either import first — the registry must come out the same.

    This is the literal reproduction of #35: with `life_registry_client` first
    the old code died on `module 'life_registry_client' has no attribute
    'load_projects_from_life'`.
    """
    result = _run_import(order, local_config)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "solo"


def test_importing_projects_does_not_load_the_registry():
    """Importing must not *run* the registry — that is what made order matter."""
    src = (REPO_ROOT / "projects.py").read_text(encoding="utf-8")
    assert "PROJECTS: tuple[Project, ...] = load_projects()" not in src
    assert "def get_projects()" in src


def test_life_registry_client_does_not_import_projects():
    """No back-edge: the source of the registry must not import the registry."""
    src = (REPO_ROOT / "life_registry_client.py").read_text(encoding="utf-8")
    assert "from project_model import Project" in src
    assert "from projects import" not in src
    assert not any(
        line.strip() == "import projects" for line in src.splitlines()
    )


def test_project_type_is_the_same_object_everywhere():
    """One `Project`, not two that happen to look alike."""
    import project_model

    assert projects.Project is project_model.Project
    assert life_registry_client.Project is project_model.Project


def test_failed_registry_load_is_loud_and_falls_back(monkeypatch, caplog, tmp_path):
    """A registry that FAILED is not a registry that is empty — log it as such."""
    monkeypatch.setattr(
        projects, "load_projects_config_path", lambda: tmp_path / "absent.json"
    )

    def boom(**kwargs):
        raise AttributeError("module has no attribute 'load_projects_from_life'")

    monkeypatch.setattr(life_registry_client, "load_projects_from_life", boom)

    with caplog.at_level(logging.ERROR, logger="coordinator.projects"):
        loaded = projects.load_projects()

    assert loaded == projects._DEFAULT_PROJECTS
    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert errors, "the fallback must not be silent"
    assert "load_projects_from_life" in caplog.text  # the real cause, not a guess
    assert "_DEFAULT_PROJECTS" in projects.registry_source()


def test_registry_source_names_the_source_actually_used(monkeypatch, local_config):
    monkeypatch.setattr(projects, "load_projects_config_path", lambda: local_config)
    projects.load_projects()
    assert str(local_config) in projects.registry_source()

    monkeypatch.setattr(
        projects, "load_projects_config_path", lambda: local_config.with_name("no.json")
    )
    monkeypatch.setattr(
        life_registry_client,
        "load_projects_from_life",
        lambda **kw: (projects.Project("live", "Live", "", "/h", "/w"),),
    )
    projects.load_projects()
    assert "Life" in projects.registry_source()


def test_a_directory_in_place_of_the_local_file_counts_as_absent(
    monkeypatch, caplog, tmp_path
):
    """Docker bind-mounts a MISSING source as an empty directory.

    On a device without projects.local.json (a prod box, 2026-09-21) the mount
    left a directory at that path, read_text raised IsADirectoryError and
    every registry load died: each wake would have crashed. A directory is not
    a config somebody wrote — treat it as no file, and say so.
    """
    fake = tmp_path / "projects.local.json"
    fake.mkdir()
    monkeypatch.setattr(projects, "load_projects_config_path", lambda: fake)
    monkeypatch.setattr(
        life_registry_client, "load_projects_from_life", lambda **kw: projects._DEFAULT_PROJECTS
    )
    with caplog.at_level(logging.WARNING, logger="coordinator.projects"):
        loaded = projects.load_projects()
    assert loaded == projects._DEFAULT_PROJECTS
    assert "directory" in caplog.text
