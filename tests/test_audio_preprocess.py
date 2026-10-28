"""Lesson transcription runs OUTSIDE the agent turn.

A 2-hour lesson takes about an hour to transcribe on the minipc CPU: inside a
turn the claude_runner watchdog kills it and the group's turn lock blocks
everyone meanwhile. A project that declares ``[telegram] audio_preprocess``
gets the command run as a serialized background subprocess; only when the
transcript is ready is the agent invoked, with the transcript in the prompt.
"""
import asyncio
import sys
import time
import types
from pathlib import Path

import pytest
from test_lesson_audio import AA_CHAT, OTHER_CHAT, _proj, _update

import audio_preprocess
import bot
import manifest
import profiles
import projects

TEMPLATE = "sh scripts/pyrun scripts/transcribe.py {file} --out {out}"
FAILED_MSG = ("😕 Non sono riuscita a trascrivere la registrazione. "
              "the owner è stato avvisato: riprova a mandarla più tardi.")


# --- manifest -----------------------------------------------------------------

def _toml(tmp_path, body):
    claude = tmp_path / ".claude"
    claude.mkdir()
    (claude / "coordinator.toml").write_text(body, encoding="utf-8")
    return manifest.load(_proj(tmp_path.name, tmp_path)).telegram


def test_manifest_parses_audio_preprocess(tmp_path):
    tg = _toml(tmp_path, '[telegram]\ngroup_chat_id = -1001\naudio_command = "/lezione"\n'
                         f'audio_preprocess = "{TEMPLATE}"\n')
    assert tg.audio_preprocess == TEMPLATE


@pytest.mark.parametrize("raw", ['"python run.py {file}"', '"python run.py --out {out}"', "42"])
def test_manifest_ignores_an_invalid_audio_preprocess(tmp_path, raw, caplog):
    tg = _toml(tmp_path, f"[telegram]\ngroup_chat_id = -1001\naudio_preprocess = {raw}\n")
    assert tg.audio_preprocess is None
    assert any(r.levelname == "WARNING" for r in caplog.records)


def test_manifest_without_audio_preprocess_is_none(tmp_path):
    assert _toml(tmp_path, "[telegram]\ngroup_chat_id = -1001\n").audio_preprocess is None


# --- build_command ------------------------------------------------------------

def test_build_command_substitutes_per_token():
    cmd = audio_preprocess.build_command(
        "sh run {file} --out={out}", Path("/w/in.ogg"), Path("/w/out.txt"))
    assert cmd == ["sh", "run", str(Path("/w/in.ogg")), f"--out={Path('/w/out.txt')}"]


def test_build_command_keeps_a_hostile_filename_as_one_argument():
    nasty = Path("/w/le zione \"x\"; rm -rf ~ '{out}'.ogg")
    out = Path("/w/out file.txt")
    cmd = audio_preprocess.build_command(TEMPLATE, nasty, out)
    assert cmd == ["sh", "scripts/pyrun", "scripts/transcribe.py", str(nasty), "--out", str(out)]


# --- run_preprocess -----------------------------------------------------------

def _py(code):
    return [sys.executable, "-c", code]


async def test_run_preprocess_success(tmp_path):
    ok, _ = await audio_preprocess.run_preprocess(_py("print('hi')"), tmp_path)
    assert ok is True


async def test_run_preprocess_failure_returns_the_stderr_tail(tmp_path):
    code = "import sys; sys.stderr.write('x' * 5000 + 'BOOM'); sys.exit(3)"
    ok, err = await audio_preprocess.run_preprocess(_py(code), tmp_path)
    assert ok is False
    assert err.endswith("BOOM") and len(err) <= 2000


async def test_run_preprocess_timeout_kills(tmp_path):
    t0 = time.monotonic()
    ok, err = await audio_preprocess.run_preprocess(
        _py("import time; time.sleep(30)"), tmp_path, timeout_s=1)
    assert (ok, err) == (False, "timeout")
    assert time.monotonic() - t0 < 15


async def test_run_preprocess_missing_program_is_a_failure(tmp_path):
    ok, err = await audio_preprocess.run_preprocess(["no-such-program-xyz"], tmp_path)
    assert ok is False and err


async def test_run_preprocess_is_serialized(tmp_path):
    log = tmp_path / "log.txt"
    code = ("import sys, time; open(sys.argv[1], 'a').write('start\\n'); "
            "time.sleep(0.5); open(sys.argv[1], 'a').write('end\\n')")
    cmd = [sys.executable, "-c", code, str(log)]
    r = await asyncio.gather(audio_preprocess.run_preprocess(cmd, tmp_path),
                             audio_preprocess.run_preprocess(cmd, tmp_path))
    assert all(ok for ok, _ in r)
    assert log.read_text().split() == ["start", "end", "start", "end"]


# --- handler ------------------------------------------------------------------

@pytest.fixture
def wired(monkeypatch, tmp_path):
    aa_ws = tmp_path / "AcademicAssistant"
    aa_ws.mkdir()
    (aa_ws / "CLAUDE.md").write_text("x", encoding="utf-8")
    aa = _proj("academicAssistant", aa_ws)
    plain_ws = tmp_path / "Plain"
    plain_ws.mkdir()
    (plain_ws / "CLAUDE.md").write_text("x", encoding="utf-8")
    plain = _proj("plain", plain_ws)
    mans = {
        aa.key: manifest.Manifest(telegram=manifest.TelegramConf(
            group_chat_id=AA_CHAT, agent="tutor", audio_command="/lezione",
            audio_preprocess=TEMPLATE)),
        plain.key: manifest.Manifest(telegram=manifest.TelegramConf(
            group_chat_id=OTHER_CHAT, agent="tutor", audio_command="/lezione")),
    }
    monkeypatch.setattr(manifest, "load", lambda p: mans[p.key])
    monkeypatch.setattr(projects, "list_projects", lambda: [aa, plain])
    monkeypatch.setattr(bot, "resolve_project",
                        lambda k: {"academicassistant": aa, "plain": plain}.get((k or "").lower()))
    monkeypatch.setattr(bot, "resolve_agent",
                        lambda p, name: types.SimpleNamespace(name=name))
    monkeypatch.setattr(bot, "_profile_or_guest", lambda u: types.SimpleNamespace(admin=False))
    monkeypatch.setattr(bot, "_is_stale", lambda m: False)
    monkeypatch.setattr(bot, "can_access", lambda p, k: True)
    monkeypatch.setattr(bot, "_served_by_another_bot", lambda p, b=None: None)
    monkeypatch.setattr(bot, "_current_bot_name", lambda c: "default")
    monkeypatch.setattr(profiles, "user_ids_for", lambda k: [777] if k == "owner" else [])
    invoked = []

    async def fake_invoke(update, project, agent, prompt):
        invoked.append((project.key, agent.name, prompt))

    monkeypatch.setattr(bot, "_invoke_and_reply", fake_invoke)
    dms = []

    async def send_message(chat_id, text, **kw):
        dms.append((chat_id, text))

    fake_bot = types.SimpleNamespace(send_message=send_message)
    return types.SimpleNamespace(aa=aa, ws=aa_ws, plain_ws=plain_ws, invoked=invoked,
                                 dms=dms, context=types.SimpleNamespace(bot=fake_bot))


def _fake_run(monkeypatch, *, ok=True, err="", write=b"testo della lezione", gate=None,
              seen=None):
    async def fake(cmd, cwd, timeout_s=3 * 3600):
        if seen is not None:
            seen.append((list(cmd), cwd))
        if gate is not None:
            await gate.wait()
        out = Path(cmd[cmd.index("--out") + 1])
        if write is not None:
            out.write_bytes(write)
        return ok, err

    monkeypatch.setattr(audio_preprocess, "run_preprocess", fake)


async def _drain():
    while bot._BACKGROUND_TASKS:
        await asyncio.gather(*list(bot._BACKGROUND_TASKS), return_exceptions=True)


async def test_transcription_runs_before_the_agent_and_off_the_handler(wired, monkeypatch):
    gate = asyncio.Event()
    seen = []
    _fake_run(monkeypatch, gate=gate, seen=seen)
    upd, replies, order, downloaded = _update(AA_CHAT, kind="audio", mime="audio/mpeg",
                                              file_name="lezione.mp3")
    handled = await bot.handle_project_audio(upd, wired.context)
    assert handled is True
    # The handler returned while the transcription is still running.
    await asyncio.sleep(0.05)
    assert wired.invoked == []
    assert len(seen) == 1
    cmd, cwd = seen[0]
    assert Path(cwd) == Path(wired.ws)
    assert cmd[:3] == ["sh", "scripts/pyrun", "scripts/transcribe.py"]
    assert Path(cmd[3]) == Path(downloaded[0])
    out = Path(cmd[5])
    assert out.parent == wired.ws / "99_raw" / "trascrizioni"
    assert out.name == "2026-10-12_42_lezione.txt"
    gate.set()
    await _drain()
    assert len(wired.invoked) == 1
    key, agent, prompt = wired.invoked[0]
    assert key == "academicAssistant" and agent == "tutor"
    assert "[CMD:/lezione]" in prompt
    assert "Allegato: .claude/inbox/telegram/2026-10-12_42_lezione.mp3" in prompt
    assert "Trascrizione: 99_raw/trascrizioni/2026-10-12_42_lezione.txt" in prompt
    assert replies[0].startswith("🎙 Ricevuta!")
    assert not bot._BACKGROUND_TASKS


async def test_failed_transcription_says_so_and_warns_the_owner(wired, monkeypatch, caplog):
    _fake_run(monkeypatch, ok=False, err="CUDA? no: Traceback ... boom", write=None)
    upd, replies, *_ = _update(AA_CHAT, kind="voice")
    assert await bot.handle_project_audio(upd, wired.context) is True
    await _drain()
    assert wired.invoked == []
    assert replies[-1] == FAILED_MSG
    assert len(wired.dms) == 1
    chat_id, text = wired.dms[0]
    assert chat_id == 777
    assert "academicAssistant" in text and "boom" in text and "vocale" in text
    assert any(r.levelname == "ERROR" and "boom" in r.getMessage() for r in caplog.records)


async def test_an_empty_transcript_is_a_failure(wired, monkeypatch):
    _fake_run(monkeypatch, ok=True, write=b"")
    upd, replies, *_ = _update(AA_CHAT, kind="voice")
    await bot.handle_project_audio(upd, wired.context)
    await _drain()
    assert wired.invoked == []
    assert replies[-1] == FAILED_MSG


async def test_without_owner_ids_the_group_is_still_told(wired, monkeypatch):
    monkeypatch.setattr(profiles, "user_ids_for", lambda k: [])
    _fake_run(monkeypatch, ok=False, err="boom", write=None)
    upd, replies, *_ = _update(AA_CHAT, kind="voice")
    await bot.handle_project_audio(upd, wired.context)
    await _drain()
    assert replies[-1] == FAILED_MSG and wired.dms == []


async def test_a_crashing_background_task_does_not_escape(wired, monkeypatch, caplog):
    async def explode(cmd, cwd, timeout_s=3 * 3600):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(audio_preprocess, "run_preprocess", explode)
    upd, replies, *_ = _update(AA_CHAT, kind="voice")
    await bot.handle_project_audio(upd, wired.context)
    await _drain()
    assert wired.invoked == []
    assert replies[-1] == FAILED_MSG
    assert any("kaboom" in (r.getMessage() + str(r.exc_info)) for r in caplog.records)


async def test_project_without_audio_preprocess_calls_the_agent_directly(wired, monkeypatch):
    seen = []
    _fake_run(monkeypatch, seen=seen)
    upd, replies, *_ = _update(OTHER_CHAT, kind="voice")
    assert await bot.handle_project_audio(upd, wired.context) is True
    assert seen == [] and not bot._BACKGROUND_TASKS
    assert len(wired.invoked) == 1
    prompt = wired.invoked[0][2]
    assert "[CMD:/lezione]" in prompt and "Allegato:" in prompt
    assert "Trascrizione:" not in prompt


AGENT_FAILED_MSG = ("😕 Ho la trascrizione ma non sono riuscita a preparare gli appunti. "
                    "the owner è stato avvisato: la riprendo appena possibile.")


async def test_agent_turn_failure_after_transcription_tells_group_and_owner(
        wired, monkeypatch, caplog):
    _fake_run(monkeypatch)

    async def failing_invoke(update, project, agent, prompt):
        raise RuntimeError("claude down")

    monkeypatch.setattr(bot, "_invoke_and_reply", failing_invoke)
    upd, replies, *_ = _update(AA_CHAT, kind="voice")
    assert await bot.handle_project_audio(upd, wired.context) is True
    await _drain()
    assert replies[-1] == AGENT_FAILED_MSG
    assert len(wired.dms) == 1
    chat_id, text = wired.dms[0]
    assert chat_id == 777
    assert "Appunti non preparati" in text and "claude down" in text
    assert "99_raw/trascrizioni/" in text
    assert any("claude down" in str(r.exc_info) for r in caplog.records if r.exc_info)


def test_build_command_substitutes_topic_and_empty_topic_is_one_empty_argument():
    tpl = "run {file} --out {out} --argomento {topic}"
    assert audio_preprocess.build_command(tpl, Path("/a/x.ogg"), Path("/o/x.txt"), "Storia dell'arte moderna")[-1] == "Storia dell'arte moderna"
    assert audio_preprocess.build_command(tpl, Path("/a/x.ogg"), Path("/o/x.txt"))[-1] == ""
