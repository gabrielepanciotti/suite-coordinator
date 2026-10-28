"""Sessioni solo per owner/admin (#89): niente bypass col testo di chi non lo e'."""
import asyncio
from types import SimpleNamespace

import pytest

import bot
import claude_runner


def _args(**kw):
    return claude_runner.claude_args(
        "ciao", persona=None, resume_id=None, model=None, effort=None, terminal_parity=False, **kw
    )[0]


def test_readonly_non_ha_bypass_ne_strumenti_di_scrittura():
    a = _args(readonly=True)
    assert "bypassPermissions" not in a
    assert a[a.index("--permission-mode") + 1] == "default"
    assert a[a.index("--tools") + 1] == "Read,Grep,Glob"
    assert "--strict-mcp-config" in a and "--mcp-config" not in a
    deny = a[a.index("--disallowedTools") + 1 :]
    assert any(d.startswith("Read(//m365") for d in deny)
    for banned in ("Bash", "Edit", "Write"):
        assert banned not in a[a.index("--tools") + 1]


def test_sessione_piena_resta_com_era():
    a = _args(readonly=False)
    assert a[a.index("--permission-mode") + 1] == "bypassPermissions"
    assert "--tools" not in a


def test_run_claude_readonly_non_va_su_opencode_ne_sull_host(monkeypatch):
    seen = {}

    async def fake_cli(prompt, **kw):
        seen.update(kw)
        return claude_runner.RunResult("ok", "sid")

    async def fake_oc(prompt, **kw):
        raise AssertionError("opencode non ha lista strumenti chiusa")

    monkeypatch.setattr(claude_runner, "_run_claude_cli", fake_cli)
    monkeypatch.setattr(claude_runner, "_run_opencode", fake_oc)
    monkeypatch.setattr(claude_runner, "host_exec_enabled", lambda: True)
    tok = claude_runner.SESSION_READONLY.set(True)
    try:
        asyncio.run(
            claude_runner.run_claude("x", workspace="/w", persona=None, backend="opencode", host_cwd="/h")
        )
    finally:
        claude_runner.SESSION_READONLY.reset(tok)
    assert "host_cwd" not in seen


def _upd(uid):
    return SimpleNamespace(effective_user=SimpleNamespace(id=uid, full_name="x"),
                           effective_chat=SimpleNamespace(id=1), effective_message=None)


def test_session_full_admin_si_ospite_no(monkeypatch):
    admin = SimpleNamespace(is_admin=True)
    guest_profile = SimpleNamespace(is_admin=False)
    monkeypatch.setattr(bot, "profile_for", lambda uid: {1: admin, 2: guest_profile}.get(uid))
    assert asyncio.run(bot._session_full(_upd(1))) is True
    assert asyncio.run(bot._session_full(_upd(2))) is False

    async def grant(role):
        return SimpleNamespace(ok=True, role=role)

    for role, expected in (("viewer", False), ("contributor", False), ("admin", True)):
        async def fake(update, project=None, team=None, _r=role):
            return await grant(_r)
        monkeypatch.setattr(bot, "_life_grant", fake)
        assert asyncio.run(bot._session_full(_upd(3))) is expected, role


def test_conferma_premuta_da_chi_non_e_admin_e_rifiutata(monkeypatch):
    answers = []

    async def answer(text=None, show_alert=False):
        answers.append(text)

    q = SimpleNamespace(data="x", answer=answer)
    upd = SimpleNamespace(callback_query=q, effective_user=SimpleNamespace(id=9, full_name="x"),
                          effective_chat=SimpleNamespace(id=1), effective_message=None)
    monkeypatch.setattr(bot, "profile_for", lambda uid: None)

    async def no_grant(update, project=None, team=None):
        return None

    monkeypatch.setattr(bot, "_life_grant", no_grant)
    called = []

    async def spy(*a, **k):
        called.append(a)

    monkeypatch.setattr(bot, "_invoke_into_topic", spy)
    asyncio.run(bot.on_confirm_callback(upd, SimpleNamespace()))
    assert called == [] and answers and "admin" in answers[0]
