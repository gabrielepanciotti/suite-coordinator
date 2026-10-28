"""TicketManager#10: conversation -> session view for the Launcher link."""
from __future__ import annotations

import importlib
import json
import os

import pytest
from fastapi.testclient import TestClient

from hub_origin import claude_slug as escape_path


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("HUB_TOPICS_PATH", str(tmp_path / "topics.json"))
    import hub_store

    return importlib.reload(hub_store)


def _transcript(projects_dir, dir_cwd, sid, cwds):
    d = projects_dir / escape_path(dir_cwd)
    d.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps({"type": "user", "cwd": c, "sessionId": sid}) for c in cwds]
    lines.append(json.dumps({"type": "summary"}))  # an event without cwd
    p = d / f"{sid}.jsonl"
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return p


def test_set_session_running_and_started_at(store):
    store.set_session(7, "s1", agent_id="a", workspace="/w", running=True)
    first = store.get_conv(7)["sessions"]["a"]
    assert first["running"] is True and first["started_at"]
    store.set_session(7, "s1", agent_id="a", workspace="/w", running=False)
    again = store.get_conv(7)["sessions"]["a"]
    assert again["running"] is False and again["started_at"] == first["started_at"]
    # Without `running` the flag is kept; a NEW session id starts fresh.
    store.set_session(7, "s1", agent_id="a", workspace="/w", running=True)
    store.set_session(7, "s1", agent_id="a", workspace="/w")
    assert store.get_conv(7)["sessions"]["a"]["running"] is True
    store.set_session(7, "s2", agent_id="a", workspace="/w")
    assert store.get_conv(7)["sessions"]["a"]["running"] is False
    # The resume lookup is unchanged by the extra fields.
    assert store.get_session(7, "a", "/w") == "s2"


def test_mark_stopped_and_clear_running(store):
    store.set_session(1, "s1", agent_id="a", workspace="/w", running=True)
    store.set_session(2, "s2", agent_id="b", workspace="/w", running=True)
    store.mark_session_stopped(1, "a")
    assert store.get_conv(1)["sessions"]["a"]["running"] is False
    assert store.clear_running_sessions() == 1
    assert store.get_conv(2)["sessions"]["b"]["running"] is False
    store.mark_session_stopped(99, "nobody")  # no record: no-op


def test_cwd_follows_the_session_into_its_worktree(tmp_path):
    import hub_origin

    projects = tmp_path / "projects"
    # Born in the repo, moved by EnterWorktree: Claude Code files the
    # transcript under the worktree's project dir, last cwd = the worktree.
    _transcript(projects, "/wt/repo-ticket-59", "sid", ["/repo", "/wt/repo-ticket-59"])
    rec = {"sessions": {"portale": {"session_id": "sid", "workspace": "/repo",
                                    "running": True, "started_at": "T"}}}
    rows = hub_origin.conversation_sessions(rec, projects)
    assert rows == [{"agent_id": "portale", "session_id": "sid",
                     "cwd": "/wt/repo-ticket-59", "running": True, "started_at": "T"}]


def test_cwd_falls_back_to_workspace_without_transcript(tmp_path):
    import hub_origin

    rec = {"sessions": {"a": {"session_id": "gone", "workspace": "/repo"}}}
    (row,) = hub_origin.conversation_sessions(rec, tmp_path / "projects")
    assert row["cwd"] == "/repo" and row["running"] is False


def test_cwd_is_the_resume_root_not_a_later_cd(tmp_path):
    # TicketManager#10, conv 462: the resumed session did `cd backend`. The
    # transcript stays under the worktree's project dir; from .../backend
    # `claude --resume` would not find it.
    import hub_origin

    projects = tmp_path / "projects"
    wt = "/nodes/.launcher-worktrees/Portale_Segnalazioni/portaleSegnalazioni-ticket-59"
    _transcript(projects, wt, "sid", ["/nodes/repo/Portale_Segnalazioni", wt, wt + "/backend"])
    assert hub_origin.real_session_cwd(projects, "sid") == wt


def test_cwd_from_a_parent_when_the_root_never_appears(tmp_path):
    import hub_origin

    projects = tmp_path / "projects"
    _transcript(projects, "/repo_x", "sid", ["/repo_x/backend/app"])
    assert hub_origin.real_session_cwd(projects, "sid") == "/repo_x"


def test_claude_slug_maps_underscores():
    import hub_origin

    assert hub_origin.claude_slug("/nodes/repo/Portale_Segnalazioni") == (
        "-nodes-repo-Portale-Segnalazioni"
    )


def test_newest_transcript_wins(tmp_path):
    import hub_origin

    projects = tmp_path / "projects"
    old = _transcript(projects, "/a", "sid", ["/a"])
    _transcript(projects, "/b", "sid", ["/b"])
    os.utime(old, (1, 1))
    assert hub_origin.real_session_cwd(projects, "sid") == "/b"


def test_interactive_holder(tmp_path):
    import time

    import hub_origin

    projects, sessions = tmp_path / "projects", tmp_path / "sessions"
    sessions.mkdir()
    _transcript(projects, "/wt/x", "sid", ["/wt/x"])
    kw = dict(sessions_dir=sessions, projects_dir=projects, now=time.time())
    assert hub_origin.interactive_holder("sid", "a", "h", **kw) is None
    (sessions / "sid.json").write_text("{}")
    assert hub_origin.interactive_holder("sid", "a", "h", **kw).action == "skip"
    # Stale transcript: the registry entry alone does not prove it is alive.
    kw["now"] = time.time() + hub_origin.STALE_S + 60
    assert hub_origin.interactive_holder("sid", "a", "h", **kw) is None
    assert hub_origin.interactive_holder("unknown", "a", "h", **kw) is None


def test_api_conversation_sessions(store, tmp_path, monkeypatch):
    import api
    import hub

    projects = tmp_path / "projects"
    _transcript(projects, "/wt/t59", "sid", ["/repo", "/wt/t59"])
    monkeypatch.setattr(hub, "_HUB_PROJECTS_DIR", projects)
    store.set_session(462, "sid", agent_id="portale-dev-linux", workspace="/repo",
                      running=True)
    client = TestClient(api.app)

    r = client.get("/api/conversations/462/sessions")
    assert r.status_code == 200
    body = r.json()
    assert body["conversation_id"] == 462
    (row,) = body["sessions"]
    assert row["agent_id"] == "portale-dev-linux"
    assert row["cwd"] == "/wt/t59" and row["running"] is True

    assert client.get("/api/conversations/999/sessions").status_code == 404
    store.upsert_conv(500, tenant="t", chat_id=1, topic_id=2)  # topic, no session
    assert client.get("/api/conversations/500/sessions").status_code == 404
