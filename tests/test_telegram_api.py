import pytest

import telegram_api


class Rec:
    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        def call(*a):
            self.calls.append((name, *a))
            return self
        return call


def test_no_env_leaves_builder_untouched():
    b = Rec()
    assert telegram_api.apply_api_server(b, {}) is b
    assert b.calls == []
    assert telegram_api.api_server_config({}) is None
    assert telegram_api.api_root({}) == "https://api.telegram.org"


def test_env_applies_three_calls():
    b = Rec()
    env = {"TELEGRAM_BOT_API_URL": "http://telegram-bot-api:8081/", "TELEGRAM_LOCAL_MODE": "1"}
    telegram_api.apply_api_server(b, env)
    assert b.calls == [
        ("base_url", "http://telegram-bot-api:8081/bot"),
        ("base_file_url", "http://telegram-bot-api:8081/file/bot"),
        ("local_mode", True),
    ]
    assert telegram_api.api_root(env) == "http://telegram-bot-api:8081"


def test_local_mode_can_be_disabled():
    b = Rec()
    telegram_api.apply_api_server(
        b, {"TELEGRAM_BOT_API_URL": "http://x:1", "TELEGRAM_LOCAL_MODE": "0"})
    assert [c[0] for c in b.calls] == ["base_url", "base_file_url"]


def test_real_builder_roundtrip(monkeypatch):
    from telegram.ext import Application
    monkeypatch.setenv("TELEGRAM_BOT_API_URL", "http://telegram-bot-api:8081")
    app = telegram_api.apply_api_server(Application.builder().token("1:" + "a" * 30)).build()
    assert app.bot.base_url == "http://telegram-bot-api:8081/bot" + "1:" + "a" * 30
    assert app.bot.local_mode is True


def _mig():
    import importlib.util, pathlib
    spec = importlib.util.spec_from_file_location(
        "tlm", pathlib.Path(__file__).resolve().parents[1] / "scripts" / "telegram_local_migrate.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


TOK = "123456:" + "A" * 30


def test_migrate_tokens_never_printed(monkeypatch, capsys):
    mod = _mig()
    assert mod.bot_tokens({"TELEGRAM_BOT_TOKEN": TOK}) == [("default", TOK)]
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOK)
    assert mod.main(["--dry-run"]) == 0
    assert TOK not in capsys.readouterr().out


def _fake_urlopen(status, body):
    import io, json, urllib.error

    def fake(req, timeout=0):
        data = json.dumps(body).encode()
        if status == 200:
            return io.BytesIO(data)
        raise urllib.error.HTTPError(req.full_url, status, "x", {}, io.BytesIO(data))
    return fake


@pytest.mark.parametrize("status,body,expected", [
    (200, {"ok": True, "result": True}, "ok"),
    (400, {"ok": False, "description": "Bad Request: logged out"}, "maybe"),
    (401, {"ok": False, "description": "Unauthorized"}, "error: HTTP 401 (Unauthorized)"),
    (404, {"ok": False, "description": "Not Found"}, "error: HTTP 404 (Not Found)"),
])
def test_log_out_verdicts(monkeypatch, status, body, expected):
    mod = _mig()
    monkeypatch.setattr(mod.urllib.request, "urlopen", _fake_urlopen(status, body))
    assert mod.log_out(TOK) == expected


def test_main_exit_codes_and_no_token_leak(monkeypatch, capsys):
    mod = _mig()
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOK)
    monkeypatch.setattr(mod.urllib.request, "urlopen", _fake_urlopen(401, {"description": "Unauthorized"}))
    assert mod.main([]) == 1
    monkeypatch.setattr(mod.urllib.request, "urlopen", _fake_urlopen(400, {"description": "x"}))
    assert mod.main([]) == 0
    assert TOK not in capsys.readouterr().out


def test_check_local_and_logout_from(monkeypatch, capsys):
    mod = _mig()
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOK)
    seen = []
    ok = _fake_urlopen(200, {"ok": True})

    def spy(req, timeout=0):
        seen.append(req.full_url.replace(TOK, "<T>"))
        return ok(req, timeout)
    monkeypatch.setattr(mod.urllib.request, "urlopen", spy)
    assert mod.main(["--check-local", "http://local:8081"]) == 0
    assert mod.main(["--logout-from", "http://local:8081"]) == 0
    assert seen == ["http://local:8081/bot<T>/getMe", "http://local:8081/bot<T>/logOut"]
    monkeypatch.setattr(mod.urllib.request, "urlopen", _fake_urlopen(401, {}))
    assert mod.main(["--check-local", "http://local:8081"]) == 1
    assert TOK not in capsys.readouterr().out


@pytest.mark.parametrize("argv", [
    ["--check-local"],
    ["--logout-from"],
    ["--check-local", "--dry-run"],
    ["--logout-from", "--dry-run"],
    ["--dry-run", "--logout-from", "-x"],
])
def test_bad_option_value_is_usage_error_before_network(monkeypatch, capsys, argv):
    mod = _mig()
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOK)

    def no_network(*a, **k):
        raise AssertionError("network called")
    monkeypatch.setattr(mod.urllib.request, "urlopen", no_network)
    assert mod.main(argv) == 2
    assert "usage:" in capsys.readouterr().err


def test_per_bot_override_sends_one_bot_to_the_cloud():
    env = {"TELEGRAM_BOT_API_URL": "http://local:8081", "TELEGRAM_BOT_API_URL_COMUNITY": "cloud"}
    assert telegram_api.api_server_config(env, "comunity") is None
    assert telegram_api.api_root(env, "comunity") == "https://api.telegram.org"
    # every other bot, and the default one, keep the local server
    assert telegram_api.api_server_config(env, "chef")["url"] == "http://local:8081"
    assert telegram_api.api_server_config(env)["url"] == "http://local:8081"
    b = Rec()
    assert telegram_api.apply_api_server(b, env, "comunity") is b and b.calls == []


def test_per_bot_override_can_point_to_another_server_and_mode():
    env = {
        "TELEGRAM_BOT_API_URL": "http://local:8081",
        "TELEGRAM_BOT_API_URL_TRAVEL_X": "http://other:9/",
        "TELEGRAM_LOCAL_MODE_TRAVEL_X": "0",
    }
    conf = telegram_api.api_server_config(env, "travel-x")
    assert conf == {"url": "http://other:9", "local_mode": False}
    assert telegram_api.api_server_config(env, "chef")["local_mode"] is True
