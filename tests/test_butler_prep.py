import pytest
from unittest.mock import AsyncMock, MagicMock

from butler_config import ButlerPolicy, ButlerWatcher
from butler_prep import build_prep_seed, run_prep_watcher
from taskgen import Dedup


def _watcher():
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


def _kwargs(**overrides):
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
async def test_empty_schedule_is_noop():
    hw = MagicMock()
    hw.list_scheduled_tasks = AsyncMock(return_value=[])
    kw = _kwargs(hw_client=hw)
    out = await run_prep_watcher(_watcher(), **kw)
    assert out == {"prepared": 0, "skipped": 0, "failed": 0, "actions_proposed": 0}
    kw["run_claude"].assert_not_awaited()
    kw["store_artifact"].assert_not_awaited()


@pytest.mark.asyncio
async def test_mapped_category_pending_prepares_artifact():
    hw = MagicMock()
    hw.list_scheduled_tasks = AsyncMock(return_value=[_st()])
    kw = _kwargs(hw_client=hw)
    out = await run_prep_watcher(_watcher(), **kw)
    assert out["prepared"] == 1
    kw["run_claude"].assert_awaited_once()
    _, call_kwargs = kw["run_claude"].call_args
    assert call_kwargs["workspace"] == "X"
    assert call_kwargs["persona"] == "chef"
    assert call_kwargs["resume_id"] is None
    kw["store_artifact"].assert_awaited_once_with(1, "<artifact>")


@pytest.mark.asyncio
async def test_second_tick_is_deduped():
    hw = MagicMock()
    hw.list_scheduled_tasks = AsyncMock(return_value=[_st()])
    dedup = Dedup()
    kw = _kwargs(hw_client=hw, dedup=dedup)
    await run_prep_watcher(_watcher(), **kw)
    out2 = await run_prep_watcher(_watcher(), **kw)
    assert out2["skipped"] == 1
    assert out2["prepared"] == 0
    assert kw["run_claude"].await_count == 1


@pytest.mark.asyncio
async def test_unmapped_category_falls_back_and_is_skipped():
    hw = MagicMock()
    hw.list_scheduled_tasks = AsyncMock(return_value=[_st(category="giardinaggio")])
    kw = _kwargs(hw_client=hw)
    out = await run_prep_watcher(_watcher(), **kw)
    assert out["skipped"] == 1
    assert out["prepared"] == 0
    kw["run_claude"].assert_not_awaited()
    kw["store_artifact"].assert_not_awaited()


@pytest.mark.asyncio
async def test_non_pending_state_is_ignored():
    hw = MagicMock()
    hw.list_scheduled_tasks = AsyncMock(return_value=[_st(state="done")])
    kw = _kwargs(hw_client=hw)
    out = await run_prep_watcher(_watcher(), **kw)
    assert out == {"prepared": 0, "skipped": 0, "failed": 0, "actions_proposed": 0}
    kw["run_claude"].assert_not_awaited()


@pytest.mark.asyncio
async def test_side_effects_policy_degrades_to_readonly():
    hw = MagicMock()
    hw.list_scheduled_tasks = AsyncMock(return_value=[_st()])
    policy = ButlerPolicy(prep_output_mode="side_effects")
    kw = _kwargs(hw_client=hw, policy=policy)
    out = await run_prep_watcher(_watcher(), **kw)
    assert out["prepared"] == 1
    kw["store_artifact"].assert_awaited_once_with(1, "<artifact>")


@pytest.mark.asyncio
async def test_run_claude_failure_is_isolated():
    hw = MagicMock()
    hw.list_scheduled_tasks = AsyncMock(return_value=[_st(st_id=1), _st(st_id=2)])
    calls = {"n": 0}

    async def _run_claude(seed, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("boom")
        return ("<artifact>", "sid")

    kw = _kwargs(hw_client=hw, run_claude=AsyncMock(side_effect=_run_claude))
    out = await run_prep_watcher(_watcher(), **kw)
    assert out["failed"] == 1
    assert out["prepared"] == 1
    kw["store_artifact"].assert_awaited_once_with(2, "<artifact>")


@pytest.mark.asyncio
async def test_empty_artifact_is_not_stored():
    hw = MagicMock()
    hw.list_scheduled_tasks = AsyncMock(return_value=[_st()])
    kw = _kwargs(hw_client=hw, run_claude=AsyncMock(return_value=(None, "sid")))
    out = await run_prep_watcher(_watcher(), **kw)
    assert out["prepared"] == 0
    assert out["failed"] == 0
    kw["store_artifact"].assert_not_awaited()


def test_prep_seed_mentions_deliverable_and_fields():
    seed = build_prep_seed(_st())
    assert "Cena zucchine" in seed
    assert "cucina" in seed
    assert "2026-07-09T20:00:00+02:00" in seed


# --- side_effects mode (#20) --------------------------------------------------

from butler_config import ButlerTrust  # noqa: E402
from butler_prep import build_actions_seed, parse_prep_actions  # noqa: E402


def _policy_side_effects():
    return ButlerPolicy(prep_output_mode="side_effects")


def _trust(cap=5, kinds=None):
    return ButlerTrust(
        kinds=kinds if kinds is not None else {"email_draft": "trusted"},
        daily_cap=cap,
    )


def _hw_one(st=None):
    hw = MagicMock()
    hw.list_scheduled_tasks = AsyncMock(return_value=[st or _st()])
    return hw


def _actions_json(*actions):
    import json as _json

    return _json.dumps({"actions": list(actions)})


def _email_action(to="a@b.it", subject="Ordine", body="Ciao"):
    return {
        "kind": "email_draft",
        "to": to,
        "subject": subject,
        "body": body,
        "rationale": "serve prima del task",
    }


def _two_phase_run_claude(actions_text):
    """run_claude that returns the artifact on the 1st call, actions on the 2nd."""
    calls = {"n": 0}

    async def _run(seed, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return ("<artefatto>", "sid1")
        return (actions_text, "sid2")

    return _run, calls


# --- parse_prep_actions -------------------------------------------------------


def test_parse_prep_actions_valid_json():
    actions = parse_prep_actions(_actions_json(_email_action()))
    assert len(actions) == 1
    assert actions[0]["kind"] == "email_draft"


def test_parse_prep_actions_dirty_text_around_json():
    text = "Certo, ecco:\n```json\n" + _actions_json(_email_action()) + "\n```\nfine."
    assert len(parse_prep_actions(text)) == 1


def test_parse_prep_actions_no_actions_key():
    assert parse_prep_actions('{"proposals": [{"kind": "sposta"}]}') == []


def test_parse_prep_actions_never_raises():
    for text in ("", None, "nessun json", "{", "{{{", '{"actions": "nope"}', "[]"):
        assert parse_prep_actions(text) == []


def test_parse_prep_actions_drops_non_dict_entries():
    actions = parse_prep_actions('{"actions": ["x", 3, {"kind": "email_draft"}]}')
    assert actions == [{"kind": "email_draft"}]


def test_build_actions_seed_mentions_task_and_artifact():
    seed = build_actions_seed(_st(), "LISTA SPESA")
    assert "Cena zucchine" in seed
    assert "LISTA SPESA" in seed
    assert "email_draft" in seed


# --- run_prep_watcher: side_effects branch ------------------------------------


@pytest.mark.asyncio
async def test_side_effects_proposes_trusted_action():
    run, calls = _two_phase_run_claude(_actions_json(_email_action()))
    propose = AsyncMock()

    counters = await run_prep_watcher(
        _watcher(),
        **_kwargs(
            run_claude=run,
            hw_client=_hw_one(),
            policy=_policy_side_effects(),
            action_trust=_trust(),
            propose_action=propose,
        ),
    )

    assert calls["n"] == 2  # artifact seed + actions seed
    assert counters["actions_proposed"] == 1
    propose.assert_awaited_once()
    action, st, chat_id = propose.await_args.args
    assert action["to"] == "a@b.it"
    assert st["id"] == 1
    assert chat_id == 542


@pytest.mark.asyncio
async def test_side_effects_still_stores_the_readonly_artifact():
    run, _calls = _two_phase_run_claude(_actions_json(_email_action()))
    store = AsyncMock()

    counters = await run_prep_watcher(
        _watcher(),
        **_kwargs(
            run_claude=run,
            hw_client=_hw_one(),
            store_artifact=store,
            policy=_policy_side_effects(),
            action_trust=_trust(),
            propose_action=AsyncMock(),
        ),
    )

    assert counters["prepared"] == 1
    store.assert_awaited_once_with(1, "<artefatto>")


@pytest.mark.asyncio
async def test_side_effects_discards_untrusted_kind():
    run, _calls = _two_phase_run_claude(_actions_json(_email_action()))
    propose = AsyncMock()

    counters = await run_prep_watcher(
        _watcher(),
        **_kwargs(
            run_claude=run,
            hw_client=_hw_one(),
            policy=_policy_side_effects(),
            action_trust=_trust(kinds={}),  # nothing trusted
            propose_action=propose,
        ),
    )

    propose.assert_not_awaited()
    assert counters["actions_proposed"] == 0
    assert counters["prepared"] == 1


@pytest.mark.asyncio
async def test_side_effects_discards_unknown_kind():
    run, _calls = _two_phase_run_claude(
        _actions_json({"kind": "delete_everything", "rationale": "trust me"})
    )
    propose = AsyncMock()

    counters = await run_prep_watcher(
        _watcher(),
        **_kwargs(
            run_claude=run,
            hw_client=_hw_one(),
            policy=_policy_side_effects(),
            action_trust=_trust(kinds={"delete_everything": "trusted"}),
            propose_action=propose,
        ),
    )

    propose.assert_not_awaited()
    assert counters["actions_proposed"] == 0


@pytest.mark.asyncio
async def test_side_effects_respects_daily_cap():
    run, _calls = _two_phase_run_claude(
        _actions_json(_email_action("a@x.it"), _email_action("b@x.it"), _email_action("c@x.it"))
    )
    propose = AsyncMock()

    counters = await run_prep_watcher(
        _watcher(),
        **_kwargs(
            run_claude=run,
            hw_client=_hw_one(),
            policy=_policy_side_effects(),
            action_trust=_trust(cap=2),
            propose_action=propose,
        ),
    )

    assert propose.await_count == 2
    assert counters["actions_proposed"] == 2


@pytest.mark.asyncio
async def test_side_effects_isolates_propose_action_error():
    run, _calls = _two_phase_run_claude(
        _actions_json(_email_action("a@x.it"), _email_action("b@x.it"))
    )
    propose = AsyncMock(side_effect=[RuntimeError("boom"), None])

    counters = await run_prep_watcher(
        _watcher(),
        **_kwargs(
            run_claude=run,
            hw_client=_hw_one(),
            policy=_policy_side_effects(),
            action_trust=_trust(),
            propose_action=propose,
        ),
    )

    assert counters["failed"] == 1
    assert counters["actions_proposed"] == 1
    assert counters["prepared"] == 1  # the artifact still landed


@pytest.mark.asyncio
async def test_side_effects_without_propose_action_degrades_to_readonly():
    run, calls = _two_phase_run_claude(_actions_json(_email_action()))

    counters = await run_prep_watcher(
        _watcher(),
        **_kwargs(
            run_claude=run,
            hw_client=_hw_one(),
            policy=_policy_side_effects(),
            action_trust=_trust(),
            propose_action=None,
        ),
    )

    assert calls["n"] == 1  # no second seed: nothing was even asked for
    assert counters["prepared"] == 1
    assert counters["actions_proposed"] == 0


@pytest.mark.asyncio
async def test_side_effects_without_action_trust_degrades_to_readonly():
    run, calls = _two_phase_run_claude(_actions_json(_email_action()))
    propose = AsyncMock()

    counters = await run_prep_watcher(
        _watcher(),
        **_kwargs(
            run_claude=run,
            hw_client=_hw_one(),
            policy=_policy_side_effects(),
            action_trust=None,
            propose_action=propose,
        ),
    )

    assert calls["n"] == 1
    propose.assert_not_awaited()
    assert counters["prepared"] == 1


@pytest.mark.asyncio
async def test_readonly_mode_never_asks_for_actions():
    run, calls = _two_phase_run_claude(_actions_json(_email_action()))
    propose = AsyncMock()

    counters = await run_prep_watcher(
        _watcher(),
        **_kwargs(
            run_claude=run,
            hw_client=_hw_one(),
            policy=ButlerPolicy(),  # readonly_artifact default
            action_trust=_trust(),
            propose_action=propose,
        ),
    )

    assert calls["n"] == 1
    propose.assert_not_awaited()
    assert counters["prepared"] == 1
