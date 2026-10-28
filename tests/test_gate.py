# tests/test_gate.py
"""Tests for the deterministic notification gate (gate.py) + its wiring."""
import asyncio
import sys

import gate
import manifest
import notif_sync
import projects
from manifest import NotificationDecl
from notifications import Notification


def _run(coro):
    return asyncio.run(coro)


def _notif(gate_cmd: str | None, project: str = "") -> Notification:
    params: dict = {"text": "ciao"}
    if project:
        params["project"] = project
    if gate_cmd is not None:
        params["gate"] = gate_cmd
    return Notification("brief", "T", "static", "0 8 * * *", (), True, params)


# --- evaluate_gate: the verdict carries WHY -----------------------------------

def test_verdict_open_has_no_blocking_detail():
    v = _run(gate.evaluate_gate(_notif("exit 0")))
    assert v.passed is True


def test_verdict_closed_reports_exit_code():
    v = _run(gate.evaluate_gate(_notif("exit 3")))
    assert v.passed is False
    assert "exit 3" in v.detail


def test_verdict_closed_reports_a_missing_gate_script():
    # The travel-* case: the gate script left the repo, so the gate exits
    # non-zero forever and the notification is silent. "not today" and "broken"
    # must not look the same.
    v = _run(gate.evaluate_gate(
        _notif(f'"{sys.executable}" /nodes/travel/gate-che-non-esiste.py')))
    assert v.passed is False
    assert "gate-che-non-esiste" in v.detail


def test_verdict_detail_is_one_bounded_line():
    # Multi-line, over-long gate output must not leak into an API response as-is.
    code = "import sys; sys.stderr.write((chr(65)*500 + chr(10))*3); sys.exit(1)"
    v = _run(gate.evaluate_gate(_notif(f'"{sys.executable}" -c "{code}"')))
    assert v.passed is False
    assert "\n" not in v.detail
    assert len(v.detail) <= gate.DETAIL_MAX_CHARS


# --- passes_gate behaviour ---------------------------------------------------

def test_no_gate_proceeds():
    assert _run(gate.passes_gate(_notif(None))) is True


def test_gate_exit_zero_proceeds():
    assert _run(gate.passes_gate(_notif("exit 0"))) is True


def test_gate_exit_nonzero_skips():
    assert _run(gate.passes_gate(_notif("exit 1"))) is False


def test_launch_failure_fails_closed(monkeypatch):
    async def boom(*a, **k):
        raise OSError("cannot spawn")
    monkeypatch.setattr(gate.asyncio, "create_subprocess_shell", boom)
    assert _run(gate.passes_gate(_notif("whatever"))) is False


def test_timeout_fails_closed(monkeypatch):
    class _HangProc:
        returncode = None

        async def communicate(self):
            await asyncio.sleep(10)

        def kill(self):
            pass

        async def wait(self):
            return 0

    async def fake_shell(*a, **k):
        return _HangProc()
    monkeypatch.setattr(gate.asyncio, "create_subprocess_shell", fake_shell)
    monkeypatch.setattr(gate, "GATE_TIMEOUT_SEC", 0.05)
    assert _run(gate.passes_gate(_notif("sleep 9"))) is False


def test_gate_runs_in_project_workspace(monkeypatch):
    captured = {}

    async def fake_shell(command, *, cwd, stdout, stderr):
        captured["cwd"] = cwd

        class _P:
            returncode = 0

            async def communicate(self):
                return b"", b""
        return _P()

    monkeypatch.setattr(gate.asyncio, "create_subprocess_shell", fake_shell)
    monkeypatch.setattr(projects, "resolve_project",
                        lambda key: projects.Project("trav", "T", "\U0001F30D", "", "/ws/trav"))
    assert _run(gate.passes_gate(_notif("ok", project="trav"))) is True
    assert captured["cwd"] == "/ws/trav"


# --- wiring: manifest parse + notif_sync propagation -------------------------

def test_manifest_parses_gate():
    p = projects.Project("trav", "T", "\U0001F30D", "", "/ws/trav")
    data = {"notification": [{
        "key": "brief", "provider": "agent", "schedule": "0 8 * * *",
        "to": "group", "prompt": "x", "gate": "python3 gate.py",
    }]}
    decls = manifest._parse_notifications(data, p)
    assert decls[0].gate == "python3 gate.py"


def test_gate_absent_defaults_empty():
    p = projects.Project("trav", "T", "\U0001F30D", "", "/ws/trav")
    data = {"notification": [{
        "key": "brief", "provider": "static", "schedule": "0 8 * * *", "text": "x",
    }]}
    assert manifest._parse_notifications(data, p)[0].gate == ""


def test_notif_sync_propagates_gate_to_params():
    decl = NotificationDecl(key="brief", title="T", provider="agent",
                            schedule="0 8 * * *", to="group", prompt="x",
                            project="trav", gate="python3 trip_gate.py")
    n = notif_sync.decl_to_notification(decl)
    assert n.params["gate"] == "python3 trip_gate.py"


def test_notif_sync_omits_empty_gate():
    decl = NotificationDecl(key="brief", title="T", provider="static",
                            schedule="0 8 * * *", to="dm", text="x")
    n = notif_sync.decl_to_notification(decl)
    assert "gate" not in n.params
