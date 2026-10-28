# tests/test_migrate_bot.py
import asyncio
import types

import bot


def _run(coro):
    return asyncio.run(coro)


def test_parse_migrate_args_project_and_optional_rev():
    assert bot._parse_migrate_args("db head") == ("db", "head")
    assert bot._parse_migrate_args("db") == ("db", "head")        # rev defaults to head
    assert bot._parse_migrate_args("") == (None, "head")
    assert bot._parse_migrate_args("db rev123 extra") == ("db", "rev123")  # 2nd token only


class _Msg:
    def __init__(self):
        self.replies: list[str] = []

    async def reply_text(self, text, *a, **k):
        self.replies.append(text)


class _Update:
    def __init__(self):
        self.message = _Msg()


class _Ctx:
    def __init__(self, args):
        self.args = args


class _Cfg:
    prod_device = "minipc"

    def prod_agents(self, key, aliases=()):
        return (f"{key}-minipc",)


def _wire(monkeypatch, targets):
    """Common wiring; returns a dict that captures the start_migration call."""
    monkeypatch.setattr(bot, "_allowed", lambda u: True)
    monkeypatch.setattr(
        bot, "resolve_project",
        lambda k: types.SimpleNamespace(key=k, label=k.capitalize()))
    monkeypatch.setattr(bot.manifest, "db_targets_of", lambda p: targets)
    monkeypatch.setattr(bot.hub, "_S", {"config": _Cfg()})
    monkeypatch.setattr(bot.hub, "_default_tenant", lambda: "acme")
    captured: dict = {}

    async def fake_start(tenant, key, rev, targets):
        captured.update(tenant=tenant, key=key, rev=rev, targets=targets)
        return 7

    monkeypatch.setattr(bot.migrate, "start_migration", fake_start)
    return captured


def test_cmd_migrate_derives_implicit_target_when_none_declared(monkeypatch):
    captured = _wire(monkeypatch, targets=[])
    upd = _Update()
    _run(bot.cmd_migrate(upd, _Ctx(["coordinator", "head"])))
    # A single target derived from the registry: prod identity on the prod device.
    assert len(captured["targets"]) == 1
    t = captured["targets"][0]
    assert (t.agent, t.host) == ("coordinator-minipc", "minipc")
    assert "derivato dal registro" in upd.message.replies[-1]


def test_cmd_migrate_uses_explicit_targets_without_derivation(monkeypatch):
    explicit = [bot.manifest.DbTargetDecl(agent="db-prod", host="edge")]
    captured = _wire(monkeypatch, targets=explicit)
    upd = _Update()
    _run(bot.cmd_migrate(upd, _Ctx(["db", "head"])))
    assert captured["targets"] == explicit
    assert "derivato dal registro" not in upd.message.replies[-1]


def test_cmd_migrate_refuses_when_no_targets_and_no_registry(monkeypatch):
    _wire(monkeypatch, targets=[])
    monkeypatch.setattr(bot.hub, "_S", {})  # no config → registro non disponibile
    upd = _Update()
    _run(bot.cmd_migrate(upd, _Ctx(["coordinator", "head"])))
    assert "registro non disponibile" in upd.message.replies[-1]
