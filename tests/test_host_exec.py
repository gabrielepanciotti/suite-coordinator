"""Issue #46: bus-woken sessions run on the host, like a terminal session."""

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from test_claude_runner import _FakeProc

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import host_claude_exec  # noqa: E402

WRAPPER = ROOT / "scripts" / "host_claude_exec.py"

# The wrapper is an sshd forced command on the Linux hosts: it runs claude via
# `bash -lc` in its own session and kills it with os.killpg. Neither exists on
# Windows, so running it there tests the platform, not the wrapper.
posix_only = pytest.mark.skipif(
    os.name == "nt", reason="host_claude_exec runs on Linux hosts (bash, killpg)"
)


# --- wrapper: request validation ---------------------------------------------


def test_wrapper_accepts_a_repo_under_an_allowed_root(tmp_path):
    repo = tmp_path / "work" / "Configuratore"
    (repo / ".git").mkdir(parents=True)
    cwd, args, env = host_claude_exec.validate_request(
        {"cwd": str(repo), "args": ["-p", "ciao"], "env": {"HUB_AGENT_ID": "x"}},
        [tmp_path / "work"],
    )
    assert cwd == repo.resolve()
    assert args == ["-p", "ciao"]
    assert env == {"HUB_AGENT_ID": "x"}


@pytest.mark.parametrize(
    "req, reason",
    [
        ({"cwd": "/etc", "args": []}, "outside the allowed roots"),
        ({"args": []}, "missing cwd"),
        ({"cwd": "__REPO__", "args": "rm -rf"}, "args must be a list"),
        ({"cwd": "__REPO__", "env": {"LD_PRELOAD": "/x.so"}}, "not allowed"),
        ({"cwd": "__REPO__/../../..", "args": []}, "outside the allowed roots"),
        (["not", "an", "object"], "not a JSON object"),
    ],
)
def test_wrapper_rejects_what_a_forced_command_must_not_run(tmp_path, req, reason):
    repo = tmp_path / "work" / "X"
    (repo / ".git").mkdir(parents=True)
    if isinstance(req, dict) and isinstance(req.get("cwd"), str):
        req = {**req, "cwd": req["cwd"].replace("__REPO__", str(repo))}
    with pytest.raises(ValueError, match=reason):
        host_claude_exec.validate_request(req, [tmp_path / "work"])


def _repo_root(tmp_path):
    repo = tmp_path / "work" / "X"
    (repo / ".git").mkdir(parents=True)
    return tmp_path / "work", repo


@pytest.mark.parametrize(
    "args",
    [
        ["-p", "x", "--settings", "/tmp/evil.json"],
        ["-p", "x", "--dangerously-skip-permissions"],
        ["-p", "x", "--mcp-config", "/tmp/m.json"],
        ["-p", "x", "--add-dir", "/"],
        ["-p", "x", "--plugin-dir", "/tmp/p"],
        ["-p", "--settings=/tmp/evil.json"],
        ["-p", "x", "--append-system-prompt", "--settings"],
        ["-p", "x", "--permission-mode", "plan2"],
        ["-p", "x", "--output-format", "text"],
        ["-p", "x", "--model", "opus; rm -rf /"],
        ["-p", "x", "--effort", "ultra"],
        ["-p", "x", "--resume", "../../etc/passwd"],
        ["-p"],
        ["some positional"],
    ],
)
def test_wrapper_rejects_flags_outside_the_allowlist(tmp_path, args):
    """Life#153 M6: the key may run claude with these flags and no others."""
    root, repo = _repo_root(tmp_path)
    with pytest.raises(ValueError):
        host_claude_exec.validate_request({"cwd": str(repo), "args": args}, [root])


def test_wrapper_accepts_exactly_what_the_coordinator_sends(tmp_path):
    root, repo = _repo_root(tmp_path)
    args = [
        "-p", "ciao", "--permission-mode", "bypassPermissions", "--output-format",
        "stream-json", "--verbose", "--model", "claude-sonnet-5-5", "--effort", "high",
        "--append-system-prompt", "persona", "--resume", "11111111-1111-1111-1111-111111111111",
    ]
    cwd, got, _ = host_claude_exec.validate_request({"cwd": str(repo), "args": args}, [root])
    assert got == args


def test_a_prompt_starting_with_a_dash_is_never_a_flag():
    import claude_runner

    args, _ = claude_runner.claude_args(
        "--settings /x", persona=None, resume_id=None, model=None, effort=None,
        terminal_parity=True,
    )
    assert not args[1].startswith("-")
    host_claude_exec.validate_args(args)


def test_wrapper_refuses_a_cwd_that_is_not_a_git_repo(tmp_path):
    root = tmp_path / "work"
    (root / "plain").mkdir(parents=True)
    with pytest.raises(ValueError, match="not inside a git repo"):
        host_claude_exec.validate_request({"cwd": str(root / "plain")}, [root])
    with pytest.raises(ValueError, match="not inside a git repo"):
        host_claude_exec.validate_request({"cwd": str(root)}, [root])


def test_wrapper_refuses_secret_and_vcs_directories(tmp_path):
    root, repo = _repo_root(tmp_path)
    for sub in (".git", "secrets", ".ssh"):
        (repo / sub).mkdir(exist_ok=True)
        with pytest.raises(ValueError, match="forbidden"):
            host_claude_exec.validate_request({"cwd": str(repo / sub)}, [root])


def test_wrapper_repo_allowlist(tmp_path, monkeypatch):
    root, repo = _repo_root(tmp_path)
    monkeypatch.setenv("HOST_EXEC_REPOS", "Other,Another")
    with pytest.raises(ValueError, match="not in the allowed list"):
        host_claude_exec.validate_request({"cwd": str(repo)}, [root])
    monkeypatch.setenv("HOST_EXEC_REPOS", "X")
    host_claude_exec.validate_request({"cwd": str(repo)}, [root])


def _trust_script(tmp_path, rc, msg="needs approval"):
    script = tmp_path / "node_trust.py"
    script.write_text(f"import sys\nsys.stderr.write({msg!r})\nsys.exit({rc})\n")
    return script


def test_unapproved_repo_is_refused(tmp_path, monkeypatch):
    """Life#153 M7: a fresh clone / third-party node does not get a session."""
    _, repo = _repo_root(tmp_path)
    monkeypatch.setenv("HOST_EXEC_NODE_TRUST_SCRIPT", str(_trust_script(tmp_path, 3)))
    with pytest.raises(ValueError, match="repo not approved: needs approval"):
        host_claude_exec.check_node_trust(repo)


def test_approved_repo_passes_and_warn_mode_only_warns(tmp_path, monkeypatch, capsys):
    _, repo = _repo_root(tmp_path)
    monkeypatch.setenv("HOST_EXEC_NODE_TRUST_SCRIPT", str(_trust_script(tmp_path, 0)))
    host_claude_exec.check_node_trust(repo)
    monkeypatch.setenv("HOST_EXEC_NODE_TRUST_SCRIPT", str(_trust_script(tmp_path, 3)))
    monkeypatch.setenv("HOST_EXEC_NODE_TRUST", "warn")
    host_claude_exec.check_node_trust(repo)
    assert "WARNING" in capsys.readouterr().err


def test_trust_check_is_skipped_when_the_script_is_missing(tmp_path, monkeypatch, capsys):
    _, repo = _repo_root(tmp_path)
    monkeypatch.setenv("HOST_EXEC_NODE_TRUST_SCRIPT", str(tmp_path / "nope.py"))
    host_claude_exec.check_node_trust(repo)
    assert "skipped" in capsys.readouterr().err


def test_wrapper_runs_the_binary_behind_a_login_shell():
    cmd = host_claude_exec.build_command(["-p", "x y"])
    assert cmd[:2] == ["bash", "-lc"]
    # args travel as argv, never re-parsed by a shell
    assert cmd[-2:] == ["-p", "x y"]


# --- wrapper: real process, end to end ---------------------------------------


def _fake_claude(tmp_path, body):
    script = tmp_path / "fake-claude"
    script.write_text("#!/usr/bin/env bash\n" + body)
    script.chmod(0o755)
    return script


def _spawn_wrapper(tmp_path, fake):
    env = {
        **os.environ,
        "HOST_EXEC_ROOTS": str(tmp_path),
        "HOST_EXEC_CLAUDE_BIN": str(fake),
    }
    return subprocess.Popen(
        [sys.executable, str(WRAPPER)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )


@posix_only
def test_wrapper_streams_stdout_from_the_real_cwd_with_hub_env(tmp_path):
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    fake = _fake_claude(tmp_path, 'echo "$PWD|$HUB_AGENT_ID|$*"\n')
    proc = _spawn_wrapper(tmp_path, fake)
    req = {"cwd": str(repo), "args": ["-p", "ciao mondo"], "env": {"HUB_AGENT_ID": "a1"}}
    proc.stdin.write((json.dumps(req) + "\n").encode())
    proc.stdin.flush()
    # stdin stays open, as ssh keeps it: closing it would mean "client gone"
    out = proc.stdout.read()
    assert proc.wait(timeout=20) == 0
    proc.stdin.close()
    assert out.decode().strip() == f"{repo.resolve()}|a1|-p ciao mondo"


@posix_only
def test_wrapper_kills_claude_when_the_client_goes_away(tmp_path):
    """EOF on stdin = the container gave up: nothing may keep running."""
    marker = tmp_path / "still-alive"
    (tmp_path / ".git").mkdir()
    fake = _fake_claude(tmp_path, f'echo started; sleep 30; touch "{marker}"\n')
    proc = _spawn_wrapper(tmp_path, fake)
    proc.stdin.write((json.dumps({"cwd": str(tmp_path), "args": []}) + "\n").encode())
    proc.stdin.flush()
    assert proc.stdout.readline().strip() == b"started"
    proc.stdin.close()
    proc.wait(timeout=20)
    time.sleep(0.5)
    assert not marker.exists()


def test_wrapper_refuses_a_bad_request_without_running_anything(tmp_path):
    marker = tmp_path / "ran"
    fake = _fake_claude(tmp_path, f'touch "{marker}"\n')
    proc = _spawn_wrapper(tmp_path, fake)
    out, err = proc.communicate(json.dumps({"cwd": "/etc"}).encode() + b"\n", timeout=20)
    assert proc.returncode == host_claude_exec.EXIT_BAD_REQUEST
    assert b"rejected" in err
    assert not marker.exists()


# --- runner ------------------------------------------------------------------


def test_terminal_parity_drops_every_lean_boot_override(monkeypatch):
    import claude_runner

    monkeypatch.setattr(claude_runner, "CLAUDE_MODEL", "sonnet")
    monkeypatch.setattr(claude_runner, "CLAUDE_SETTING_SOURCES", "project,local")
    monkeypatch.setattr(claude_runner, "CLAUDE_STRICT_MCP", True)
    args, model = claude_runner.claude_args(
        "p", persona=None, resume_id="s1", model=None, effort=None, terminal_parity=True
    )
    assert model is None
    for flag in ("--model", "--setting-sources", "--strict-mcp-config", "--mcp-config"):
        assert flag not in args
    assert args[-2:] == ["--resume", "s1"]
    # an EXPLICIT choice still wins: somebody asked for it
    args, model = claude_runner.claude_args(
        "p", persona=None, resume_id=None, model="opus", effort=None, terminal_parity=True
    )
    assert model == "opus" and "--model" in args


def test_container_runs_keep_the_lean_boot(monkeypatch):
    import claude_runner

    monkeypatch.setattr(claude_runner, "CLAUDE_MODEL", "sonnet")
    monkeypatch.setattr(claude_runner, "CLAUDE_SETTING_SOURCES", "project,local")
    args, model = claude_runner.claude_args(
        "p", persona=None, resume_id=None, model=None, effort=None, terminal_parity=False
    )
    assert model == "sonnet"
    assert "--setting-sources" in args


def test_host_request_carries_only_bus_coordinates():
    import claude_runner

    line = claude_runner.host_exec_request(
        ["-p", "x"],
        host_cwd="/nodes/X",
        extra_env={"HUB_URL": "http://bus", "PATH": "/evil", "HUB_AGENT_KEY": None},
    )
    req = json.loads(line)
    assert req == {"cwd": "/nodes/X", "args": ["-p", "x"], "env": {"HUB_URL": "http://bus"}}


class _HostProc(_FakeProc):
    def __init__(self, lines):
        super().__init__(lines)
        self.stdin = _FakeStdin()


class _FakeStdin:
    def __init__(self):
        self.data = b""
        self.closed = False

    def write(self, b):
        self.data += b

    async def drain(self):
        pass

    def is_closing(self):
        return self.closed

    def close(self):
        self.closed = True


async def test_run_claude_goes_through_ssh_when_host_exec_is_on(monkeypatch):
    import claude_runner

    monkeypatch.setattr(claude_runner, "HOST_EXEC_SSH", "dev@host.docker.internal")
    proc = _HostProc([b'{"type":"result","result":"fatto","session_id":"s9"}\n'])
    calls = []

    async def fake_exec(*argv, **kwargs):
        calls.append((list(argv), kwargs))
        return proc

    monkeypatch.setattr(claude_runner.asyncio, "create_subprocess_exec", fake_exec)
    text, sid = await claude_runner.run_claude(
        "ciao",
        workspace="/workspaces/configuratore",
        persona=None,
        extra_env={"HUB_AGENT_ID": "configuratore-dev-linux"},
        host_cwd="/nodes/Configuratore",
    )
    assert (text, sid) == ("fatto", "s9")
    argv, kwargs = calls[0]
    assert argv[0] == "ssh" and argv[-2:] == ["dev@host.docker.internal", claude_runner.HOST_EXEC_FAIL_CLOSED]
    assert "cwd" not in kwargs  # the host path does not exist in the container
    req = json.loads(proc.stdin.data)
    assert req["cwd"] == "/nodes/Configuratore"
    assert req["env"] == {"HUB_AGENT_ID": "configuratore-dev-linux"}
    assert "--model" not in req["args"]
    assert proc.stdin.closed


async def test_host_cwd_is_ignored_when_host_exec_is_off(monkeypatch):
    import claude_runner

    monkeypatch.setattr(claude_runner, "HOST_EXEC_SSH", "")
    proc = _FakeProc([b'{"type":"result","result":"ok","session_id":"s1"}\n'])
    calls = []

    async def fake_exec(*argv, **kwargs):
        calls.append((list(argv), kwargs))
        return proc

    monkeypatch.setattr(claude_runner.asyncio, "create_subprocess_exec", fake_exec)
    await claude_runner.run_claude(
        "ciao", workspace="/workspaces/x", persona=None, host_cwd="/nodes/X"
    )
    argv, kwargs = calls[0]
    assert argv[0] == claude_runner.CLAUDE_BIN
    assert kwargs["cwd"] == "/workspaces/x"


# --- hub wake ----------------------------------------------------------------


def test_wake_runs_on_host_only_with_host_exec_and_a_host_path(monkeypatch):
    import claude_runner
    import hub

    proj = SimpleNamespace(host_path="/nodes/Configuratore", workspace="/workspaces/c")
    monkeypatch.setattr(claude_runner, "HOST_EXEC_SSH", "")
    assert hub.wake_host_cwd(proj) is None
    monkeypatch.setattr(claude_runner, "HOST_EXEC_SSH", "dev@h")
    assert hub.wake_host_cwd(proj) == "/nodes/Configuratore"
    # a node not cloned on this device stays in the container
    assert hub.wake_host_cwd(SimpleNamespace(host_path="", workspace="/w")) is None
