# tests/test_pipeline_stages.py
import pipeline_stages as ps


def test_default_stages_canonical_order():
    stages = ps.default_stages("offerte-dev", "deployer-edge")
    assert [(s.name, s.type, s.agent) for s in stages] == [
        ("implement", "implement", "offerte-dev"),
        ("test", "test", "offerte-dev"),
        ("gate", "gate", ""),
        ("deploy", "deploy", "deployer-edge"),
        ("verify", "verify", "deployer-edge"),
    ]


def test_default_stages_single_target_as_list_keeps_plain_names():
    # Passare una lista di uno solo non deve cambiare la forma della pipeline.
    assert ps.default_stages("offerte-dev", ["deployer-edge"]) == \
        ps.default_stages("offerte-dev", "deployer-edge")


def test_default_stages_fans_out_over_prod_servers():
    # Il Coordinator gira su entrambi i prod: una sola approvazione, due deploy,
    # in sequenza — se il primo fallisce la pipeline si ferma prima del secondo.
    stages = ps.default_stages("coordinator-dev-linux",
                               ["deployer-core", "deployer-edge"])
    assert [(s.name, s.type, s.agent) for s in stages] == [
        ("implement", "implement", "coordinator-dev-linux"),
        ("test", "test", "coordinator-dev-linux"),
        ("gate", "gate", ""),
        ("deploy-core", "deploy", "deployer-core"),
        ("verify-core", "verify", "deployer-core"),
        ("deploy-edge", "deploy", "deployer-edge"),
        ("verify-edge", "verify", "deployer-edge"),
    ]
    # Un solo gate: si approva una volta, non una per server.
    assert sum(1 for s in stages if s.type == "gate") == 1


def test_to_dict_from_dict_roundtrip():
    s = ps.PipelineStageDecl("test", "test", "offerte-dev", {"suite": "fast"})
    assert ps.PipelineStageDecl.from_dict(ps.to_dict(s)) == s


def test_build_command_shape_dev_stage():
    s = ps.PipelineStageDecl("implement", "implement", "offerte-dev")
    cmd = ps.build_command(42, s, "allinea e deploya")
    assert cmd == {"pipeline": {
        "id": 42, "stage": "implement", "type": "implement",
        "task": "allinea e deploya", "args": {},
    }}


def test_build_command_shape_test_stage():
    s = ps.PipelineStageDecl("test", "test", "offerte-dev")
    cmd = ps.build_command(42, s, "allinea e deploya")
    assert cmd == {"pipeline": {
        "id": 42, "stage": "test", "type": "test",
        "task": "allinea e deploya", "args": {},
    }}


def test_build_command_shape_deploy_stage_adds_deployer_block():
    s = ps.PipelineStageDecl("deploy", "deploy", "deployer-edge")
    cmd = ps.build_command(42, s, "allinea e deploya")
    assert cmd == {
        "deployer": {"verb": "deploy", "args": {}},
        "pipeline": {
            "id": 42, "stage": "deploy", "type": "deploy",
            "task": "allinea e deploya", "args": {},
        },
    }


def test_build_command_shape_verify_stage_adds_deployer_block():
    s = ps.PipelineStageDecl("verify", "verify", "deployer-edge")
    cmd = ps.build_command(42, s, "allinea e deploya")
    assert cmd == {
        "deployer": {"verb": "verify", "args": {}},
        "pipeline": {
            "id": 42, "stage": "verify", "type": "verify",
            "task": "allinea e deploya", "args": {},
        },
    }


def test_build_command_shape_rollback_stage_adds_deployer_block():
    s = ps.PipelineStageDecl(
        "rollback", "deploy", "deployer-edge",
        {"rollback": True, "to_sha": "abc1234"},
    )
    cmd = ps.build_command(42, s, "allinea e deploya")
    assert cmd == {
        "deployer": {"verb": "rollback", "args": {"to_sha": "abc1234"}},
        "pipeline": {
            "id": 42, "stage": "rollback", "type": "deploy",
            "task": "allinea e deploya", "args": {"rollback": True, "to_sha": "abc1234"},
        },
    }


def test_infra_command_deploy():
    s = ps.PipelineStageDecl("deploy", "deploy", "d")
    assert ps.infra_command(s) == ("deploy", {})


def test_infra_command_verify():
    s = ps.PipelineStageDecl("verify", "verify", "d")
    assert ps.infra_command(s) == ("verify", {})


def test_infra_command_rollback():
    s = ps.PipelineStageDecl("rollback", "deploy", "d", {"rollback": True, "to_sha": "abc1234"})
    assert ps.infra_command(s) == ("rollback", {"to_sha": "abc1234"})


def test_infra_command_dev_stage_is_none():
    assert ps.infra_command(ps.PipelineStageDecl("implement", "implement", "d")) is None
    assert ps.infra_command(ps.PipelineStageDecl("test", "test", "d")) is None
    assert ps.infra_command(ps.PipelineStageDecl("gate", "gate")) is None


def test_parse_verdict_matches_id_and_stage():
    ctx = {"pipeline_verdict": {"id": 42, "stage": "test", "ok": True,
                                "summary": "53/53", "sha": "def456"}}
    assert ps.parse_verdict(ctx, 42, "test") == {
        "ok": True, "blocked": False, "summary": "53/53", "sha": "def456"}


def test_parse_verdict_blocked():
    ctx = {"pipeline_verdict": {"id": 42, "stage": "test", "blocked": True,
                                "summary": "GDPR gate", "decision_needed": "Autorizzi?"}}
    assert ps.parse_verdict(ctx, 42, "test") == {
        "ok": False, "blocked": True, "summary": "GDPR gate",
        "decision_needed": "Autorizzi?", "sha": None}


def test_parse_verdict_rejects_wrong_id_or_stage_or_shape():
    good = {"pipeline_verdict": {"id": 42, "stage": "test", "ok": True}}
    assert ps.parse_verdict(good, 99, "test") is None      # wrong id
    assert ps.parse_verdict(good, 42, "deploy") is None     # wrong stage
    assert ps.parse_verdict({"nope": 1}, 42, "test") is None
    assert ps.parse_verdict(None, 42, "test") is None
    assert ps.parse_verdict({"pipeline_verdict": "x"}, 42, "test") is None


import os
import manifest
from projects import Project


def _project(tmp_path):
    ws = tmp_path / "ws"
    (ws / ".claude").mkdir(parents=True)
    return Project("demo", "Demo", "🧪", str(ws), str(ws)), ws


def test_manifest_parses_pipeline_stages(tmp_path):
    proj, ws = _project(tmp_path)
    (ws / ".claude" / "coordinator.toml").write_text(
        '[[pipeline_stage]]\n'
        'name = "test"\n'
        'type = "test"\n'
        'agent = "demo-dev"\n\n'
        '[[pipeline_stage]]\n'
        'name = "gate"\n'
        'type = "gate"\n',
        encoding="utf-8",
    )
    manifest._CACHE.clear()
    stages = manifest.pipeline_stages_of(proj)
    assert [(s.name, s.type, s.agent) for s in stages] == [
        ("test", "test", "demo-dev"), ("gate", "gate", "")]


def test_manifest_skips_bad_pipeline_stage(tmp_path):
    proj, ws = _project(tmp_path)
    (ws / ".claude" / "coordinator.toml").write_text(
        '[[pipeline_stage]]\nname = "x"\ntype = "bogus"\n\n'   # bad type -> skipped
        '[[pipeline_stage]]\ntype = "test"\nagent = "d"\n\n'    # missing name -> skipped
        '[[pipeline_stage]]\nname = "ok"\ntype = "deploy"\nagent = "d"\n',
        encoding="utf-8",
    )
    manifest._CACHE.clear()
    stages = manifest.pipeline_stages_of(proj)
    assert [(s.name, s.type) for s in stages] == [("ok", "deploy")]


def test_manifest_empty_pipeline_when_absent(tmp_path):
    proj, ws = _project(tmp_path)
    (ws / ".claude" / "coordinator.toml").write_text('[telegram]\nagent = "x"\n',
                                                      encoding="utf-8")
    manifest._CACHE.clear()
    assert manifest.pipeline_stages_of(proj) == []
