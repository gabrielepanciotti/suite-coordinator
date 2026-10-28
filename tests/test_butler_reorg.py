import pytest
from unittest.mock import AsyncMock

from butler_reorg import build_reorg_seed, parse_reorg_proposals, run_reorg_watcher
from butler_config import ButlerWatcher, ButlerPolicy, ButlerTrust
from taskgen import Dedup


def _watcher(**overrides):
    kw = dict(
        key="reorg-owner", kind="reorg", cadence="cron:0 8 * * *",
        horizon_hours=48, enabled=True, recipient_profile="owner",
        node_key="scheduler",
    )
    kw.update(overrides)
    return ButlerWatcher(**kw)


def _proposal_text():
    return (
        '{"proposals":[{"kind":"sposta","st_ids":[1,2],'
        '"new_start_iso":"2026-07-10T19:00:00+02:00","rationale":"vicini"}]}'
    )


# --- parse_reorg_proposals ---------------------------------------------------

def test_parse_reorg_proposals_extracts_block():
    out = parse_reorg_proposals(_proposal_text())
    assert len(out) == 1
    assert out[0]["kind"] == "sposta"
    assert out[0]["st_ids"] == [1, 2]


def test_parse_reorg_proposals_garbage_returns_empty():
    assert parse_reorg_proposals("not json at all") == []
    assert parse_reorg_proposals("") == []
    assert parse_reorg_proposals(None) == []


def test_build_reorg_seed_mentions_json_and_schedule():
    seed = build_reorg_seed([{"id": 1, "name": "Palestra", "category": "sport", "start_at": "..."}])
    assert "proposals" in seed
    assert "Palestra" in seed


# --- run_reorg_watcher --------------------------------------------------------

def _kw(**overrides):
    base = dict(
        run_claude=AsyncMock(return_value=(_proposal_text(), "sid")),
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


@pytest.mark.asyncio
async def test_empty_schedule_is_noop():
    hw = AsyncMock()
    hw.list_scheduled_tasks = AsyncMock(return_value=[])
    run_claude = AsyncMock()
    kw = _kw(hw_client=hw, run_claude=run_claude)
    out = await run_reorg_watcher(_watcher(), **kw)
    assert out == {"proposed": 0, "applied": 0, "skipped": 0, "failed": 0}
    run_claude.assert_not_awaited()


@pytest.mark.asyncio
async def test_valid_proposal_sends_card_and_dedups_second_tick():
    hw = AsyncMock()
    hw.list_scheduled_tasks = AsyncMock(return_value=[{"id": 1, "name": "A"}, {"id": 2, "name": "B"}])
    dedup = Dedup()
    send_reorg_card = AsyncMock()
    kw = _kw(hw_client=hw, dedup=dedup, send_reorg_card=send_reorg_card)

    out = await run_reorg_watcher(_watcher(), **kw)
    assert out["proposed"] == 1
    assert out["failed"] == 0
    send_reorg_card.assert_awaited_once()
    proposal, chat_id = send_reorg_card.await_args.args
    assert proposal["telegram_chat_id"] == 542
    assert proposal["kind"] == "sposta"
    assert proposal["st_ids"] == [1, 2]
    assert chat_id == 542

    out2 = await run_reorg_watcher(_watcher(), **kw)
    assert out2["skipped"] == 1
    assert send_reorg_card.await_count == 1


@pytest.mark.asyncio
async def test_list_scheduled_tasks_raises_no_crash():
    hw = AsyncMock()
    hw.list_scheduled_tasks = AsyncMock(side_effect=RuntimeError("boom"))
    run_claude = AsyncMock()
    kw = _kw(hw_client=hw, run_claude=run_claude)
    out = await run_reorg_watcher(_watcher(), **kw)
    assert out == {"proposed": 0, "applied": 0, "skipped": 0, "failed": 0}
    run_claude.assert_not_awaited()


@pytest.mark.asyncio
async def test_auto_apply_trusted_without_applier_degrades_to_propose():
    """No ``apply_reorg``/``trust`` injected -> the mode cannot act, so it proposes."""
    hw = AsyncMock()
    hw.list_scheduled_tasks = AsyncMock(return_value=[{"id": 1, "name": "A"}])
    send_reorg_card = AsyncMock()
    policy = ButlerPolicy(reorg_apply_mode="auto_apply_trusted")
    kw = _kw(hw_client=hw, send_reorg_card=send_reorg_card, policy=policy)

    out = await run_reorg_watcher(_watcher(), **kw)
    assert out["proposed"] == 1
    assert out["applied"] == 0
    send_reorg_card.assert_awaited_once()


@pytest.mark.asyncio
async def test_send_reorg_card_failure_is_isolated():
    hw = AsyncMock()
    hw.list_scheduled_tasks = AsyncMock(return_value=[{"id": 1, "name": "A"}])
    send_reorg_card = AsyncMock(side_effect=RuntimeError("telegram down"))
    kw = _kw(hw_client=hw, send_reorg_card=send_reorg_card)

    out = await run_reorg_watcher(_watcher(), **kw)
    assert out["failed"] == 1
    assert out["proposed"] == 0


# --- auto_apply_trusted (#19) --------------------------------------------------


def _schedule():
    return [
        {"id": 1, "name": "A", "start_at": "2026-07-10T09:00:00+02:00"},
        {"id": 2, "name": "B", "start_at": "2026-07-10T10:00:00+02:00"},
    ]


def _auto_kw(*, trust=None, apply_reorg=None, **overrides):
    hw = AsyncMock()
    hw.list_scheduled_tasks = AsyncMock(return_value=_schedule())
    base = _kw(
        hw_client=hw,
        policy=ButlerPolicy(reorg_apply_mode="auto_apply_trusted"),
        trust=trust if trust is not None else ButlerTrust(
            kinds={"sposta": "trusted"}, daily_cap=3
        ),
        apply_reorg=apply_reorg if apply_reorg is not None else AsyncMock(return_value=[]),
    )
    base.update(overrides)
    return base


@pytest.mark.asyncio
async def test_trusted_kind_is_applied_and_card_says_applied():
    apply_reorg = AsyncMock(return_value=[])
    send_reorg_card = AsyncMock()
    kw = _auto_kw(apply_reorg=apply_reorg, send_reorg_card=send_reorg_card)

    out = await run_reorg_watcher(_watcher(), **kw)

    assert out["applied"] == 1
    assert out["proposed"] == 0
    apply_reorg.assert_awaited_once_with([1, 2], "2026-07-10T19:00:00+02:00", 542)
    proposal, chat_id = send_reorg_card.await_args.args
    assert proposal["applied"] is True
    assert "failed_ids" not in proposal
    assert chat_id == 542


@pytest.mark.asyncio
async def test_untrusted_kind_stays_a_proposal():
    apply_reorg = AsyncMock(return_value=[])
    send_reorg_card = AsyncMock()
    kw = _auto_kw(
        trust=ButlerTrust(kinds={"accorpa": "trusted"}, daily_cap=3),
        apply_reorg=apply_reorg,
        send_reorg_card=send_reorg_card,
    )

    out = await run_reorg_watcher(_watcher(), **kw)

    assert out["proposed"] == 1 and out["applied"] == 0
    apply_reorg.assert_not_awaited()
    proposal, _ = send_reorg_card.await_args.args
    assert "applied" not in proposal


@pytest.mark.asyncio
async def test_daily_cap_zero_forces_propose():
    apply_reorg = AsyncMock(return_value=[])
    kw = _auto_kw(
        trust=ButlerTrust(kinds={"sposta": "trusted"}, daily_cap=0),
        apply_reorg=apply_reorg,
    )

    out = await run_reorg_watcher(_watcher(), **kw)

    assert out["proposed"] == 1 and out["applied"] == 0
    apply_reorg.assert_not_awaited()


@pytest.mark.asyncio
async def test_daily_cap_reached_mid_tick_forces_propose():
    """cap=1 with two proposals: the first is applied, the second is proposed."""
    text = (
        '{"proposals":[{"kind":"sposta","st_ids":[1],'
        '"new_start_iso":"2026-07-10T19:00:00+02:00","rationale":"a"},'
        '{"kind":"sposta","st_ids":[2],'
        '"new_start_iso":"2026-07-10T20:00:00+02:00","rationale":"b"}]}'
    )
    apply_reorg = AsyncMock(return_value=[])
    kw = _auto_kw(
        trust=ButlerTrust(kinds={"sposta": "trusted"}, daily_cap=1),
        apply_reorg=apply_reorg,
        run_claude=AsyncMock(return_value=(text, "sid")),
    )

    out = await run_reorg_watcher(_watcher(), **kw)

    assert out["applied"] == 1 and out["proposed"] == 1
    assert apply_reorg.await_count == 1


@pytest.mark.asyncio
async def test_total_apply_failure_counts_failed_and_skips_dedup():
    apply_reorg = AsyncMock(return_value=[1, 2])
    send_reorg_card = AsyncMock()
    dedup = Dedup()
    kw = _auto_kw(apply_reorg=apply_reorg, send_reorg_card=send_reorg_card, dedup=dedup)

    out = await run_reorg_watcher(_watcher(), **kw)
    assert out["failed"] == 1 and out["applied"] == 0
    send_reorg_card.assert_not_awaited()

    # no dedup record -> the next tick retries the very same proposal
    out2 = await run_reorg_watcher(_watcher(), **kw)
    assert out2["failed"] == 1 and out2["skipped"] == 0
    assert apply_reorg.await_count == 2


@pytest.mark.asyncio
async def test_partial_apply_failure_counts_applied_with_failed_ids():
    apply_reorg = AsyncMock(return_value=[2])
    send_reorg_card = AsyncMock()
    dedup = Dedup()
    kw = _auto_kw(apply_reorg=apply_reorg, send_reorg_card=send_reorg_card, dedup=dedup)

    out = await run_reorg_watcher(_watcher(), **kw)
    assert out["applied"] == 1 and out["failed"] == 0
    proposal, _ = send_reorg_card.await_args.args
    assert proposal["applied"] is True
    assert proposal["failed_ids"] == [2]

    out2 = await run_reorg_watcher(_watcher(), **kw)
    assert out2["skipped"] == 1


@pytest.mark.asyncio
async def test_prev_starts_snapshot_comes_from_the_schedule():
    send_reorg_card = AsyncMock()
    kw = _auto_kw(send_reorg_card=send_reorg_card)

    await run_reorg_watcher(_watcher(), **kw)

    proposal, _ = send_reorg_card.await_args.args
    assert proposal["prev_starts"] == {
        "1": "2026-07-10T09:00:00+02:00",
        "2": "2026-07-10T10:00:00+02:00",
    }


@pytest.mark.asyncio
async def test_unknown_mode_degrades_to_propose():
    hw = AsyncMock()
    hw.list_scheduled_tasks = AsyncMock(return_value=_schedule())
    apply_reorg = AsyncMock(return_value=[])
    kw = _kw(
        hw_client=hw,
        policy=ButlerPolicy(reorg_apply_mode="teletrasporto"),
        trust=ButlerTrust(kinds={"sposta": "trusted"}, daily_cap=5),
        apply_reorg=apply_reorg,
    )

    out = await run_reorg_watcher(_watcher(), **kw)

    assert out["proposed"] == 1 and out["applied"] == 0
    apply_reorg.assert_not_awaited()
