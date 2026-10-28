"""DeepSeek balance watchdog (Coordinator#95).

The scenario: 2026-10-22, every bus wake and scheduled job started failing with
``opencode: Insufficient Balance``. ``GET /user/balance`` says it in advance, so
this module must alert BEFORE the first failure, stay quiet while healthy, not
repeat hourly, and never put the API key in a message or an exception.
"""

import asyncio
import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest

import llm_balance
import notif_store
import notifications

NOW = datetime(2026, 10, 26, 12, 0, tzinfo=UTC)
ZERO = {
    "is_available": False,
    "balance_infos": [
        {"currency": "EUR", "total_balance": "0.00", "granted_balance": "0.00",
         "topped_up_balance": "0.00"},
    ],
}
FUNDED = {
    "is_available": True,
    "balance_infos": [{"currency": "EUR", "total_balance": "12.34"}],
}


def test_zero_balance_is_recognised_from_the_flag_and_from_the_amounts():
    assert llm_balance.parse_balance(ZERO) == (True, "EUR 0")
    assert llm_balance.parse_balance(FUNDED) == (False, "EUR 12.34")
    # no flag: the totals decide
    assert llm_balance.parse_balance({"balance_infos": [{"total_balance": "0"}]})[0] is True
    assert llm_balance.parse_balance({"balance_infos": [{"total_balance": "3"}]})[0] is False
    # an unreadable body must not page anyone
    assert llm_balance.parse_balance({})[0] is False
    assert llm_balance.parse_balance("nope")[0] is False


def test_alerts_on_the_transition_then_repeats_only_after_the_interval():
    text, state = llm_balance.decide(True, "EUR 0", {}, NOW)
    assert "Saldo DeepSeek esaurito" in text and "EUR 0" in text
    assert state["alarm"] is True and state["last_alert_iso"] == NOW.isoformat()

    quiet, same = llm_balance.decide(True, "EUR 0", state, NOW + timedelta(hours=1))
    assert quiet is None and same["last_alert_iso"] == state["last_alert_iso"]

    loud, _ = llm_balance.decide(True, "EUR 0", state, NOW + timedelta(hours=7))
    assert "esaurito" in loud


def test_recovery_is_said_once_and_only_after_an_alert():
    text, state = llm_balance.decide(True, "EUR 0", {}, NOW)
    back, healed = llm_balance.decide(False, "EUR 5", state, NOW + timedelta(minutes=5))
    assert "ripristinato" in back and healed["alarm"] is False
    again, _ = llm_balance.decide(False, "EUR 5", healed, NOW + timedelta(hours=1))
    assert again is None
    never, _ = llm_balance.decide(False, "EUR 5", {}, NOW)
    assert never is None


def _env(**over):
    base = {"AI_BACKEND": "opencode", "DEEPSEEK_API_KEY": "super-secret"}
    base.update(over)
    return base


def test_check_writes_the_alert_and_the_state_without_ever_naming_the_key(tmp_path):
    state_path = tmp_path / "balance.json"

    async def fake_fetch(key, **kw):
        assert key == "super-secret"
        return ZERO

    text = asyncio.run(llm_balance.check(
        now=NOW, env=_env(), state_path=state_path, fetch=fake_fetch))
    assert "esaurito" in text and "super-secret" not in text
    saved = json.loads(state_path.read_text())
    assert saved["alarm"] is True

    text = asyncio.run(llm_balance.check(
        now=NOW + timedelta(minutes=30), env=_env(), state_path=state_path, fetch=fake_fetch))
    assert text is None


def test_check_is_silent_when_the_backend_is_not_deepseek(tmp_path):
    called = []

    async def fake_fetch(*a, **k):
        called.append(1)
        return ZERO

    text = asyncio.run(llm_balance.check(
        env=_env(AI_BACKEND="claude"), state_path=tmp_path / "b.json", fetch=fake_fetch))
    assert text is None and called == []


def test_a_read_error_is_a_log_line_not_an_alarm(tmp_path, caplog):
    async def broken(*a, **k):
        raise llm_balance.BalanceError("HTTP 500")

    text = asyncio.run(llm_balance.check(
        env=_env(), state_path=tmp_path / "b.json", fetch=broken))
    assert text is None
    assert "super-secret" not in caplog.text


def test_fetch_uses_the_bearer_header_and_never_leaks_the_key_on_failure():
    seen = {}

    def handler(request):
        seen["auth"] = request.headers.get("Authorization")
        return httpx.Response(401, json={"error": "invalid key"})

    transport = httpx.MockTransport(handler)
    with pytest.raises(llm_balance.BalanceError) as err:
        asyncio.run(llm_balance.fetch_balance("super-secret", transport=transport))
    assert seen["auth"] == "Bearer super-secret"
    assert "HTTP 401" in str(err.value)
    assert "super-secret" not in str(err.value)


def test_fetch_parses_the_payload():
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=FUNDED))
    assert asyncio.run(llm_balance.fetch_balance("k", transport=transport)) == FUNDED


def test_ensure_watchdog_only_exists_where_deepseek_is_used(tmp_path, monkeypatch):
    monkeypatch.setattr(notif_store, "NOTIF_PATH", str(tmp_path / "notifications.json"))

    assert llm_balance.ensure_watchdog(_env(AI_BACKEND="claude")) is None
    assert llm_balance.ensure_watchdog(_env(NOTIF_HEALTH_DEST="")) is None

    key = llm_balance.ensure_watchdog(_env(NOTIF_HEALTH_DEST="123"))
    assert key == llm_balance.WATCHDOG_KEY
    notif = notif_store.get(key)
    assert notif.provider == "deepseek-balance"
    assert notif.destination == "123"
    assert notif.schedule == llm_balance.WATCHDOG_SCHEDULE

    # a manual disable survives a restart
    notif_store.upsert(type(notif)(**{**notif.__dict__, "enabled": False}))
    llm_balance.ensure_watchdog(_env(NOTIF_HEALTH_DEST="123"))
    assert notif_store.get(key).enabled is False


def test_the_provider_delegates_to_the_check(monkeypatch):
    import providers

    async def fake_check():
        return "🔴 saldo"

    monkeypatch.setattr(llm_balance, "check", fake_check)
    notif = notifications.Notification(
        key="k", title="t", provider="deepseek-balance",
        schedule="0 * * * *", recipients=(),
    )
    assert asyncio.run(providers.run_provider(notif, run_claude=None)) == "🔴 saldo"
