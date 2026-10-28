from unittest.mock import AsyncMock, MagicMock

import pytest

import taskgen
from taskgen_config import load_trust_policy, load_watchers
from scheduler import register_taskgen_jobs


def _cfg(*, watcher_enabled=True, extra_watchers=None):
    watchers = [
        {
            "key": "cheftoni",
            "node_key": "personalChef",
            "cadence": "cron:0 11 * * *",
            "pre_gate": "temporal",
            "enabled": watcher_enabled,
            "recipient_profile": "owner",
        }
    ]
    if extra_watchers:
        watchers.extend(extra_watchers)
    return {
        "watchers": watchers,
        "trust": {"categories": {"cucina": "trusted"}, "daily_cap": 2},
    }


def test_load_watchers_seed_cheftoni():
    cfg = {
        "watchers": [
            {
                "key": "cheftoni",
                "node_key": "personalChef",
                "cadence": "cron:0 11 * * *",
                "pre_gate": "temporal",
                "enabled": True,
                "recipient_profile": "owner",
            }
        ],
        "trust": {"categories": {"cucina": "trusted"}, "daily_cap": 2},
    }
    ws = load_watchers(cfg)
    assert len(ws) == 1 and ws[0].node_key == "personalChef"
    p = load_trust_policy(cfg)
    assert p.decision("cucina") == "trusted"
    assert p.decision("sport") == "propose"
    assert p.default_category_id is None


def test_load_trust_policy_parses_default_category_id():
    cfg = {
        "trust": {
            "categories": {"cucina": "trusted"},
            "daily_cap": 2,
            "default_category_id": 1,
        },
    }
    p = load_trust_policy(cfg)
    assert p.default_category_id == 1


def test_register_taskgen_jobs_adds_one_job_per_enabled_watcher():
    sched = MagicMock()
    registered = register_taskgen_jobs(
        sched,
        run_claude=AsyncMock(),
        hw_client=MagicMock(),
        send_notice=AsyncMock(),
        resolve_workspace=lambda k: "G:/ChefToni",
        dedup=MagicMock(),
        chat_id_of=lambda profile: 542,
        config=_cfg(),
    )
    assert sched.add_job.call_count == 1
    assert len(registered) == 1
    assert registered[0].key == "cheftoni"


def test_register_taskgen_jobs_skips_disabled_watchers():
    sched = MagicMock()
    registered = register_taskgen_jobs(
        sched,
        run_claude=AsyncMock(),
        hw_client=MagicMock(),
        send_notice=AsyncMock(),
        resolve_workspace=lambda k: "G:/ChefToni",
        dedup=MagicMock(),
        chat_id_of=lambda profile: 542,
        config=_cfg(watcher_enabled=False),
    )
    assert sched.add_job.call_count == 0
    assert registered == []


def test_register_taskgen_jobs_skips_unrecognized_cadence():
    sched = MagicMock()
    extra = [
        {
            "key": "broken",
            "node_key": "mycorise",
            "cadence": "weird:whatever",
            "pre_gate": "temporal",
            "enabled": True,
            "recipient_profile": "owner",
        }
    ]
    registered = register_taskgen_jobs(
        sched,
        run_claude=AsyncMock(),
        hw_client=MagicMock(),
        send_notice=AsyncMock(),
        resolve_workspace=lambda k: "G:/ChefToni",
        dedup=MagicMock(),
        chat_id_of=lambda profile: 542,
        config=_cfg(extra_watchers=extra),
    )
    # only the well-formed cron watcher registers; the malformed cadence is skipped
    assert sched.add_job.call_count == 1
    assert [w.key for w in registered] == ["cheftoni"]


@pytest.mark.asyncio
async def test_register_taskgen_jobs_tick_skips_run_watcher_when_gate_closed(monkeypatch):
    sched = MagicMock()
    extra = [
        {
            "key": "mycorise-umidita",
            "node_key": "mycorise",
            "cadence": "cron:0 9 * * *",
            "pre_gate": "dati",
            "gate_cmd": "exit 1",
            "enabled": True,
            "recipient_profile": "owner",
        }
    ]
    mock_run_watcher = AsyncMock()
    monkeypatch.setattr(taskgen, "run_watcher", mock_run_watcher)

    register_taskgen_jobs(
        sched,
        run_claude=AsyncMock(),
        hw_client=MagicMock(),
        send_notice=AsyncMock(),
        resolve_workspace=lambda k: "G:/Mycorise",
        dedup=MagicMock(),
        chat_id_of=lambda profile: 542,
        config=_cfg(extra_watchers=extra),
    )

    assert sched.add_job.call_count == 2
    tick_calls = {
        call.kwargs["id"]: call.args[0] for call in sched.add_job.call_args_list
    }
    await tick_calls["taskgen:mycorise-umidita"]()

    mock_run_watcher.assert_not_awaited()
