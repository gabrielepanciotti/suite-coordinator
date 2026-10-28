# tests/test_migrate_protocol.py
import migrate_protocol as mp


def test_phases_order():
    assert mp.PHASES == ("prepare", "verify", "commit")


def test_build_command_shape():
    assert mp.build_command(7, "prepare", "head") == {"migrate": {
        "id": 7, "phase": "prepare", "rev": "head", "args": {}}}


def test_build_command_passes_args():
    cmd = mp.build_command(7, "commit", "abc123", {"timeout": 60})
    assert cmd["migrate"]["args"] == {"timeout": 60}


def test_parse_verdict_matches_id_phase_agent():
    ctx = {"migrate_verdict": {"id": 7, "phase": "prepare", "agent": "db-prod",
                               "ok": True, "summary": "1 rev"}}
    assert mp.parse_verdict(ctx, 7, "prepare", "db-prod") == {
        "ok": True, "blocked": False, "summary": "1 rev", "agent": "db-prod"}


def test_parse_verdict_rejects_mismatch_and_malformed():
    good = {"migrate_verdict": {"id": 7, "phase": "prepare", "agent": "db-prod", "ok": True}}
    assert mp.parse_verdict(good, 9, "prepare", "db-prod") is None     # wrong id
    assert mp.parse_verdict(good, 7, "verify", "db-prod") is None       # wrong phase
    assert mp.parse_verdict(good, 7, "prepare", "db-dev") is None       # wrong agent
    assert mp.parse_verdict({"x": 1}, 7, "prepare", "db-prod") is None
    assert mp.parse_verdict(None, 7, "prepare", "db-prod") is None
    assert mp.parse_verdict({"migrate_verdict": "y"}, 7, "prepare", "db-prod") is None


def test_parse_verdict_blocked_with_decision_needed():
    ctx = {"migrate_verdict": {"id": 3, "phase": "commit", "agent": "db-prod",
                               "blocked": True, "summary": "GDPR gate",
                               "decision_needed": "Confirm PII wipe?"}}
    result = mp.parse_verdict(ctx, 3, "commit", "db-prod")
    assert result == {"ok": False, "blocked": True, "summary": "GDPR gate",
                      "decision_needed": "Confirm PII wipe?", "agent": "db-prod"}


def test_parse_verdict_blocked_missing_decision_needed_defaults_empty():
    ctx = {"migrate_verdict": {"id": 3, "phase": "commit", "agent": "db-prod",
                               "blocked": True, "summary": "auth gate"}}
    result = mp.parse_verdict(ctx, 3, "commit", "db-prod")
    assert result == {"ok": False, "blocked": True, "summary": "auth gate",
                      "decision_needed": "", "agent": "db-prod"}


def test_parse_verdict_blocked_mismatch_returns_none():
    ctx = {"migrate_verdict": {"id": 3, "phase": "commit", "agent": "db-prod",
                               "blocked": True, "decision_needed": "Confirm?"}}
    assert mp.parse_verdict(ctx, 9, "commit", "db-prod") is None   # wrong id
    assert mp.parse_verdict(ctx, 3, "prepare", "db-prod") is None  # wrong phase
    assert mp.parse_verdict(ctx, 3, "commit", "db-dev") is None    # wrong agent


import manifest
from projects import Project


def _project(tmp_path):
    ws = tmp_path / "ws"
    (ws / ".claude").mkdir(parents=True)
    return Project("demo", "Demo", "🧪", str(ws), str(ws)), ws


def test_manifest_parses_db_targets(tmp_path):
    proj, ws = _project(tmp_path)
    (ws / ".claude" / "coordinator.toml").write_text(
        '[[db_target]]\nagent = "db-prod"\nhost = "edge"\n\n'
        '[[db_target]]\nagent = "db-dev-linux"\nhost = "core"\n',
        encoding="utf-8")
    manifest._CACHE.clear()
    targets = manifest.db_targets_of(proj)
    assert [(t.agent, t.host) for t in targets] == [
        ("db-prod", "edge"), ("db-dev-linux", "core")]


def test_manifest_db_target_host_defaults_to_agent(tmp_path):
    proj, ws = _project(tmp_path)
    (ws / ".claude" / "coordinator.toml").write_text(
        '[[db_target]]\nagent = "db-prod"\n', encoding="utf-8")
    manifest._CACHE.clear()
    targets = manifest.db_targets_of(proj)
    assert targets[0].host == "db-prod"


def test_manifest_db_target_skips_missing_agent(tmp_path):
    proj, ws = _project(tmp_path)
    (ws / ".claude" / "coordinator.toml").write_text(
        '[[db_target]]\nhost = "x"\n\n[[db_target]]\nagent = "ok"\n',
        encoding="utf-8")
    manifest._CACHE.clear()
    assert [t.agent for t in manifest.db_targets_of(proj)] == ["ok"]


def test_manifest_db_targets_empty_when_absent(tmp_path):
    proj, ws = _project(tmp_path)
    (ws / ".claude" / "coordinator.toml").write_text('[telegram]\nagent="x"\n',
                                                      encoding="utf-8")
    manifest._CACHE.clear()
    assert manifest.db_targets_of(proj) == []
