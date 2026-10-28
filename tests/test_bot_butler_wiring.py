"""Task 7 — bot.py butler wiring: card sending, callbacks, state round-trip.

Covers the parts that are testable without actually starting the Telegram
Application: ``_REORG_CACHE``/``_PREP_CACHE``/``_BUTLER_DEDUP`` persistence
via ``_save_state``/``_load_state``, token generation for reorg cards, and
the ``rg:``/``pr:`` callback handlers with a fake Update/CallbackQuery.
``import bot`` works fine under pytest (see tests/conftest.py, which sets
TELEGRAM_BOT_TOKEN/ALLOWED_CHAT_IDS before any test module imports bot.py),
so this suite imports bot.py directly rather than isolating helpers.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

import bot
import taskgen


@pytest.fixture(autouse=True)
def _reset_butler_state(monkeypatch, tmp_path):
    """Isolate each test: fresh caches/dedup, STATE_PATH pointed at a tmp dir
    so _save_state actually writes (it no-ops when the data dir is missing)."""
    bot._REORG_CACHE.clear()
    bot._PREP_CACHE.clear()
    # Who may press the card is #89's business (test_ruoli_89): here, the card.
    monkeypatch.setattr(bot, "SESSION_GATE", False)
    monkeypatch.setattr(bot, "_BUTLER_DEDUP", taskgen.Dedup())
    monkeypatch.setattr(bot, "STATE_PATH", str(tmp_path / "state.json"))
    yield
    bot._REORG_CACHE.clear()
    bot._PREP_CACHE.clear()


# --- state round-trip ---------------------------------------------------------


def test_reorg_cache_round_trips_through_save_load():
    bot._REORG_CACHE["tok1"] = {
        "kind": "sposta",
        "st_ids": [1, 2],
        "new_start_iso": "2026-07-10T19:00:00+02:00",
        "rationale": "vicini",
        "telegram_chat_id": 111,
    }
    bot._save_state()

    bot._REORG_CACHE.clear()
    bot._load_state()

    assert bot._REORG_CACHE["tok1"]["st_ids"] == [1, 2]
    assert bot._REORG_CACHE["tok1"]["telegram_chat_id"] == 111


def test_prep_cache_round_trips_through_save_load_int_keys():
    bot._PREP_CACHE[42] = "ricetta pronta"
    bot._save_state()

    bot._PREP_CACHE.clear()
    bot._load_state()

    assert bot._PREP_CACHE[42] == "ricetta pronta"
    assert isinstance(list(bot._PREP_CACHE.keys())[0], int)


def test_butler_dedup_round_trips_through_save_load():
    probe = taskgen.Suggestion(
        title="sposta",
        category="2026-07-10T19:00:00+02:00",
        when_iso="1,2",
        duration_min=0,
        description=None,
        why=None,
    )
    bot._BUTLER_DEDUP.record(probe, now=1000.0)
    bot._save_state()

    monkeypatch_dedup = taskgen.Dedup()
    bot._BUTLER_DEDUP = monkeypatch_dedup
    bot._load_state()

    assert bot._BUTLER_DEDUP.seen(probe, now=1000.0) is True


def test_state_json_has_butler_keys():
    bot._REORG_CACHE["tokX"] = {"kind": "sposta", "st_ids": [9], "new_start_iso": "x",
                                 "rationale": None, "telegram_chat_id": 1}
    bot._PREP_CACHE[7] = "text"
    bot._save_state()

    with open(bot.STATE_PATH, encoding="utf-8") as fh:
        data = json.load(fh)

    assert "reorg_cache" in data
    assert "prep_cache" in data
    assert "butler_dedup" in data
    assert data["reorg_cache"]["tokX"]["st_ids"] == [9]
    assert data["prep_cache"]["7"] == "text"


def test_load_state_retro_compat_missing_butler_keys(tmp_path, monkeypatch):
    """An old state.json (pre-butler) has none of the 3 new keys — load must
    not crash and must leave the caches empty."""
    state_path = tmp_path / "old_state.json"
    state_path.write_text(json.dumps({"topics": {}, "sessions": {}}), encoding="utf-8")
    monkeypatch.setattr(bot, "STATE_PATH", str(state_path))

    bot._load_state()

    assert bot._REORG_CACHE == {}
    assert bot._PREP_CACHE == {}


# --- token generation ----------------------------------------------------------


def test_reorg_token_generation_is_unique(monkeypatch):
    """secrets.token_urlsafe(6) collisions are astronomically rare, but the
    generator must still loop past an existing key -- simulate a collision by
    pre-seeding the cache with the first token secrets.token_urlsafe would
    return, and asserting the final stored token differs."""
    calls = {"n": 0}
    tokens = ["collide", "collide", "unique-one"]

    def fake_token_urlsafe(_n):
        i = calls["n"]
        calls["n"] += 1
        return tokens[min(i, len(tokens) - 1)]

    monkeypatch.setattr(bot.secrets, "token_urlsafe", fake_token_urlsafe)
    bot._REORG_CACHE["collide"] = {"kind": "sposta", "st_ids": [1], "new_start_iso": "x",
                                    "rationale": None, "telegram_chat_id": 1}
    bot._BOT = MagicMock()
    bot._BOT.send_message = AsyncMock()

    import asyncio

    asyncio.run(
        bot._butler_send_reorg_card(
            {"kind": "sposta", "st_ids": [2], "new_start_iso": "y",
             "rationale": None, "telegram_chat_id": 2},
            chat_id=2,
        )
    )

    assert "unique-one" in bot._REORG_CACHE
    assert bot._REORG_CACHE["unique-one"]["st_ids"] == [2]
    bot._BOT = None


# --- rg:/pr: callback handlers -------------------------------------------------


class _FakeQuery:
    def __init__(self, data):
        self.data = data
        self.message = MagicMock()
        self.message.reply_text = AsyncMock()
        self.answer = AsyncMock()
        self.edit_message_text = AsyncMock()


class _FakeUpdate:
    def __init__(self, data):
        self.callback_query = _FakeQuery(data)


def test_on_reorg_cb_no_pops_token_and_edits():
    bot._REORG_CACHE["tokA"] = {
        "kind": "sposta", "st_ids": [1], "new_start_iso": "x",
        "rationale": None, "telegram_chat_id": 1,
    }
    update = _FakeUpdate(bot.pack_cb("rg", "no", "tokA"))

    import asyncio

    asyncio.run(bot._on_reorg_cb(update, MagicMock()))

    assert "tokA" not in bot._REORG_CACHE
    update.callback_query.edit_message_text.assert_awaited_with("Ignorato")


def test_on_reorg_cb_expired_token():
    update = _FakeUpdate(bot.pack_cb("rg", "apply", "gone"))

    import asyncio

    asyncio.run(bot._on_reorg_cb(update, MagicMock()))

    update.callback_query.answer.assert_awaited()


def test_on_prep_cb_missing_artifact_alerts():
    update = _FakeUpdate(bot.pack_cb("pr", "show", "999"))

    import asyncio

    asyncio.run(bot._on_prep_cb(update, MagicMock()))

    update.callback_query.answer.assert_awaited_with(
        "Preparazione non disponibile", show_alert=True
    )


def test_on_prep_cb_shows_cached_artifact():
    bot._PREP_CACHE[5] = "ecco l'artefatto"
    update = _FakeUpdate(bot.pack_cb("pr", "show", "5"))

    import asyncio

    asyncio.run(bot._on_prep_cb(update, MagicMock()))

    update.callback_query.message.reply_text.assert_awaited()


# --- on-demand triggers (#21) -------------------------------------------------


class _FakeMessage:
    def __init__(self):
        self.reply_text = AsyncMock()


class _FakeCommandUpdate:
    def __init__(self, chat_id=777):
        self.effective_message = _FakeMessage()
        self.effective_chat = MagicMock()
        self.effective_chat.id = chat_id


def _fake_context(args=None):
    ctx = MagicMock()
    ctx.args = args or []
    return ctx


@pytest.fixture
def _butler_cmd_env(monkeypatch):
    """Allow the command, stub the Scheduler client, keep the config in hand."""
    import scheduler
    import scheduler_client as hw

    monkeypatch.setattr(bot, "_allowed", lambda update: True)
    monkeypatch.setattr(hw, "client_from_env", lambda: MagicMock())
    cfg = {
        "watchers": [
            {
                "key": "reorg-owner",
                "kind": "reorg",
                "cadence": "cron:0 8 * * *",
                "recipient_profile": "owner",
            },
            {
                "key": "prep-owner",
                "kind": "prep",
                "cadence": "cron:0 */3 * * *",
                "recipient_profile": "owner",
            },
        ],
        "policy": {},
    }
    holder = {"cfg": cfg}
    monkeypatch.setattr(scheduler, "_load_butler_config", lambda *a, **k: holder["cfg"])
    return holder


@pytest.mark.asyncio
async def test_riorganizza_runs_reorg_watcher_with_command_chat_id(
    monkeypatch, _butler_cmd_env
):
    import butler_reorg

    run = AsyncMock(return_value={"proposed": 2, "skipped": 1, "failed": 0})
    monkeypatch.setattr(butler_reorg, "run_reorg_watcher", run)

    update = _FakeCommandUpdate(chat_id=4242)
    await bot.cmd_riorganizza(update, _fake_context())

    run.assert_awaited_once()
    assert run.call_args.args[0].key == "reorg-owner"
    assert run.call_args.kwargs["telegram_chat_id"] == 4242
    reply = update.effective_message.reply_text.await_args.args[0]
    assert "2 proposte" in reply and "1 già viste" in reply and "0 errori" in reply


@pytest.mark.asyncio
async def test_prepara_runs_prep_watcher_with_command_chat_id(
    monkeypatch, _butler_cmd_env
):
    import butler_prep

    run = AsyncMock(return_value={"prepared": 3, "skipped": 0, "failed": 1})
    monkeypatch.setattr(butler_prep, "run_prep_watcher", run)

    update = _FakeCommandUpdate(chat_id=99)
    await bot.cmd_prepara(update, _fake_context())

    run.assert_awaited_once()
    assert run.call_args.args[0].key == "prep-owner"
    assert run.call_args.kwargs["telegram_chat_id"] == 99
    reply = update.effective_message.reply_text.await_args.args[0]
    assert "3 preparate" in reply and "1 errori" in reply


@pytest.mark.asyncio
async def test_riorganizza_reports_missing_watcher(monkeypatch, _butler_cmd_env):
    _butler_cmd_env["cfg"] = {"watchers": [], "policy": {}}

    update = _FakeCommandUpdate()
    await bot.cmd_riorganizza(update, _fake_context())

    reply = update.effective_message.reply_text.await_args.args[0]
    assert "reorg" in reply and "Nessun watcher" in reply


@pytest.mark.asyncio
async def test_prepara_reports_missing_watcher(monkeypatch, _butler_cmd_env):
    _butler_cmd_env["cfg"] = {"watchers": [], "policy": {}}

    update = _FakeCommandUpdate()
    await bot.cmd_prepara(update, _fake_context())

    reply = update.effective_message.reply_text.await_args.args[0]
    assert "prep" in reply and "Nessun watcher" in reply


@pytest.mark.asyncio
async def test_prepara_refuses_task_argument(monkeypatch, _butler_cmd_env):
    import butler_prep

    run = AsyncMock()
    monkeypatch.setattr(butler_prep, "run_prep_watcher", run)

    update = _FakeCommandUpdate()
    await bot.cmd_prepara(update, _fake_context(["cena"]))

    run.assert_not_awaited()
    reply = update.effective_message.reply_text.await_args.args[0]
    assert "non è supportato" in reply


@pytest.mark.asyncio
async def test_riorganizza_never_raises_out(monkeypatch, _butler_cmd_env):
    import butler_reorg

    monkeypatch.setattr(
        butler_reorg, "run_reorg_watcher", AsyncMock(side_effect=RuntimeError("boom"))
    )

    update = _FakeCommandUpdate()
    await bot.cmd_riorganizza(update, _fake_context())

    reply = update.effective_message.reply_text.await_args.args[0]
    assert "fallita" in reply


@pytest.mark.asyncio
async def test_manual_tick_denied_for_unallowed_user(monkeypatch, _butler_cmd_env):
    import butler_reorg

    run = AsyncMock()
    monkeypatch.setattr(butler_reorg, "run_reorg_watcher", run)
    monkeypatch.setattr(bot, "_allowed", lambda update: False)

    update = _FakeCommandUpdate()
    await bot.cmd_riorganizza(update, _fake_context())

    run.assert_not_awaited()
    update.effective_message.reply_text.assert_not_awaited()


# --- act-then-notify card + undo (#19) ----------------------------------------


def _sent_card(monkeypatch, proposal):
    """Send a reorg card with a stubbed _BOT; return (text, reply_markup)."""
    import asyncio

    send_message = AsyncMock()
    fake_bot = MagicMock()
    fake_bot.send_message = send_message
    monkeypatch.setattr(bot, "_BOT", fake_bot)
    asyncio.run(bot._butler_send_reorg_card(proposal, chat_id=7))
    kwargs = send_message.await_args.kwargs
    return send_message.await_args.args[1], kwargs["reply_markup"]


def _button_texts(markup):
    return [b.text for row in markup.inline_keyboard for b in row]


def test_applied_card_offers_only_undo(monkeypatch):
    text, markup = _sent_card(
        monkeypatch,
        {
            "kind": "accorpa", "st_ids": [3, 4],
            "new_start_iso": "2026-07-10T19:00:00+02:00",
            "rationale": "vicini", "telegram_chat_id": 7,
            "prev_starts": {"3": "a", "4": "b"}, "applied": True,
        },
    )
    labels = _button_texts(markup)
    assert len(labels) == 1
    assert "Annulla" in labels[0]
    assert not any("Applica" in t or "Ignora" in t for t in labels)
    assert "Riorganizzato" in text
    token = list(bot._REORG_CACHE)[0]
    assert markup.inline_keyboard[0][0].callback_data == bot.pack_cb("rg", "undo", token)


def test_applied_card_mentions_failed_ids(monkeypatch):
    text, _ = _sent_card(
        monkeypatch,
        {
            "kind": "sposta", "st_ids": [3, 4],
            "new_start_iso": "x", "rationale": None, "telegram_chat_id": 7,
            "prev_starts": {}, "applied": True, "failed_ids": [4],
        },
    )
    assert "4" in text.split("\n")[-1]


def test_proposal_card_still_offers_apply_and_ignore(monkeypatch):
    _text, markup = _sent_card(
        monkeypatch,
        {
            "kind": "sposta", "st_ids": [3],
            "new_start_iso": "x", "rationale": None, "telegram_chat_id": 7,
        },
    )
    labels = _button_texts(markup)
    assert any("Applica" in t for t in labels)
    assert any("Ignora" in t for t in labels)
    assert not any("Annulla" in t for t in labels)


def _stub_scheduler_client(monkeypatch):
    import scheduler_client as hw

    client = MagicMock()
    client.reschedule_task = AsyncMock()
    monkeypatch.setattr(hw, "client_from_env", lambda **kw: client)
    return client


def test_on_reorg_cb_undo_restores_original_starts(monkeypatch):
    import asyncio

    client = _stub_scheduler_client(monkeypatch)
    bot._REORG_CACHE["tokU"] = {
        "kind": "sposta", "st_ids": [3, 4],
        "new_start_iso": "2026-07-10T19:00:00+02:00",
        "rationale": None, "telegram_chat_id": 77, "applied": True,
        "prev_starts": {"3": "2026-07-10T09:00:00+02:00",
                        "4": "2026-07-10T10:00:00+02:00"},
    }
    update = _FakeUpdate(bot.pack_cb("rg", "undo", "tokU"))

    asyncio.run(bot._on_reorg_cb(update, MagicMock()))

    calls = {c.kwargs["st_id"]: c.kwargs for c in client.reschedule_task.await_args_list}
    assert calls[3]["start_at"] == "2026-07-10T09:00:00+02:00"
    assert calls[4]["start_at"] == "2026-07-10T10:00:00+02:00"
    # the OWNER's chat, never the tap's chat
    assert calls[3]["telegram_chat_id"] == 77
    assert "tokU" not in bot._REORG_CACHE
    update.callback_query.edit_message_text.assert_awaited_with("↩️ Annullato")


def test_on_reorg_cb_undo_without_prev_starts_does_not_explode(monkeypatch):
    import asyncio

    client = _stub_scheduler_client(monkeypatch)
    bot._REORG_CACHE["tokV"] = {
        "kind": "sposta", "st_ids": [3], "new_start_iso": "x",
        "rationale": None, "telegram_chat_id": 77, "applied": True,
    }
    update = _FakeUpdate(bot.pack_cb("rg", "undo", "tokV"))

    asyncio.run(bot._on_reorg_cb(update, MagicMock()))

    client.reschedule_task.assert_not_awaited()
    assert "tokV" not in bot._REORG_CACHE
    text = update.callback_query.edit_message_text.await_args.args[0]
    assert "annullare" in text


def test_on_reorg_cb_undo_partial_failure_is_reported(monkeypatch):
    import asyncio

    client = _stub_scheduler_client(monkeypatch)

    async def _reschedule(*, st_id, telegram_chat_id, start_at):
        if st_id == 4:
            raise RuntimeError("scheduler down")

    client.reschedule_task = AsyncMock(side_effect=_reschedule)
    bot._REORG_CACHE["tokW"] = {
        "kind": "sposta", "st_ids": [3, 4], "new_start_iso": "x",
        "rationale": None, "telegram_chat_id": 77, "applied": True,
        "prev_starts": {"3": "a", "4": "b"},
    }
    update = _FakeUpdate(bot.pack_cb("rg", "undo", "tokW"))

    asyncio.run(bot._on_reorg_cb(update, MagicMock()))

    text = update.callback_query.edit_message_text.await_args.args[0]
    assert "parzialmente" in text and "3" in text


def test_butler_apply_reorg_returns_failed_ids(monkeypatch):
    import asyncio

    client = _stub_scheduler_client(monkeypatch)

    async def _reschedule(*, st_id, telegram_chat_id, start_at):
        if st_id == 2:
            raise RuntimeError("nope")

    client.reschedule_task = AsyncMock(side_effect=_reschedule)

    failed = asyncio.run(bot._butler_apply_reorg([1, 2], "2026-07-10T19:00:00+02:00", 55))
    assert failed == [2]


def test_butler_deps_exposes_apply_reorg():
    assert bot._butler_deps()["apply_reorg"] is bot._butler_apply_reorg


# --- preparation actions with side effects (#20) -------------------------------


@pytest.fixture(autouse=True)
def _reset_action_state():
    bot._PREP_ACTION_CACHE.clear()
    bot._BUTLER_AUDIT.clear()
    yield
    bot._PREP_ACTION_CACHE.clear()
    bot._BUTLER_AUDIT.clear()


def _email_action():
    return {
        "kind": "email_draft",
        "to": "fornitore@esempio.it",
        "subject": "Ordine per la cena",
        "body": "Buongiorno, servirebbero 2 kg di zucchine.",
        "rationale": "il task inizia fra due ore",
    }


def _propose(monkeypatch, action=None, st=None, chat_id=7):
    """Send an action card with a stubbed _BOT; return (text, markup, token)."""
    import asyncio

    send_message = AsyncMock()
    fake_bot = MagicMock()
    fake_bot.send_message = send_message
    monkeypatch.setattr(bot, "_BOT", fake_bot)
    asyncio.run(
        bot._butler_propose_action(
            action or _email_action(),
            st if st is not None else {"id": 42, "name": "Cena zucchine"},
            chat_id,
        )
    )
    text = send_message.await_args.args[1]
    markup = send_message.await_args.kwargs["reply_markup"]
    token = next(iter(bot._PREP_ACTION_CACHE))
    return text, markup, token


def test_action_card_shows_what_would_be_done(monkeypatch):
    text, markup, token = _propose(monkeypatch)

    assert "fornitore@esempio.it" in text
    assert "Ordine per la cena" in text
    assert "zucchine" in text
    assert _button_texts(markup) == ["✅ Conferma", "❌ Scarta"]
    assert bot._PREP_ACTION_CACHE[token]["st_id"] == 42


def test_action_card_has_no_parse_mode(monkeypatch):
    """LLM-written free text must never be sent as markup."""
    import asyncio

    send_message = AsyncMock()
    fake_bot = MagicMock()
    fake_bot.send_message = send_message
    monkeypatch.setattr(bot, "_BOT", fake_bot)
    asyncio.run(bot._butler_propose_action(_email_action(), {"id": 1}, 7))

    assert "parse_mode" not in send_message.await_args.kwargs


def test_action_card_truncates_long_body(monkeypatch):
    action = _email_action()
    action["body"] = "x" * 2000
    text, _markup, _token = _propose(monkeypatch, action=action)

    assert "troncato" in text
    assert len(text) < 1200


def test_proposing_an_action_executes_nothing(monkeypatch):
    """AC centrale di #20: proporre non è eseguire."""
    executed = MagicMock()
    monkeypatch.setattr(bot, "_butler_execute_action", executed)

    _propose(monkeypatch)

    executed.assert_not_called()
    assert [r["outcome"] for r in bot._BUTLER_AUDIT] == ["proposed"]


def test_pa_no_discards_without_executing(monkeypatch):
    import asyncio

    _text, _markup, token = _propose(monkeypatch)
    executed = MagicMock()
    monkeypatch.setattr(bot, "_butler_execute_action", executed)

    update = _FakeUpdate(bot.pack_cb("pa", "no", token))
    asyncio.run(bot._on_prep_action_cb(update, MagicMock()))

    executed.assert_not_called()
    assert token not in bot._PREP_ACTION_CACHE
    update.callback_query.edit_message_text.assert_awaited_with("Scartata")
    assert bot._BUTLER_AUDIT[-1]["outcome"] == "discarded"


def test_pa_ok_executes_and_audits(monkeypatch):
    import asyncio

    _text, _markup, token = _propose(monkeypatch)
    monkeypatch.setattr(
        bot, "_butler_execute_action", MagicMock(return_value=(True, "bozza creata"))
    )

    update = _FakeUpdate(bot.pack_cb("pa", "ok", token))
    asyncio.run(bot._on_prep_action_cb(update, MagicMock()))

    assert token not in bot._PREP_ACTION_CACHE
    outcomes = [r["outcome"] for r in bot._BUTLER_AUDIT]
    assert outcomes == ["proposed", "confirmed", "executed"]
    assert bot._BUTLER_AUDIT[-1]["kind"] == "email_draft"
    assert bot._BUTLER_AUDIT[-1]["st_id"] == 42
    text = update.callback_query.edit_message_text.await_args.args[0]
    assert "bozza creata" in text


def test_pa_ok_reports_a_failed_execution(monkeypatch):
    import asyncio

    _text, _markup, token = _propose(monkeypatch)
    monkeypatch.setattr(
        bot,
        "_butler_execute_action",
        MagicMock(return_value=(False, "bozze non supportate dal mailer")),
    )

    update = _FakeUpdate(bot.pack_cb("pa", "ok", token))
    asyncio.run(bot._on_prep_action_cb(update, MagicMock()))

    assert bot._BUTLER_AUDIT[-1]["outcome"] == "failed"
    text = update.callback_query.edit_message_text.await_args.args[0]
    assert "bozze non supportate dal mailer" in text


def test_pa_expired_token_does_not_explode(monkeypatch):
    import asyncio

    executed = MagicMock()
    monkeypatch.setattr(bot, "_butler_execute_action", executed)

    update = _FakeUpdate(bot.pack_cb("pa", "ok", "gone"))
    asyncio.run(bot._on_prep_action_cb(update, MagicMock()))

    executed.assert_not_called()
    update.callback_query.answer.assert_awaited_with("Azione scaduta", show_alert=True)


def test_execute_action_refuses_unknown_kind():
    ok, detail = bot._butler_execute_action({"kind": "delete_everything"})
    assert ok is False
    assert "non supportata" in detail


# test_execute_action_email_draft_without_mailer_support e
# test_execute_action_uses_create_draft_when_available vivono solo sul branch
# `lifework`: importano graph_mailer, che e' work instance-specifico e non esiste
# su master (Coordinator personale). Vedi requirements.txt/Dockerfile.


def test_prep_action_cache_and_audit_round_trip_through_state(monkeypatch):
    _text, _markup, token = _propose(monkeypatch)
    bot._save_state()

    bot._PREP_ACTION_CACHE.clear()
    bot._BUTLER_AUDIT.clear()
    bot._load_state()

    assert bot._PREP_ACTION_CACHE[token]["to"] == "fornitore@esempio.it"
    assert bot._BUTLER_AUDIT[-1]["outcome"] == "proposed"


def test_audit_is_capped(monkeypatch):
    for i in range(bot._BUTLER_AUDIT_MAX + 25):
        bot._butler_audit("email_draft", i, "proposed")

    assert len(bot._BUTLER_AUDIT) == bot._BUTLER_AUDIT_MAX
    assert bot._BUTLER_AUDIT[-1]["st_id"] == bot._BUTLER_AUDIT_MAX + 24


def test_butler_deps_carries_propose_action():
    assert bot._butler_deps()["propose_action"] is bot._butler_propose_action


@pytest.mark.asyncio
async def test_prepara_manual_tick_injects_action_trust_and_propose_action(
    monkeypatch, _butler_cmd_env
):
    """The on-demand /prepara must inject exactly what the autonomous job does."""
    import butler_prep

    _butler_cmd_env["cfg"]["action_trust"] = {
        "kinds": {"email_draft": "trusted"},
        "daily_cap": 1,
    }
    run = AsyncMock(return_value={"prepared": 0, "skipped": 0, "failed": 0,
                                  "actions_proposed": 0})
    monkeypatch.setattr(butler_prep, "run_prep_watcher", run)

    await bot.cmd_prepara(_FakeCommandUpdate(chat_id=5), _fake_context())

    kwargs = run.call_args.kwargs
    assert kwargs["propose_action"] is bot._butler_propose_action
    assert kwargs["action_trust"].decision("email_draft") == "trusted"
    assert kwargs["action_trust"].daily_cap == 1
