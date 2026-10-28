"""Conversation log: every turn is appended as one JSON line with the full
prompt and reply, so a topic's history can be reconstructed for debugging."""
import json

import bot


def test_log_turn_appends_full_prompt_and_reply(tmp_path, monkeypatch):
    p = tmp_path / "conversations.jsonl"
    monkeypatch.setattr(bot, "CONV_LOG_PATH", str(p))

    bot._log_turn(
        chat_id=-100,
        topic_id=5,
        project_key="cosmos",
        agent_name="builder",
        prompt="come va il backend?",
        reply="tutto verde ✅",
        resume_id="old-sid",
        session_id="new-sid",
        elapsed_ms=1234,
        ok=True,
    )

    lines = p.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert rec["chat"] == -100
    assert rec["topic"] == 5
    assert rec["project"] == "cosmos"
    assert rec["agent"] == "builder"
    assert rec["in"] == "come va il backend?"      # full prompt, not truncated
    assert rec["out"] == "tutto verde ✅"           # full reply (unicode kept)
    assert rec["resume"] == "old-sid"
    assert rec["session"] == "new-sid"
    assert rec["ms"] == 1234 and rec["ok"] is True


def test_log_turn_appends_one_line_per_call(tmp_path, monkeypatch):
    p = tmp_path / "conversations.jsonl"
    monkeypatch.setattr(bot, "CONV_LOG_PATH", str(p))
    for i in range(3):
        bot._log_turn(
            chat_id=1, topic_id=0, project_key="personalChef", agent_name="generale",
            prompt=f"q{i}", reply=f"a{i}", resume_id=None, session_id=None,
            elapsed_ms=i, ok=True,
        )
    assert len(p.read_text(encoding="utf-8").splitlines()) == 3


def test_log_turn_is_skipped_when_data_dir_missing(monkeypatch):
    monkeypatch.setattr(bot, "CONV_LOG_PATH", "/no_such_dir_zzz/conversations.jsonl")
    # Must not raise even though it cannot write.
    bot._log_turn(
        chat_id=1, topic_id=0, project_key="personalChef", agent_name="generale",
        prompt="x", reply="y", resume_id=None, session_id=None, elapsed_ms=0,
        ok=True,
    )
