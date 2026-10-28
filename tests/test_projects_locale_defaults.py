"""The fallback registry comes from locale/projects.json (issue #43).

The engine ships only the generic ``claude`` (~/.claude config) entry; the
installation's inventory is data. The loader must never raise: the prod
workers run with ``LIFE_BACKEND_URL=off`` and this list is all they have.
"""

from __future__ import annotations

import json

import pytest
import logging
from pathlib import Path

import local_defaults
import projects


def _write(path: Path, payload: object) -> Path:
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_file_present_loads_entries_then_claude(tmp_path: Path) -> None:
    f = _write(
        tmp_path / "projects.json",
        [
            {
                "_comment": "ignored",
                "key": "alpha",
                "label": "A Alpha",
                "emoji": "A",
                "host_rel": "Alpha",
                "workspace": "/workspaces/alpha",
                "aliases": ["a"],
                "session_globs": ["*alpha*"],
                "kind": "personal",
            },
            {"key": "beta", "label": "Beta", "host_rel": "Beta", "workspace": "/workspaces/beta"},
        ],
    )
    loaded = projects._load_default_projects(f)
    assert [p.key for p in loaded] == ["alpha", "beta", "claude"]
    alpha = loaded[0]
    assert alpha.aliases == ("a",)
    assert alpha.session_globs == ("*alpha*",)
    assert alpha.kind == "personal"
    assert loaded[1].kind == "work"
    assert loaded[1].emoji == ""
    assert loaded[-1] is projects._CLAUDE_CONFIG_PROJECT


def test_host_rel_resolved_against_srv_root(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(projects, "_SRV_ROOT", Path("/nodes"))
    f = _write(
        tmp_path / "projects.json",
        [{"key": "h", "label": "H", "host_rel": "Casa/Home", "workspace": "/workspaces/h"}],
    )
    (h, _claude) = projects._load_default_projects(f)
    assert h.host_path == str(Path("/nodes") / "Casa" / "Home")


def test_claude_not_duplicated_when_file_defines_it(tmp_path: Path) -> None:
    f = _write(
        tmp_path / "projects.json",
        [{"key": "claude", "label": "C", "host_path": "/nodes/c", "workspace": "/workspaces/c"}],
    )
    loaded = projects._load_default_projects(f)
    assert [p.key for p in loaded] == ["claude"]
    assert loaded[0].host_path == "/nodes/c"


def test_file_absent_gives_only_claude(tmp_path: Path, caplog) -> None:
    with caplog.at_level(logging.INFO, logger="coordinator.projects"):
        loaded = projects._load_default_projects(tmp_path / "missing.json")
    assert loaded == (projects._CLAUDE_CONFIG_PROJECT,)
    assert any(r.levelno == logging.INFO and "missing.json" in r.message for r in caplog.records)


def test_malformed_json_gives_only_claude_and_logs_error(tmp_path: Path, caplog) -> None:
    f = tmp_path / "projects.json"
    f.write_text("[{not json", encoding="utf-8")
    with caplog.at_level(logging.ERROR, logger="coordinator.projects"):
        loaded = projects._load_default_projects(f)
    assert loaded == (projects._CLAUDE_CONFIG_PROJECT,)
    assert any(r.levelno == logging.ERROR for r in caplog.records)


def test_wrong_shape_gives_only_claude_and_logs_error(tmp_path: Path, caplog) -> None:
    for payload in ({"projects": []}, [{"key": "x", "label": "X"}], ["not-an-object"]):
        f = _write(tmp_path / "projects.json", payload)
        caplog.clear()
        with caplog.at_level(logging.ERROR, logger="coordinator.projects"):
            loaded = projects._load_default_projects(f)
        assert loaded == (projects._CLAUDE_CONFIG_PROJECT,)
        assert any(r.levelno == logging.ERROR for r in caplog.records)


def test_default_path_follows_locale_dir(tmp_path: Path, monkeypatch) -> None:
    _write(
        tmp_path / "projects.json",
        [{"key": "z", "label": "Z", "host_rel": "Z", "workspace": "/workspaces/z"}],
    )
    monkeypatch.setenv(local_defaults.LOCALE_FILE_ENV, str(tmp_path / "defaults.json"))
    assert local_defaults.locale_dir() == tmp_path
    assert [p.key for p in projects._load_default_projects()] == ["z", "claude"]


def test_versioned_locale_file_has_no_absolute_paths() -> None:
    f = Path(projects.__file__).resolve().parent / "locale" / "projects.json"
    if not f.is_file():
        pytest.skip("no installation locale/projects.json in this checkout")
    entries = json.loads(f.read_text(encoding="utf-8"))
    assert entries, "the installation inventory should not be empty"
    for e in entries:
        assert "host_path" not in e
        assert not Path(e["host_rel"]).is_absolute()
