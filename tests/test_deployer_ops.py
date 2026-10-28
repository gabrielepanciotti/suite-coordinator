# tests/test_deployer_ops.py
import asyncio
import dataclasses

import deployer_config as dc
import deployer_ops as ops


def _run(coro):
    return asyncio.run(coro)


def _cfg(tmp_path):
    return dc.DeployerConfig(
        hub_url="http://h:8031", tenant="acme", api_key="k",
        agent_id="deployer-test",
        allowed_senders=frozenset({"owner-telegram"}),
        env_keys=frozenset({"HUB_LOCAL_AGENTS", "HUB_TURN_CAP"}),
        compose_file="docker-compose.prod.yml",
        project_dir=str(tmp_path),
        worker_service="coordinator-worker",
        env_file=".env", branch="master",
    )


# --- upsert_env (pure) ---

def test_upsert_env_replaces_existing_key_preserving_rest():
    text = "# comment\nFOO=1\nHUB_TURN_CAP=12\nBAR=2\n"
    out = ops.upsert_env(text, "HUB_TURN_CAP", "20")
    assert "HUB_TURN_CAP=20" in out
    assert "FOO=1" in out and "BAR=2" in out and "# comment" in out
    assert "HUB_TURN_CAP=12" not in out


def test_upsert_env_appends_when_absent():
    out = ops.upsert_env("FOO=1\n", "HUB_LOCAL_AGENTS", "a:b")
    assert "FOO=1" in out
    assert "HUB_LOCAL_AGENTS=a:b" in out


def test_upsert_env_does_not_match_substring_keys():
    # "HUB_TURN_CAP" must not be altered when upserting "TURN_CAP"
    out = ops.upsert_env("HUB_TURN_CAP=12\n", "TURN_CAP", "9")
    assert "HUB_TURN_CAP=12" in out
    assert "TURN_CAP=9" in out


# --- op_set_env (I/O + whitelist) ---

def test_op_set_env_writes_whitelisted_key(tmp_path):
    (tmp_path / ".env").write_text("HUB_TURN_CAP=12\n", encoding="utf-8")
    res = _run(ops.op_set_env(_cfg(tmp_path), "HUB_TURN_CAP", "20"))
    assert res.ok is True
    assert "HUB_TURN_CAP=20" in (tmp_path / ".env").read_text(encoding="utf-8")


def test_op_set_env_refuses_non_whitelisted_key(tmp_path):
    (tmp_path / ".env").write_text("HUB_TURN_CAP=12\n", encoding="utf-8")
    res = _run(ops.op_set_env(_cfg(tmp_path), "HUB_API_KEY_ACME", "leak"))
    assert res.ok is False
    # file untouched
    assert "leak" not in (tmp_path / ".env").read_text(encoding="utf-8")


# --- subprocess ops (patched runner) ---

def test_op_pull_reports_failure_on_nonzero(tmp_path, monkeypatch):
    async def fake_run(argv, cwd=None):
        assert argv[:3] == ["git", "-C", str(tmp_path)]
        return 1, "fatal: Not possible to fast-forward, aborting."
    monkeypatch.setattr(ops, "_run", fake_run)
    res = _run(ops.op_pull(_cfg(tmp_path)))
    assert res.ok is False
    assert "fast-forward" in res.log_tail


def test_op_pull_uses_configured_remote(tmp_path, monkeypatch):
    # A Windows box whose origin is GitHub over HTTPS (credential helper needs
    # an interactive session) pulls from an SSH mirror, not `origin`.
    seen = []

    async def fake_run(argv, cwd=None):
        seen.append(argv)
        return 0, "Already up to date."
    monkeypatch.setattr(ops, "_run", fake_run)
    cfg = dataclasses.replace(_cfg(tmp_path), remote="mirror")
    res = _run(ops.op_pull(cfg))
    assert res.ok is True
    assert seen[0][-3:] == ["--ff-only", "mirror", "master"]
    assert "mirror/master" in res.summary


def test_op_pull_defaults_to_origin(tmp_path, monkeypatch):
    seen = []

    async def fake_run(argv, cwd=None):
        seen.append(argv)
        return 0, "Already up to date."
    monkeypatch.setattr(ops, "_run", fake_run)
    _run(ops.op_pull(_cfg(tmp_path)))
    assert seen[0][-3:] == ["--ff-only", "origin", "master"]


def test_op_recreate_worker_targets_only_worker_service(tmp_path, monkeypatch):
    seen = {}
    async def fake_run(argv, cwd=None):
        seen["argv"] = argv
        return 0, "Recreating coordinator-worker ... done"
    monkeypatch.setattr(ops, "_run", fake_run)
    res = _run(ops.op_recreate_worker(_cfg(tmp_path)))
    assert res.ok is True
    assert "--force-recreate" in seen["argv"]
    assert seen["argv"][-1] == "coordinator-worker"


def test_op_deploy_aborts_before_recreate_if_pull_fails(tmp_path, monkeypatch):
    calls = []
    async def fake_pull(cfg):
        calls.append("pull")
        return ops.OpResult(False, "pull failed (exit 1)", "diverged")
    async def fake_recreate(cfg):
        calls.append("recreate")
        return ops.OpResult(True, "recreated", "")
    monkeypatch.setattr(ops, "op_pull", fake_pull)
    monkeypatch.setattr(ops, "op_recreate_worker", fake_recreate)
    res = _run(ops.op_deploy(_cfg(tmp_path)))
    assert res.ok is False
    assert calls == ["pull"]  # recreate never ran


# --- op_rollback (validation + detached checkout + recreate) ---

def test_op_rollback_refuses_empty_sha(tmp_path):
    res = _run(ops.op_rollback(_cfg(tmp_path), "  "))
    assert res.ok is False
    assert "no to_sha" in res.summary


def test_op_rollback_refuses_unknown_commit(tmp_path, monkeypatch):
    async def fake_run(argv, cwd=None):
        if "cat-file" in argv:
            return 128, "fatal: Not a valid object name"
        raise AssertionError(f"should not reach: {argv}")
    monkeypatch.setattr(ops, "_run", fake_run)
    res = _run(ops.op_rollback(_cfg(tmp_path), "deadbeef"))
    assert res.ok is False
    assert "unknown commit" in res.summary


def test_op_rollback_refuses_non_ancestor(tmp_path, monkeypatch):
    async def fake_run(argv, cwd=None):
        if "cat-file" in argv:
            return 0, ""
        if "merge-base" in argv:
            assert "--is-ancestor" in argv and "master" in argv
            return 1, ""
        raise AssertionError(f"should not reach: {argv}")
    monkeypatch.setattr(ops, "_run", fake_run)
    res = _run(ops.op_rollback(_cfg(tmp_path), "abc1234"))
    assert res.ok is False
    assert "not an ancestor" in res.summary


def test_op_rollback_success_checks_then_checkout_then_recreate(tmp_path, monkeypatch):
    calls = []

    async def fake_run(argv, cwd=None):
        calls.append(argv)
        if "cat-file" in argv:
            return 0, ""
        if "merge-base" in argv:
            return 0, ""
        if "checkout" in argv:
            assert "--detach" in argv and "abc1234" in argv
            return 0, ""
        if "compose" in argv:
            return 0, "Recreated"
        raise AssertionError(f"unexpected: {argv}")

    monkeypatch.setattr(ops, "_run", fake_run)
    res = _run(ops.op_rollback(_cfg(tmp_path), "abc1234"))
    assert res.ok is True
    assert "rolled back to abc1234" in res.summary
    kinds = ["cat-file" in c and "cat-file" or "merge-base" in c and "merge-base"
             or "checkout" in c and "checkout" or "compose" in c and "compose"
             for c in calls]
    assert kinds == ["cat-file", "merge-base", "checkout", "compose"]


def test_op_rollback_refuses_non_string_sha(tmp_path):
    res = _run(ops.op_rollback(_cfg(tmp_path), 1234))  # bus could deliver a non-string
    assert res.ok is False
    assert "valid sha" in res.summary


def test_op_rollback_refuses_malformed_sha(tmp_path):
    res = _run(ops.op_rollback(_cfg(tmp_path), "-rf"))  # leading dash / not hex
    assert res.ok is False
    assert "valid sha" in res.summary



# --- op_verify (docker compose ps --format json) ---

def test_op_verify_ok_when_running_and_healthy(tmp_path, monkeypatch):
    async def fake_run(argv, cwd=None):
        return 0, '{"State": "running", "Health": ""}\n'
    monkeypatch.setattr(ops, "_run", fake_run)
    res = _run(ops.op_verify(_cfg(tmp_path)))
    assert res.ok is True
    assert "running" in res.summary


def test_op_verify_not_ok_when_unhealthy(tmp_path, monkeypatch):
    async def fake_run(argv, cwd=None):
        return 0, '{"State": "running", "Health": "unhealthy"}\n'
    monkeypatch.setattr(ops, "_run", fake_run)
    res = _run(ops.op_verify(_cfg(tmp_path)))
    assert res.ok is False
    assert "not healthy" in res.summary


def test_op_verify_not_ok_when_not_running(tmp_path, monkeypatch):
    async def fake_run(argv, cwd=None):
        return 0, '{"State": "exited", "Health": ""}\n'
    monkeypatch.setattr(ops, "_run", fake_run)
    res = _run(ops.op_verify(_cfg(tmp_path)))
    assert res.ok is False
    assert "not healthy" in res.summary


def test_op_verify_ok_when_output_is_json_array(tmp_path, monkeypatch):
    # Some Compose v2 builds emit a single-line JSON array instead of NDJSON.
    async def fake_run(argv, cwd=None):
        return 0, '[{"State": "running", "Health": "healthy"}]\n'
    monkeypatch.setattr(ops, "_run", fake_run)
    res = _run(ops.op_verify(_cfg(tmp_path)))
    assert res.ok is True
    assert "running" in res.summary


def test_op_verify_falls_back_to_exit_code_on_unparseable_output(tmp_path, monkeypatch):
    async def fake_run(argv, cwd=None):
        return 0, "not json at all"
    monkeypatch.setattr(ops, "_run", fake_run)
    res = _run(ops.op_verify(_cfg(tmp_path)))
    assert res.ok is True  # exit 0 + non-empty output


def test_op_verify_fallback_fails_on_nonzero_exit(tmp_path, monkeypatch):
    async def fake_run(argv, cwd=None):
        return 1, "not json at all"
    monkeypatch.setattr(ops, "_run", fake_run)
    res = _run(ops.op_verify(_cfg(tmp_path)))
    assert res.ok is False


# --- head_sha ---

def test_head_sha_returns_stripped_sha(tmp_path, monkeypatch):
    async def fake_run(argv, cwd=None):
        assert argv == ["git", "-C", str(tmp_path), "rev-parse", "--short", "HEAD"]
        return 0, "abc1234\n"
    monkeypatch.setattr(ops, "_run", fake_run)
    assert _run(ops.head_sha(_cfg(tmp_path))) == "abc1234"


def test_head_sha_returns_empty_on_error(tmp_path, monkeypatch):
    async def fake_run(argv, cwd=None):
        return 128, "fatal: not a git repository"
    monkeypatch.setattr(ops, "_run", fake_run)
    assert _run(ops.head_sha(_cfg(tmp_path))) == ""


def test_op_rollback_aborts_recreate_if_checkout_fails(tmp_path, monkeypatch):
    calls = []

    async def fake_run(argv, cwd=None):
        calls.append(argv)
        if "cat-file" in argv:
            return 0, ""
        if "merge-base" in argv:
            return 0, ""
        if "checkout" in argv:
            return 1, "error: local changes would be overwritten"
        raise AssertionError(f"recreate must not run: {argv}")

    monkeypatch.setattr(ops, "_run", fake_run)
    res = _run(ops.op_rollback(_cfg(tmp_path), "abc1234"))
    assert res.ok is False
    assert "checkout failed" in res.summary
    assert not any("compose" in c for c in calls)  # recreate never reached
