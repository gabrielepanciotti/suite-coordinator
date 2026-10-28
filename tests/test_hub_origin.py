# tests/test_hub_origin.py
"""Table tests for the Coordinator's origin-session decision (issue #51)."""
import os

import hub_origin
from projects import escape_path

STALE = hub_origin.STALE_S


def _origin(sid="sid-1", host="hostA", cwd="/nodes/foo", agent="agentX"):
    return {"session_id": sid, "host": host, "cwd": cwd, "agent": agent}


def _make_transcript(projects_dir, cwd, sid, *, mtime):
    d = projects_dir / escape_path(cwd)
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{sid}.jsonl"
    p.write_text("x", encoding="utf-8")
    os.utime(p, (mtime, mtime))
    return p


def _make_registry(sessions_dir, sid):
    sessions_dir.mkdir(parents=True, exist_ok=True)
    (sessions_dir / f"{sid}.json").write_text("{}", encoding="utf-8")


def test_no_origin_is_derived(tmp_path):
    t = hub_origin.who_takes_turn(
        None, "agentX", "hostA",
        sessions_dir=tmp_path / "s", projects_dir=tmp_path / "p", now=0.0,
    )
    assert t.action == "derived"


def test_other_agent_is_derived(tmp_path):
    t = hub_origin.who_takes_turn(
        _origin(agent="other"), "agentX", "hostA",
        sessions_dir=tmp_path / "s", projects_dir=tmp_path / "p", now=0.0,
    )
    assert t.action == "derived"


def test_other_host_is_skip(tmp_path):
    t = hub_origin.who_takes_turn(
        _origin(host="hostB"), "agentX", "hostA",
        sessions_dir=tmp_path / "s", projects_dir=tmp_path / "p", now=0.0,
    )
    assert t.action == "skip"


def test_missing_session_or_cwd_is_derived(tmp_path):
    for o in (_origin(sid=""), _origin(cwd=""), {"host": "hostA", "agent": "agentX"}):
        t = hub_origin.who_takes_turn(
            o, "agentX", "hostA",
            sessions_dir=tmp_path / "s", projects_dir=tmp_path / "p", now=0.0,
        )
        assert t.action == "derived", o


def test_transcript_not_on_host_is_derived(tmp_path):
    t = hub_origin.who_takes_turn(
        _origin(), "agentX", "hostA",
        sessions_dir=tmp_path / "s", projects_dir=tmp_path / "p", now=0.0,
    )
    assert t.action == "derived"


def test_no_registry_here_is_derived(tmp_path):
    _make_transcript(tmp_path / "p", "/nodes/foo", "sid-1", mtime=1.0)
    # sessions_dir does not exist -> cannot tell alive from dead
    t = hub_origin.who_takes_turn(
        _origin(), "agentX", "hostA",
        sessions_dir=tmp_path / "missing", projects_dir=tmp_path / "p", now=1.0,
    )
    assert t.action == "derived"


def test_alive_session_is_skip(tmp_path):
    _make_transcript(tmp_path / "p", "/nodes/foo", "sid-1", mtime=1000.0)
    _make_registry(tmp_path / "s", "sid-1")
    t = hub_origin.who_takes_turn(
        _origin(), "agentX", "hostA",
        sessions_dir=tmp_path / "s", projects_dir=tmp_path / "p", now=1000.0,
    )
    assert t.action == "skip"
    assert "alive" in t.reason


def test_dead_session_without_registry_resumes(tmp_path):
    _make_transcript(tmp_path / "p", "/nodes/foo", "sid-1", mtime=1.0)
    (tmp_path / "s").mkdir(parents=True, exist_ok=True)  # registry dir visible, entry gone
    t = hub_origin.who_takes_turn(
        _origin(), "agentX", "hostA",
        sessions_dir=tmp_path / "s", projects_dir=tmp_path / "p", now=STALE + 10.0,
    )
    assert t.action == "resume_origin"
    assert t.session_id == "sid-1"
    assert t.cwd == "/nodes/foo"


def test_stale_transcript_resumes_even_with_registry(tmp_path):
    # Registry entry present but transcript older than STALE_S -> crashed
    # without a clean SessionEnd; resume it.
    _make_transcript(tmp_path / "p", "/nodes/foo", "sid-1", mtime=1.0)
    _make_registry(tmp_path / "s", "sid-1")
    t = hub_origin.who_takes_turn(
        _origin(), "agentX", "hostA",
        sessions_dir=tmp_path / "s", projects_dir=tmp_path / "p", now=STALE + 10.0,
    )
    assert t.action == "resume_origin"


def test_host_comparison_is_case_insensitive(tmp_path):
    t = hub_origin.who_takes_turn(
        _origin(host="HOSTA"), "agentX", "hosta",
        sessions_dir=tmp_path / "s", projects_dir=tmp_path / "p", now=0.0,
    )
    assert t.action == "derived"  # agent matches, host matches -> not a skip


# --- the last interactive voice (#54) ---------------------------------------


def _msg(agent, voice=None, session=None):
    ctx = {}
    if voice:
        ctx["voice"] = voice
    if session:
        ctx["session"] = session
    return {"from_agent": agent, "context": ctx or None}


def test_last_interactive_voice_takes_the_newest_terminal_message():
    old, new = _origin(sid="old"), _origin(sid="new")
    msgs = [
        _msg("agentX", "interactive", old),
        _msg("agentY"),
        _msg("agentX", "interactive", new),
        _msg("agentX", "headless"),
    ]
    assert hub_origin.last_interactive_voice(msgs, "agentX")["session_id"] == "new"


def test_last_interactive_voice_ignores_others_and_headless():
    msgs = [
        _msg("agentY", "interactive", _origin(agent="agentY")),
        _msg("agentX", "headless", _origin()),
        _msg("agentX"),
    ]
    assert hub_origin.last_interactive_voice(msgs, "agentX") is None
    assert hub_origin.last_interactive_voice(None, "agentX") is None


def test_last_interactive_voice_needs_the_session_of_that_agent():
    # A session stamped for another identity is not this agent's voice.
    msgs = [_msg("agentX", "interactive", _origin(agent="agentY"))]
    assert hub_origin.last_interactive_voice(msgs, "agentX") is None


def test_pick_turn_any_skip_wins():
    skip = hub_origin.Turn("skip", "alive")
    resume = hub_origin.Turn("resume_origin", "closed", session_id="o", cwd="/c")
    assert hub_origin.pick_turn(resume, skip) is skip
    assert hub_origin.pick_turn(skip, resume) is skip


def test_pick_turn_prefers_the_newer_voice_to_resume():
    origin = hub_origin.Turn("resume_origin", "closed", session_id="o", cwd="/c")
    voice = hub_origin.Turn("resume_origin", "closed", session_id="v", cwd="/c")
    derived = hub_origin.Turn("derived", "n/a")
    assert hub_origin.pick_turn(origin, voice) is voice
    assert hub_origin.pick_turn(origin, derived) is origin
    assert hub_origin.pick_turn(derived, None) is derived


def test_alive_terminal_voice_on_a_conversation_it_did_not_open(tmp_path):
    # The #54 incident: the terminal session is replying on a conversation it
    # did not open, and is still alive -> the headless twin stays out.
    sessions, projects = tmp_path / "s", tmp_path / "p"
    now = 10_000_000.0
    voice = _origin(sid="term")
    _make_transcript(projects, voice["cwd"], "term", mtime=now - 60)
    _make_registry(sessions, "term")
    t = hub_origin.who_takes_turn(
        voice, "agentX", "hostA", sessions_dir=sessions, projects_dir=projects, now=now
    )
    assert hub_origin.pick_turn(hub_origin.Turn("derived", "no origin"), t).action == "skip"
