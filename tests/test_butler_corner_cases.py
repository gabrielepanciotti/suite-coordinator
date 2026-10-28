"""Corner cases for butler_reorg / butler_prep / butler_config (Task 8).

Pure/mocked, deterministic — no real I/O, no bot import.
"""

from datetime import datetime, timedelta, timezone

import pytest
from unittest.mock import AsyncMock, MagicMock

from butler_config import (
    ButlerPolicy,
    ButlerWatcher,
    load_butler_policy,
    load_butler_watchers,
)
from butler_reorg import _dedup_probe, parse_reorg_proposals, run_reorg_watcher
from butler_prep import run_prep_watcher
from taskgen import Dedup


# --- reorg ---------------------------------------------------------------


def _reorg_watcher(**overrides):
    kw = dict(
        key="reorg-owner", kind="reorg", cadence="cron:0 8 * * *",
        horizon_hours=48, enabled=True, recipient_profile="owner",
        node_key="scheduler",
    )
    kw.update(overrides)
    return ButlerWatcher(**kw)


def _reorg_kw(**overrides):
    base = dict(
        run_claude=AsyncMock(return_value=('{"proposals":[]}', "sid")),
        hw_client=None,
        dedup=Dedup(),
        resolve_workspace=lambda k: "X",
        send_reorg_card=AsyncMock(),
        telegram_chat_id=542,
        now=1_000_000.0,
        policy=ButlerPolicy(),
    )
    base.update(overrides)
    return base


def test_dedup_probe_empty_st_ids_is_stable_no_crash():
    probe1 = _dedup_probe("sposta", [], "2026-07-10T19:00:00+02:00")
    probe2 = _dedup_probe("sposta", [], "2026-07-10T19:00:00+02:00")
    assert probe1.when_iso == probe2.when_iso == ""
    dedup = Dedup()
    assert dedup.seen(probe1, now=1_000_000.0) is False
    dedup.record(probe1, now=1_000_000.0)
    assert dedup.seen(probe2, now=1_000_000.0) is True


def test_dedup_probe_duplicate_st_ids_is_stable():
    # duplicates in input order shouldn't change the resulting key ordering.
    probe_a = _dedup_probe("accorpa", [2, 1, 2], "2026-07-10T19:00:00+02:00")
    probe_b = _dedup_probe("accorpa", [1, 2, 2], "2026-07-10T19:00:00+02:00")
    assert probe_a.when_iso == probe_b.when_iso == "1,2,2"
    dedup = Dedup()
    dedup.record(probe_a, now=1_000_000.0)
    assert dedup.seen(probe_b, now=1_000_000.0) is True


@pytest.mark.asyncio
async def test_reorg_watcher_survives_empty_and_duplicate_st_ids():
    hw = AsyncMock()
    hw.list_scheduled_tasks = AsyncMock(return_value=[{"id": 1, "name": "A"}])
    text = (
        '{"proposals":['
        '{"kind":"accorpa","st_ids":[],"new_start_iso":"2026-07-10T19:00:00+02:00"},'
        '{"kind":"sposta","st_ids":[2,1,2],"new_start_iso":"2026-07-10T20:00:00+02:00"}'
        ']}'
    )
    send_reorg_card = AsyncMock()
    kw = _reorg_kw(
        hw_client=hw,
        run_claude=AsyncMock(return_value=(text, "sid")),
        send_reorg_card=send_reorg_card,
    )
    out = await run_reorg_watcher(_reorg_watcher(), **kw)
    assert out == {"proposed": 2, "applied": 0, "skipped": 0, "failed": 0}
    assert send_reorg_card.await_count == 2


@pytest.mark.asyncio
async def test_reorg_watcher_non_iso_new_start_does_not_crash():
    hw = AsyncMock()
    hw.list_scheduled_tasks = AsyncMock(return_value=[{"id": 1, "name": "A"}])
    text = '{"proposals":[{"kind":"sposta","st_ids":[1],"new_start_iso":"not-a-date"}]}'
    send_reorg_card = AsyncMock()
    kw = _reorg_kw(
        hw_client=hw,
        run_claude=AsyncMock(return_value=(text, "sid")),
        send_reorg_card=send_reorg_card,
    )
    out = await run_reorg_watcher(_reorg_watcher(), **kw)
    # the watcher itself never parses new_start_iso as a date — it's passed
    # through as-is to send_reorg_card; reschedule (if any) happens downstream.
    assert out == {"proposed": 1, "applied": 0, "skipped": 0, "failed": 0}
    proposal, _chat_id = send_reorg_card.await_args.args
    assert proposal["new_start_iso"] == "not-a-date"


@pytest.mark.asyncio
async def test_reorg_dedup_ttl_expired_reproposes():
    hw = AsyncMock()
    hw.list_scheduled_tasks = AsyncMock(return_value=[{"id": 1, "name": "A"}])
    text = '{"proposals":[{"kind":"sposta","st_ids":[1],"new_start_iso":"2026-07-10T19:00:00+02:00"}]}'
    dedup = Dedup(ttl_seconds=60)
    send_reorg_card = AsyncMock()
    kw = _reorg_kw(
        hw_client=hw,
        run_claude=AsyncMock(return_value=(text, "sid")),
        dedup=dedup,
        send_reorg_card=send_reorg_card,
        now=1_000_000.0,
    )
    out1 = await run_reorg_watcher(_reorg_watcher(), **kw)
    assert out1["proposed"] == 1

    kw2 = dict(kw, now=1_000_000.0 + 61)
    out2 = await run_reorg_watcher(_reorg_watcher(), **kw2)
    assert out2["proposed"] == 1
    assert out2["skipped"] == 0
    assert send_reorg_card.await_count == 2


def test_parse_reorg_proposals_no_proposals_key():
    assert parse_reorg_proposals('{"foo": "bar"}') == []


def test_parse_reorg_proposals_empty_text():
    assert parse_reorg_proposals("") == []


def test_parse_reorg_proposals_proposals_not_a_list():
    assert parse_reorg_proposals('{"proposals": "not-a-list"}') == []
    assert parse_reorg_proposals('{"proposals": {"kind": "sposta"}}') == []


# --- prep ------------------------------------------------------------------


def _prep_watcher():
    return ButlerWatcher(
        key="prep-owner",
        kind="prep",
        cadence="cron:0 */3 * * *",
        horizon_hours=6,
        enabled=True,
        recipient_profile="owner",
        node_key="mycorise",
    )


def _resolve_specialist(category):
    if category == "cucina":
        return ("personalChef", "chef")
    return ("scheduler", "generale")


def _st(st_id=1, category="cucina", state="pending", start_at="2026-07-09T20:00:00+02:00"):
    return {
        "id": st_id,
        "task_id": 100 + st_id,
        "name": "Cena zucchine",
        "category": category,
        "start_at": start_at,
        "end_at": "2026-07-09T20:45:00+02:00",
        "duration_min": 45,
        "notification_state": state,
        "is_locked": False,
    }


def _prep_kwargs(**overrides):
    base = dict(
        run_claude=AsyncMock(return_value=("<artifact>", "sid")),
        hw_client=MagicMock(),
        dedup=Dedup(),
        resolve_workspace=lambda k: "X",
        resolve_specialist=_resolve_specialist,
        store_artifact=AsyncMock(),
        telegram_chat_id=542,
        now=1_000_000.0,
        policy=ButlerPolicy(),
    )
    base.update(overrides)
    return base


@pytest.mark.asyncio
async def test_prep_watcher_non_dict_st_is_isolated_failure():
    hw = MagicMock()
    hw.list_scheduled_tasks = AsyncMock(return_value=["not-a-dict", _st(st_id=2)])
    kw = _prep_kwargs(hw_client=hw)
    out = await run_prep_watcher(_prep_watcher(), **kw)
    assert out["failed"] == 1
    assert out["prepared"] == 1
    kw["store_artifact"].assert_awaited_once_with(2, "<artifact>")


@pytest.mark.asyncio
async def test_prep_watcher_empty_string_artifact_is_not_stored():
    hw = MagicMock()
    hw.list_scheduled_tasks = AsyncMock(return_value=[_st()])
    kw = _prep_kwargs(hw_client=hw, run_claude=AsyncMock(return_value=("", "sid")))
    out = await run_prep_watcher(_prep_watcher(), **kw)
    assert out["prepared"] == 0
    assert out["failed"] == 0
    kw["store_artifact"].assert_not_awaited()


@pytest.mark.asyncio
async def test_prep_watcher_non_pending_state_is_skipped_silently():
    hw = MagicMock()
    hw.list_scheduled_tasks = AsyncMock(return_value=[_st(state="pre_sent")])
    kw = _prep_kwargs(hw_client=hw)
    out = await run_prep_watcher(_prep_watcher(), **kw)
    assert out == {"prepared": 0, "skipped": 0, "failed": 0, "actions_proposed": 0}
    kw["run_claude"].assert_not_awaited()
    kw["store_artifact"].assert_not_awaited()


@pytest.mark.asyncio
async def test_prep_watcher_calls_list_scheduled_tasks_with_now_to_horizon_window():
    hw = MagicMock()
    hw.list_scheduled_tasks = AsyncMock(return_value=[])
    now = 1_000_000.0
    watcher = _prep_watcher()
    kw = _prep_kwargs(hw_client=hw, now=now)
    await run_prep_watcher(watcher, **kw)

    hw.list_scheduled_tasks.assert_awaited_once()
    _, call_kwargs = hw.list_scheduled_tasks.await_args
    expected_frm = datetime.fromtimestamp(now, tz=timezone.utc)
    expected_to = expected_frm + timedelta(hours=watcher.horizon_hours)
    assert call_kwargs["frm"] == expected_frm.isoformat()
    assert call_kwargs["to"] == expected_to.isoformat()
    assert call_kwargs["telegram_chat_id"] == 542


# --- config/policy -----------------------------------------------------------


def test_load_butler_watchers_watchers_non_list_value_no_crash():
    assert load_butler_watchers({"watchers": "oops-not-a-list"}) == []
    assert load_butler_watchers({"watchers": {"key": "x"}}) == []


def test_load_butler_watchers_empty_dict_config():
    assert load_butler_watchers({}) == []


def test_load_butler_policy_watchers_none_no_crash():
    policy = load_butler_policy({"watchers": None})
    assert policy.reorg_apply_mode == "propose"


def test_load_butler_policy_policy_section_non_dict_no_crash():
    assert load_butler_policy({"policy": ["oops"]}).reorg_apply_mode == "propose"
    assert load_butler_policy({"policy": "oops"}).prep_output_mode == "readonly_artifact"


def test_load_butler_policy_per_agent_non_dict_no_crash():
    policy = load_butler_policy({"policy": {"per_agent": "oops"}})
    assert policy.per_agent == {}


def test_policy_resolve_partial_per_agent_override_falls_back_per_field():
    policy = ButlerPolicy(
        reorg_apply_mode="propose",
        prep_output_mode="readonly_artifact",
        trigger_mode="autonomous",
        per_agent={"mycorise": {"reorg_apply_mode": "auto_apply_trusted"}},
    )
    # only reorg_apply_mode is overridden for "mycorise" -> the other fields
    # fall back to the global default.
    assert policy.resolve("reorg_apply_mode", "mycorise") == "auto_apply_trusted"
    assert policy.resolve("prep_output_mode", "mycorise") == "readonly_artifact"
    assert policy.resolve("trigger_mode", "mycorise") == "autonomous"
