# tests/test_turn_serial_and_project_model.py
"""#63: one turn at a time per card, and a caption added by editing a photo is a
follow-up on the same photo, not a second ingest.
#65: a project can declare its default model/effort in coordinator.toml."""
import asyncio
import types

import bot
import manifest
import model_prefs
import projects
from tests.test_album_debounce import _photo_update


# --- #63: serial turns per (chat, topic) -------------------------------------

def _wire_turn(monkeypatch, tmp_path):
    proj = projects.Project("personalChef", "P", "P", "", str(tmp_path))
    agent = types.SimpleNamespace(name="generale", persona="")
    sessions = {}
    monkeypatch.setattr(bot, "_get_session", lambda c, t: sessions.get((c, t)))
    monkeypatch.setattr(bot, "_set_session", lambda c, t, sid: sessions.__setitem__((c, t), sid))
    monkeypatch.setattr(bot, "_get_prefs", lambda c, t: model_prefs.Prefs())
    monkeypatch.setattr(bot, "_project_default_prefs", lambda p: model_prefs.Prefs())
    monkeypatch.setattr(bot, "_topic_host_cwd", lambda p: None)
    monkeypatch.setattr(bot, "_log_turn", lambda **kw: None)
    monkeypatch.setattr(bot.indicazioni, "pubblica", lambda *a, **k: None)
    monkeypatch.setattr(bot.llm_usage, "record", lambda *a, **k: None)
    monkeypatch.setattr(bot, "CLAUDE_LIVE_SESSIONS", False)

    log = []
    counter = {"n": 0}

    async def fake_run(prompt, resume_id=None, **kw):
        log.append(("start", prompt, resume_id))
        await asyncio.sleep(0.05)
        counter["n"] += 1
        log.append(("end", prompt))
        return f"ok {prompt}", f"sid-{counter['n']}"

    monkeypatch.setattr(bot, "run_claude", fake_run)
    return proj, agent, log


def test_two_turns_on_the_same_card_run_one_after_the_other(monkeypatch, tmp_path):
    proj, agent, log = _wire_turn(monkeypatch, tmp_path)

    async def scenario():
        await asyncio.gather(
            bot._claude_turn(-100, 0, proj, agent, "foto", None),
            bot._claude_turn(-100, 0, proj, agent, "/scontrino", None),
        )

    asyncio.run(scenario())
    kinds = [e[0] for e in log]
    assert kinds == ["start", "end", "start", "end"], log
    # The second turn resumes the session the first one produced.
    second_start = log[2]
    assert second_start[2] == "sid-1"


def test_different_cards_still_run_in_parallel(monkeypatch, tmp_path):
    proj, agent, log = _wire_turn(monkeypatch, tmp_path)

    async def scenario():
        await asyncio.gather(
            bot._claude_turn(-100, 0, proj, agent, "a", None),
            bot._claude_turn(-200, 0, proj, agent, "b", None),
        )

    asyncio.run(scenario())
    assert [e[0] for e in log][:2] == ["start", "start"], log


# --- #63: caption added by editing the photo ---------------------------------

def _wire_photo(monkeypatch, tmp_path):
    (tmp_path / "node.config").write_text("{}", encoding="utf-8")
    proj = projects.Project("personalChef", "P", "P", "", str(tmp_path))
    agent = types.SimpleNamespace(name="a", persona="")
    monkeypatch.setattr(bot, "_profile", lambda u: types.SimpleNamespace(admin=True))
    monkeypatch.setattr(bot, "_is_stale", lambda m: False)
    monkeypatch.setattr(bot, "_topic_binding", lambda c, t: (proj, agent))
    monkeypatch.setattr(bot, "can_access", lambda p, k: True)
    monkeypatch.setattr(bot, "_ocr_image", lambda p: "")
    downloads = []

    async def fake_dl(msg, chat_id, project, ag, tid):
        downloads.append(msg.message_id)
        return f"{project.workspace}/raw/telegram/{msg.message_id}.jpg"

    monkeypatch.setattr(bot, "_download_photo", fake_dl)
    calls = []

    async def fake_invoke(update, project, ag, prompt):
        calls.append(prompt)

    monkeypatch.setattr(bot, "_invoke_and_reply", fake_invoke)
    return downloads, calls


def test_caption_added_by_edit_is_a_followup_not_a_second_ingest(monkeypatch, tmp_path):
    downloads, calls = _wire_photo(monkeypatch, tmp_path)

    async def scenario():
        await bot.handle_photo(_photo_update(-100, 35), None)
        edited = _photo_update(-100, 35, caption="/scontrino")
        edited.edited_message = edited.effective_message
        await bot.handle_photo(edited, None)

    asyncio.run(scenario())
    assert downloads == [35]  # downloaded once
    assert len(calls) == 2
    assert calls[0].startswith("[CMD:/dispensa]")
    assert calls[1].startswith("/scontrino")
    assert "35.jpg" in calls[1] and "stessa foto" in calls[1]


def test_same_photo_delivered_twice_without_edit_runs_once(monkeypatch, tmp_path):
    downloads, calls = _wire_photo(monkeypatch, tmp_path)

    async def scenario():
        await bot.handle_photo(_photo_update(-100, 36), None)
        await bot.handle_photo(_photo_update(-100, 36), None)

    asyncio.run(scenario())
    assert downloads == [36]
    assert len(calls) == 1


# --- #65: project default model ----------------------------------------------

def test_over_default_takes_the_model_selection_as_a_unit():
    card = model_prefs.Prefs(choice_id="claude-sub")
    project = model_prefs.Prefs(model="opus", effort="high")
    merged = card.over_default(project)
    assert merged.choice_id == "claude-sub"
    assert merged.model is None  # no stray "opus" glued onto the card's pick
    assert merged.effort == "high"


def test_over_default_fills_an_empty_card():
    merged = model_prefs.Prefs().over_default(model_prefs.Prefs(choice_id="deepseek-pro"))
    assert merged.choice_id == "deepseek-pro"


def _project_with_toml(tmp_path, body: str):
    d = tmp_path / ".claude"
    d.mkdir(exist_ok=True)
    (d / "coordinator.toml").write_text(body, encoding="utf-8")
    manifest._CACHE.clear()
    return projects.Project("personalChef", "P", "P", "", str(tmp_path))


def test_manifest_catalog_id_becomes_the_project_default(monkeypatch, tmp_path):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "k")
    proj = _project_with_toml(tmp_path, '[telegram]\nmodel = "deepseek-pro"\neffort = "high"\n')
    prefs = bot._project_default_prefs(proj)
    assert prefs.choice_id == "deepseek-pro"
    assert prefs.effort == "high"
    backend, model, extra = bot._resolve_turn(proj.key, model_prefs.Prefs().over_default(prefs))
    assert (backend, model) == ("opencode", "deepseek/deepseek-v4-pro")
    assert extra == {"DEEPSEEK_API_KEY": "k"}


def test_manifest_plain_model_token(tmp_path):
    proj = _project_with_toml(tmp_path, '[telegram]\nmodel = "opus"\n')
    assert bot._project_default_prefs(proj).model == model_prefs.normalize_model("opus")


def test_catalog_id_without_its_key_is_dropped(monkeypatch, tmp_path):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setattr(bot, "PROJECT_API_KEY_OVERRIDES", {})
    proj = _project_with_toml(tmp_path, '[telegram]\nmodel = "deepseek-pro"\n')
    assert not bot._project_default_prefs(proj)


def test_no_manifest_no_default(tmp_path):
    proj = projects.Project("x", "X", "X", "", str(tmp_path / "nope"))
    assert not bot._project_default_prefs(proj)
