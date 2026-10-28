# tests/test_provider_command.py
"""Provider ``command`` (#84): deterministic node jobs, no LLM, manifest-only."""
import asyncio
import os
import sys
import time

import pytest

import manifest
import notif_store
import notif_sync
import notifications
import projects
import providers
import scheduler
from manifest import NotificationDecl
from notifications import Notification


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture
def ws(tmp_path, monkeypatch):
    """A resolvable project whose workspace is a real temp dir."""
    proj = projects.Project("trav", "T", "\U0001F30D", "", str(tmp_path))
    monkeypatch.setattr(projects, "resolve_project",
                        lambda key: proj if key == "trav" else None)
    return tmp_path


def _py(code: str) -> str:
    return f'"{sys.executable}" -c "{code}"'


def _notif(command: str, timeout=None, title="Job", project="trav") -> Notification:
    params: dict = {"project": project, "command": command}
    if timeout is not None:
        params["timeout_sec"] = timeout
    return Notification("trav-job", title, "command", "0 7 * * *", ("owner",), True, params)


def _fire(n: Notification):
    return _run(providers.run_provider(n, run_claude=None))


def _fail(n: Notification) -> str:
    # A broken job never reaches the chat: it raises, and _fire_once files the
    # reason in the outcome ledger (frontend + daily digest).
    with pytest.raises(providers.ProviderError) as exc:
        _fire(n)
    return str(exc.value)


def test_registered():
    assert "command" in providers.PROVIDERS
    assert "command" in notifications.VALID_PROVIDERS


def test_stdout_is_forwarded(ws):
    assert _fire(_notif(_py("print('ciao mondo')"))) == "ciao mondo"


def test_empty_stdout_is_silence(ws):
    assert _fire(_notif(_py("pass"))) is None


def test_nonzero_exit_reports_code_and_stderr_tail(ws):
    code = "import sys; print('out-ignorato'); sys.stderr.write('boom'); sys.exit(2)"
    msg = _fail(_notif(_py(code), title="Finestra"))
    assert msg.startswith("«Finestra» fallito (exit 2)")
    assert "boom" in msg
    assert "out-ignorato" not in msg


def test_nonzero_exit_falls_back_to_stdout_tail(ws):
    code = "import sys; print('solo-stdout'); sys.exit(1)"
    msg = _fail(_notif(_py(code)))
    assert "exit 1" in msg and "solo-stdout" in msg


def test_long_stderr_keeps_only_the_tail(ws):
    code = ("import sys\n"
            "for i in range(200): sys.stderr.write('riga%d\\n' % i)\n"
            "sys.exit(3)")
    script = ws / "noisy.py"
    script.write_text(code, encoding="utf-8")
    msg = _fail(_notif(f'"{sys.executable}" noisy.py'))
    assert "exit 3" in msg
    assert "riga199" in msg
    assert "riga100" not in msg
    assert len(msg) < 1500 + 100
    assert len(msg.splitlines()) <= 21


def test_timeout_kills_and_reports(ws):
    t0 = time.monotonic()
    msg = _fail(_notif(_py("import time; time.sleep(30)"), timeout=1, title="Lento"))
    assert time.monotonic() - t0 < 15
    assert msg == "«Lento» scaduto dopo 1 s"


def test_timeout_kills_the_process(ws, monkeypatch):
    killed = {}
    real = asyncio.create_subprocess_shell

    async def spy(*a, **kw):
        proc = await real(*a, **kw)
        orig_kill = proc.kill

        def kill():
            killed["yes"] = True
            orig_kill()
        proc.kill = kill
        return proc

    monkeypatch.setattr(providers.asyncio, "create_subprocess_shell", spy)
    _fail(_notif(_py("import time; time.sleep(30)"), timeout=1))
    assert killed.get("yes")


def test_runs_in_node_workspace(ws):
    out = _fire(_notif(_py("import os; print(os.getcwd())")))
    assert os.path.realpath(out) == os.path.realpath(str(ws))


def test_html_in_output_is_escaped(ws):
    out = _fire(_notif(_py("print('<b>a</b> & b')")))
    assert out == "&lt;b&gt;a&lt;/b&gt; &amp; b"


def test_error_text_is_plain_the_digest_escapes_it(ws):
    code = "import sys; sys.stderr.write('<oops> &'); sys.exit(1)"
    msg = _fail(_notif(_py(code), title="A<B"))
    assert "«A<B»" in msg and "<oops> &" in msg


def test_unresolvable_project_is_loud(ws):
    assert "progetto sconosciuto" in _fail(_notif("echo x", project="nope"))


def test_empty_command_is_loud(ws):
    assert "comando mancante" in _fail(_notif("  "))


def test_launch_failure_is_loud(ws, monkeypatch):
    async def boom(*a, **kw):
        raise OSError("niente shell")
    monkeypatch.setattr(providers.asyncio, "create_subprocess_shell", boom)
    assert "niente shell" in _fail(_notif("echo x"))


# --- manifest + sync -----------------------------------------------------------

def test_manifest_parses_command_and_timeout():
    p = projects.Project("trav", "T", "\U0001F30D", "", "/ws/trav")
    data = {"notification": [{
        "key": "trav-job", "provider": "command", "schedule": "0 7 * * *",
        "command": "sh x.sh", "timeout_sec": 120}]}
    (d,) = manifest._parse_notifications(data, p)
    assert d.command == "sh x.sh" and d.timeout_sec == 120.0


def test_manifest_defaults_without_command_fields():
    p = projects.Project("trav", "T", "\U0001F30D", "", "/ws/trav")
    data = {"notification": [{"key": "k", "provider": "static", "schedule": "0 7 * * *"}]}
    (d,) = manifest._parse_notifications(data, p)
    assert d.command == "" and d.timeout_sec is None


def test_decl_to_notification_maps_command_params(monkeypatch):
    monkeypatch.setattr(notif_sync, "_checked_recipients", lambda d: tuple(d.recipients))
    d = NotificationDecl("trav-job", "T", "command", "0 7 * * *", project="trav",
                         command="sh x.sh", timeout_sec=120.0, recipients=("owner",))
    n = notif_sync.decl_to_notification(d)
    assert n.params == {"project": "trav", "command": "sh x.sh", "timeout_sec": 120.0}
    d2 = NotificationDecl("trav-job", "T", "command", "0 7 * * *", project="trav",
                          command="sh x.sh", recipients=("owner",))
    assert "timeout_sec" not in notif_sync.decl_to_notification(d2).params


# --- validate ------------------------------------------------------------------

def test_validate_ok(ws):
    assert notifications.validate(_notif("echo x", timeout=60)) == []


@pytest.mark.parametrize("params,needle", [
    ({"project": "nope", "command": "x"}, "project"),
    ({"command": "x"}, "project"),
    ({"project": "trav", "command": "  "}, "'command' mancante"),
    ({"project": "trav", "command": "x", "timeout_sec": 0}, "timeout_sec"),
    ({"project": "trav", "command": "x", "timeout_sec": 3601}, "timeout_sec"),
    ({"project": "trav", "command": "x", "timeout_sec": "5"}, "timeout_sec"),
])
def test_validate_rejects(ws, params, needle):
    n = Notification("trav-job", "T", "command", "0 7 * * *", ("owner",), True, params)
    assert any(needle in e for e in notifications.validate(n))


# --- API: manifest-only ----------------------------------------------------------

def _client(monkeypatch, items):
    from fastapi.testclient import TestClient
    import api
    import instances
    store = {n.key: n for n in items}
    monkeypatch.setattr(notif_store, "load", lambda: list(store.values()))
    monkeypatch.setattr(notif_store, "get", lambda k: store.get(k))
    monkeypatch.setattr(notif_store, "upsert", lambda n: store.__setitem__(n.key, n))
    monkeypatch.setattr(notif_store, "load_runs", lambda: {})
    monkeypatch.setattr(api, "_next_run", lambda k: None)
    monkeypatch.setattr(api, "_manifest_keys", lambda: set())
    monkeypatch.setattr(instances, "known_ids", lambda env=None: ["personal"])
    monkeypatch.setattr(instances, "get",
                        lambda i, env=None: object() if i == "personal" else None)
    monkeypatch.setattr(scheduler, "apply", lambda n: None)
    return TestClient(api.app), store


_BODY = {"title": "T", "provider": "command", "schedule": "0 7 * * *",
         "recipients": ["owner"], "instance": "personal",
         "params": {"project": "trav", "command": "rm -rf /"}}


def test_api_refuses_create(ws, monkeypatch):
    client, store = _client(monkeypatch, [])
    r = client.post("/api/notifications", json=_BODY)
    assert r.status_code == 403
    assert not store


def test_api_refuses_preview(ws, monkeypatch):
    client, _ = _client(monkeypatch, [])
    assert client.post("/api/notifications/preview", json=_BODY).status_code == 403


def test_api_refuses_switching_provider_to_command(ws, monkeypatch):
    existing = Notification("s", "T", "static", "0 7 * * *", ("owner",), True, {"text": "x"})
    client, store = _client(monkeypatch, [existing])
    r = client.patch("/api/notifications/s", json={
        "instance": "personal", "provider": "command",
        "params": {"project": "trav", "command": "id"}})
    assert r.status_code == 403
    assert store["s"].provider == "static"


def test_api_refuses_editing_params_of_a_command(ws, monkeypatch):
    client, store = _client(monkeypatch, [_notif("echo ok")])
    r = client.patch("/api/notifications/trav-job", json={
        "instance": "personal", "params": {"project": "trav", "command": "id"}})
    assert r.status_code == 403
    assert store["trav-job"].params["command"] == "echo ok"


def test_api_still_allows_toggling_a_command(ws, monkeypatch):
    client, store = _client(monkeypatch, [_notif("echo ok")])
    r = client.patch("/api/notifications/trav-job",
                     json={"instance": "personal", "enabled": False})
    assert r.status_code == 200
    assert store["trav-job"].enabled is False


def test_api_meta_does_not_offer_command(monkeypatch):
    client, _ = _client(monkeypatch, [])
    providers_offered = client.get("/api/meta").json()["providers"]
    assert "command" not in providers_offered
    assert "static" in providers_offered


def test_api_refuses_agent_with_command(ws, monkeypatch):
    # An agent's `command` is a shell too: manifest only, like provider=command.
    client, store = _client(monkeypatch, [])
    body = {**_BODY, "provider": "agent",
            "params": {"project": "trav", "prompt": "p", "command": "id"}}
    assert client.post("/api/notifications", json=body).status_code == 403
    assert client.post("/api/notifications/preview", json=body).status_code == 403
    assert not store


def test_api_refuses_adding_command_to_an_agent(ws, monkeypatch):
    existing = Notification("a", "T", "agent", "0 7 * * *", ("owner",), True,
                            {"project": "trav", "prompt": "p"})
    client, store = _client(monkeypatch, [existing])
    r = client.patch("/api/notifications/a", json={
        "instance": "personal", "params": {"project": "trav", "prompt": "p", "command": "id"}})
    assert r.status_code == 403
    assert "command" not in store["a"].params


def test_manifest_agent_command_reaches_params():
    d = NotificationDecl(key="aa-n", title="T", provider="agent", schedule="*/30 * * * *",
                         project="aa", prompt="p", command="python3 x.py", timeout_sec=60)
    n = notif_sync.decl_to_notification(d)
    assert n.params["command"] == "python3 x.py" and n.params["timeout_sec"] == 60
