from unittest.mock import AsyncMock, MagicMock

import pytest

import butler_prep
import butler_reorg
from scheduler import register_butler_jobs


def _cfg(*, watcher_enabled=True, extra_watchers=None, kind="reorg"):
    watchers = [
        {
            "key": "reorg-daily",
            "kind": kind,
            "node_key": "personalChef",
            "cadence": "cron:0 11 * * *",
            "enabled": watcher_enabled,
            "recipient_profile": "owner",
        }
    ]
    if extra_watchers:
        watchers.extend(extra_watchers)
    return {"watchers": watchers, "policy": {}}


def _deps(**overrides):
    base = dict(
        run_claude=AsyncMock(),
        hw_client=MagicMock(),
        resolve_workspace=lambda k: "G:/ChefToni",
        resolve_specialist=lambda cat: ("cheftoni", "chef"),
        chat_id_of=lambda profile: 542,
        dedup=MagicMock(),
        send_reorg_card=AsyncMock(),
        store_artifact=AsyncMock(),
    )
    base.update(overrides)
    return base


def test_register_butler_jobs_adds_one_job_per_enabled_watcher():
    sched = MagicMock()
    registered = register_butler_jobs(sched, config=_cfg(), **_deps())
    assert sched.add_job.call_count == 1
    assert len(registered) == 1
    assert registered[0].key == "reorg-daily"
    job_id = sched.add_job.call_args.kwargs["id"]
    assert job_id == "butler:reorg-daily"


def test_register_butler_jobs_skips_disabled_watchers():
    sched = MagicMock()
    registered = register_butler_jobs(
        sched, config=_cfg(watcher_enabled=False), **_deps()
    )
    assert sched.add_job.call_count == 0
    assert registered == []


def test_register_butler_jobs_skips_unrecognized_cadence():
    sched = MagicMock()
    extra = [
        {
            "key": "broken",
            "kind": "reorg",
            "node_key": "mycorise",
            "cadence": "weird:whatever",
            "enabled": True,
            "recipient_profile": "owner",
        }
    ]
    registered = register_butler_jobs(
        sched, config=_cfg(extra_watchers=extra), **_deps()
    )
    assert sched.add_job.call_count == 1
    assert [w.key for w in registered] == ["reorg-daily"]


@pytest.mark.asyncio
async def test_tick_routes_reorg_kind_to_run_reorg_watcher(monkeypatch):
    sched = MagicMock()
    mock_run_reorg = AsyncMock()
    monkeypatch.setattr(butler_reorg, "run_reorg_watcher", mock_run_reorg)

    register_butler_jobs(sched, config=_cfg(kind="reorg"), **_deps())

    tick = {
        call.kwargs["id"]: call.args[0] for call in sched.add_job.call_args_list
    }["butler:reorg-daily"]
    await tick()

    mock_run_reorg.assert_awaited_once()
    assert mock_run_reorg.call_args.kwargs["telegram_chat_id"] == 542


@pytest.mark.asyncio
async def test_tick_routes_prep_kind_to_run_prep_watcher(monkeypatch):
    sched = MagicMock()
    mock_run_prep = AsyncMock()
    monkeypatch.setattr(butler_prep, "run_prep_watcher", mock_run_prep)

    register_butler_jobs(sched, config=_cfg(kind="prep"), **_deps())

    tick = {
        call.kwargs["id"]: call.args[0] for call in sched.add_job.call_args_list
    }["butler:reorg-daily"]
    await tick()

    mock_run_prep.assert_awaited_once()
    assert mock_run_prep.call_args.kwargs["telegram_chat_id"] == 542


@pytest.mark.asyncio
async def test_tick_skips_when_chat_id_none(monkeypatch):
    sched = MagicMock()
    mock_run_reorg = AsyncMock()
    monkeypatch.setattr(butler_reorg, "run_reorg_watcher", mock_run_reorg)

    register_butler_jobs(
        sched, config=_cfg(kind="reorg"), **_deps(chat_id_of=lambda profile: None)
    )

    tick = {
        call.kwargs["id"]: call.args[0] for call in sched.add_job.call_args_list
    }["butler:reorg-daily"]
    await tick()

    mock_run_reorg.assert_not_awaited()


@pytest.mark.asyncio
async def test_tick_calls_save_state_in_finally(monkeypatch):
    sched = MagicMock()
    monkeypatch.setattr(butler_reorg, "run_reorg_watcher", AsyncMock())
    save_state = MagicMock()

    register_butler_jobs(
        sched, config=_cfg(kind="reorg"), save_state=save_state, **_deps()
    )

    tick = {
        call.kwargs["id"]: call.args[0] for call in sched.add_job.call_args_list
    }["butler:reorg-daily"]
    await tick()

    save_state.assert_called_once()


# --- trigger_mode gate (#21) --------------------------------------------------


def _cfg_with_policy(policy, *, node_key="personalChef"):
    return {
        "watchers": [
            {
                "key": "reorg-daily",
                "kind": "reorg",
                "node_key": node_key,
                "cadence": "cron:0 11 * * *",
                "enabled": True,
                "recipient_profile": "owner",
            }
        ],
        "policy": policy,
    }


def test_on_demand_trigger_mode_registers_no_job():
    sched = MagicMock()
    registered = register_butler_jobs(
        sched, config=_cfg_with_policy({"trigger_mode": "on_demand"}), **_deps()
    )
    assert sched.add_job.call_count == 0
    assert registered == []


def test_autonomous_trigger_mode_is_the_default_and_registers():
    sched = MagicMock()
    registered = register_butler_jobs(sched, config=_cfg_with_policy({}), **_deps())
    assert sched.add_job.call_count == 1
    assert [w.key for w in registered] == ["reorg-daily"]

    sched2 = MagicMock()
    registered2 = register_butler_jobs(
        sched2, config=_cfg_with_policy({"trigger_mode": "autonomous"}), **_deps()
    )
    assert sched2.add_job.call_count == 1
    assert [w.key for w in registered2] == ["reorg-daily"]


def test_per_agent_override_puts_only_one_node_on_demand():
    cfg = _cfg_with_policy(
        {"per_agent": {"personalChef": {"trigger_mode": "on_demand"}}}
    )
    cfg["watchers"].append(
        {
            "key": "prep-mycorise",
            "kind": "prep",
            "node_key": "mycorise",
            "cadence": "cron:0 */3 * * *",
            "enabled": True,
            "recipient_profile": "owner",
        }
    )
    sched = MagicMock()
    registered = register_butler_jobs(sched, config=cfg, **_deps())
    assert [w.key for w in registered] == ["prep-mycorise"]
    assert sched.add_job.call_count == 1
    assert sched.add_job.call_args.kwargs["id"] == "butler:prep-mycorise"


# --- trust / apply_reorg injection (#19) --------------------------------------


@pytest.mark.asyncio
async def test_tick_passes_trust_and_apply_reorg_to_run_reorg_watcher(monkeypatch):
    sched = MagicMock()
    mock_run_reorg = AsyncMock()
    monkeypatch.setattr(butler_reorg, "run_reorg_watcher", mock_run_reorg)
    apply_reorg = AsyncMock(return_value=[])
    cfg = _cfg(kind="reorg")
    cfg["trust"] = {"kinds": {"accorpa": "trusted"}, "daily_cap": 2}

    register_butler_jobs(sched, config=cfg, apply_reorg=apply_reorg, **_deps())

    tick = {
        call.kwargs["id"]: call.args[0] for call in sched.add_job.call_args_list
    }["butler:reorg-daily"]
    await tick()

    kwargs = mock_run_reorg.call_args.kwargs
    assert kwargs["apply_reorg"] is apply_reorg
    assert kwargs["trust"].decision("accorpa") == "trusted"
    assert kwargs["trust"].daily_cap == 2


@pytest.mark.asyncio
async def test_tick_without_apply_reorg_still_works(monkeypatch):
    """Retro-compat: a caller that doesn't inject apply_reorg gets None, not a TypeError."""
    sched = MagicMock()
    mock_run_reorg = AsyncMock()
    monkeypatch.setattr(butler_reorg, "run_reorg_watcher", mock_run_reorg)

    register_butler_jobs(sched, config=_cfg(kind="reorg"), **_deps())

    tick = {
        call.kwargs["id"]: call.args[0] for call in sched.add_job.call_args_list
    }["butler:reorg-daily"]
    await tick()

    kwargs = mock_run_reorg.call_args.kwargs
    assert kwargs["apply_reorg"] is None
    assert kwargs["trust"].decision("accorpa") == "propose"


# --- action_trust / propose_action injection (#20) ----------------------------


@pytest.mark.asyncio
async def test_tick_passes_action_trust_and_propose_action_to_prep_watcher(monkeypatch):
    sched = MagicMock()
    mock_run_prep = AsyncMock()
    monkeypatch.setattr(butler_prep, "run_prep_watcher", mock_run_prep)
    propose_action = AsyncMock()
    cfg = _cfg(kind="prep")
    cfg["action_trust"] = {"kinds": {"email_draft": "trusted"}, "daily_cap": 2}

    register_butler_jobs(
        sched, config=cfg, propose_action=propose_action, **_deps()
    )

    tick = {
        call.kwargs["id"]: call.args[0] for call in sched.add_job.call_args_list
    }["butler:reorg-daily"]
    await tick()

    kwargs = mock_run_prep.call_args.kwargs
    assert kwargs["propose_action"] is propose_action
    assert kwargs["action_trust"].decision("email_draft") == "trusted"
    assert kwargs["action_trust"].daily_cap == 2


@pytest.mark.asyncio
async def test_tick_without_propose_action_still_works(monkeypatch):
    """Retro-compat: a caller that doesn't inject propose_action gets None."""
    sched = MagicMock()
    mock_run_prep = AsyncMock()
    monkeypatch.setattr(butler_prep, "run_prep_watcher", mock_run_prep)

    register_butler_jobs(sched, config=_cfg(kind="prep"), **_deps())

    tick = {
        call.kwargs["id"]: call.args[0] for call in sched.add_job.call_args_list
    }["butler:reorg-daily"]
    await tick()

    kwargs = mock_run_prep.call_args.kwargs
    assert kwargs["propose_action"] is None
    assert kwargs["action_trust"].decision("email_draft") == "propose"
