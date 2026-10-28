import pytest
from unittest.mock import AsyncMock, MagicMock
from taskgen import run_watcher, build_introspection_seed, Dedup
from taskgen_config import Watcher, TrustPolicy

pytestmark = pytest.mark.asyncio

def _watcher():
    return Watcher(key="cheftoni", node_key="personalChef", cadence="cron:0 11 * * *",
                   pre_gate="temporal", enabled=True, recipient_profile="owner")

async def test_run_watcher_trusted_creates_scheduled_task():
    run_claude = AsyncMock(return_value=(
        '{"suggestions":[{"title":"Cena zucchine","category":"cucina",'
        '"when_iso":"2026-07-07T20:00:00+02:00","duration_min":45,'
        '"description":"scadono","why":"scadenza"}]}', "sid"))
    hw = MagicMock()
    hw.create_scheduled_task = AsyncMock(return_value={"id": 9, "notification_state": "pending"})
    hw.create_proposal = AsyncMock()
    notices = []
    policy = TrustPolicy(categories={"cucina": "trusted"}, daily_cap=2)
    out = await run_watcher(
        _watcher(), run_claude=run_claude, hw_client=hw, policy=policy,
        dedup=Dedup(), resolve_workspace=lambda k: "G:/ChefToni",
        send_notice=AsyncMock(side_effect=lambda **k: notices.append(k)),
        telegram_chat_id=542, now=1_000_000.0)
    assert out["trusted"] == 1
    hw.create_scheduled_task.assert_awaited_once()
    hw.create_proposal.assert_not_awaited()

async def test_run_watcher_unknown_category_creates_proposal():
    run_claude = AsyncMock(return_value=(
        '{"suggestions":[{"title":"Corri","category":"sport",'
        '"when_iso":"2026-07-07T18:00:00+02:00","duration_min":30,'
        '"description":null,"why":null}]}', "sid"))
    hw = MagicMock()
    hw.create_scheduled_task = AsyncMock()
    hw.create_proposal = AsyncMock(return_value={"id": 3})
    policy = TrustPolicy(categories={"cucina": "trusted"}, daily_cap=2)
    out = await run_watcher(
        _watcher(), run_claude=run_claude, hw_client=hw, policy=policy,
        dedup=Dedup(), resolve_workspace=lambda k: "G:/ChefToni",
        send_notice=AsyncMock(), telegram_chat_id=542, now=1_000_000.0,
        default_category_id=1)
    assert out["proposed"] == 1
    hw.create_proposal.assert_awaited_once()
    hw.create_scheduled_task.assert_not_awaited()
    _, kwargs = hw.create_proposal.call_args
    payload = kwargs["payload"]
    assert payload["name"] == "Corri"
    assert payload["category_id"] == 1
    assert payload["duration_estimated"] == 30
    assert payload["schedule_mode"] == "weekly_count"
    assert payload["weekly_count"] == 1


async def test_run_watcher_propose_without_default_category_id_fails():
    run_claude = AsyncMock(return_value=(
        '{"suggestions":[{"title":"Corri","category":"sport",'
        '"when_iso":"2026-07-07T18:00:00+02:00","duration_min":30,'
        '"description":null,"why":null}]}', "sid"))
    hw = MagicMock()
    hw.create_scheduled_task = AsyncMock()
    hw.create_proposal = AsyncMock(return_value={"id": 3})
    policy = TrustPolicy(categories={"cucina": "trusted"}, daily_cap=2)
    out = await run_watcher(
        _watcher(), run_claude=run_claude, hw_client=hw, policy=policy,
        dedup=Dedup(), resolve_workspace=lambda k: "G:/ChefToni",
        send_notice=AsyncMock(), telegram_chat_id=542, now=1_000_000.0,
        default_category_id=None)
    assert out["failed"] >= 1
    assert out["proposed"] == 0
    hw.create_proposal.assert_not_awaited()

async def test_run_watcher_dedup_skips_second_run():
    payload = ('{"suggestions":[{"title":"Cena","category":"cucina",'
               '"when_iso":"2026-07-07T20:00:00+02:00","duration_min":45,'
               '"description":null,"why":null}]}', "sid")
    run_claude = AsyncMock(return_value=payload)
    hw = MagicMock(); hw.create_scheduled_task = AsyncMock(return_value={"id": 1}); hw.create_proposal = AsyncMock()
    dedup = Dedup(); policy = TrustPolicy(categories={"cucina": "trusted"}, daily_cap=5)
    kw = dict(run_claude=run_claude, hw_client=hw, policy=policy, dedup=dedup,
              resolve_workspace=lambda k: "G:/ChefToni", send_notice=AsyncMock(),
              telegram_chat_id=542, now=1_000_000.0)
    await run_watcher(_watcher(), **kw)
    await run_watcher(_watcher(), **kw)
    assert hw.create_scheduled_task.await_count == 1  # second run deduped

async def test_run_watcher_isolates_failing_suggestion():
    run_claude = AsyncMock(return_value=(
        '{"suggestions":['
        '{"title":"Cena zucchine","category":"cucina",'
        '"when_iso":"2026-07-07T20:00:00+02:00","duration_min":45,'
        '"description":"scadono","why":"scadenza"},'
        '{"title":"Pasta al forno","category":"cucina",'
        '"when_iso":"2026-07-08T20:00:00+02:00","duration_min":30,'
        '"description":null,"why":null}'
        ']}', "sid"))
    calls = {"n": 0}

    async def _create_scheduled_task(**kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("boom")
        return {"id": 5, "notification_state": "pending"}

    hw = MagicMock()
    hw.create_scheduled_task = AsyncMock(side_effect=_create_scheduled_task)
    hw.create_proposal = AsyncMock()
    dedup = Dedup()
    policy = TrustPolicy(categories={"cucina": "trusted"}, daily_cap=5)
    kw = dict(run_claude=run_claude, hw_client=hw, policy=policy, dedup=dedup,
              resolve_workspace=lambda k: "G:/ChefToni", send_notice=AsyncMock(),
              telegram_chat_id=542, now=1_000_000.0)

    out = await run_watcher(_watcher(), **kw)

    assert out["failed"] == 1
    assert out["trusted"] == 1
    assert hw.create_scheduled_task.await_count == 2

    # second run: the failed suggestion (not recorded in dedup) is retried
    out2 = await run_watcher(_watcher(), **kw)
    assert hw.create_scheduled_task.await_count == 3

def test_introspection_seed_mentions_json():
    seed = build_introspection_seed("personalChef")
    assert "suggestions" in seed
    assert "JSON" in seed or "json" in seed

async def test_run_watcher_trusted_resolves_real_category_id():
    run_claude = AsyncMock(return_value=(
        '{"suggestions":[{"title":"Cena","category":"cucina",'
        '"when_iso":"2026-07-07T20:00:00+02:00","duration_min":45,'
        '"description":null,"why":null}]}', "sid"))
    hw = MagicMock()
    hw.create_scheduled_task = AsyncMock(return_value={"id": 9})
    hw.create_proposal = AsyncMock()
    hw.list_categories = AsyncMock(return_value=[{"id": 42, "name": "cucina"}])
    policy = TrustPolicy(categories={"cucina": "trusted"}, daily_cap=2)
    await run_watcher(_watcher(), run_claude=run_claude, hw_client=hw, policy=policy,
        dedup=Dedup(), resolve_workspace=lambda k: "G:/ChefToni",
        send_notice=AsyncMock(), telegram_chat_id=542, now=1_000_000.0)
    _, kwargs = hw.create_scheduled_task.call_args
    assert kwargs["category_id"] == 42
