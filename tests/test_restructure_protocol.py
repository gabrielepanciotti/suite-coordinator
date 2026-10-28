# tests/test_restructure_protocol.py
import restructure_protocol as rp


def test_build_command_shape():
    assert rp.build_command(7) == {"agents_restructure": {
        "id": 7, "target": "canonical", "persona_archetype": "node-persona"}}


def test_build_command_custom_archetype():
    cmd = rp.build_command(9, persona_archetype="node-persona-x")
    assert cmd["agents_restructure"]["persona_archetype"] == "node-persona-x"


def test_parse_verdict_matches_id_agent():
    ctx = {"restructure_verdict": {
        "id": 7, "agent": "cosmos", "ok": True, "persona_name": "cosmos",
        "agents": ["cosmos-backend-impl", "cosmos"],
        "removed_candidates": ["notifications"], "summary": "done"}}
    assert rp.parse_verdict(ctx, 7, "cosmos") == {
        "ok": True, "blocked": False, "persona_name": "cosmos",
        "agents": ["cosmos-backend-impl", "cosmos"],
        "removed_candidates": ["notifications"], "summary": "done", "agent": "cosmos"}


def test_parse_verdict_rejects_mismatch_and_malformed():
    good = {"restructure_verdict": {"id": 7, "agent": "cosmos", "ok": True}}
    assert rp.parse_verdict(good, 9, "cosmos") is None       # wrong id
    assert rp.parse_verdict(good, 7, "network") is None       # wrong agent
    assert rp.parse_verdict({"x": 1}, 7, "cosmos") is None
    assert rp.parse_verdict(None, 7, "cosmos") is None
    assert rp.parse_verdict({"restructure_verdict": "y"}, 7, "cosmos") is None


def test_parse_verdict_defaults_lists_and_flags():
    ctx = {"restructure_verdict": {"id": 3, "agent": "network", "ok": True}}
    v = rp.parse_verdict(ctx, 3, "network")
    assert v["persona_name"] == ""
    assert v["agents"] == []
    assert v["removed_candidates"] == []
    assert v["blocked"] is False


def test_parse_verdict_blocked_with_decision_needed():
    ctx = {"restructure_verdict": {
        "id": 3, "agent": "cosmos", "blocked": True,
        "summary": "unrecognized domain agents", "decision_needed": "Delete legacy?"}}
    assert rp.parse_verdict(ctx, 3, "cosmos") == {
        "ok": False, "blocked": True, "persona_name": "",
        "agents": [], "removed_candidates": [],
        "summary": "unrecognized domain agents",
        "decision_needed": "Delete legacy?", "agent": "cosmos"}


def test_verify_manifest_ok_when_persona_in_roster():
    v = {"persona_name": "cosmos", "agents": ["cosmos-backend-impl", "cosmos"]}
    assert rp.verify_manifest(v) is True


def test_verify_manifest_fails_when_persona_missing_or_absent():
    assert rp.verify_manifest({"persona_name": "", "agents": ["cosmos"]}) is False
    assert rp.verify_manifest({"persona_name": "cosmos", "agents": ["x"]}) is False
    assert rp.verify_manifest({"persona_name": "cosmos", "agents": []}) is False
