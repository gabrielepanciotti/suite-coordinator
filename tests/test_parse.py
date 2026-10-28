import bot


def test_parse_ok():
    raw = '{"result": "ciao", "session_id": "abc-123"}'
    text, sid = bot._parse_claude_json(raw)
    assert text == "ciao"
    assert sid == "abc-123"


def test_parse_empty_result_falls_back():
    raw = '{"result": "", "session_id": "x"}'
    text, sid = bot._parse_claude_json(raw)
    assert text == "(risposta vuota)"
    assert sid == "x"


def test_parse_non_json_returns_raw():
    text, sid = bot._parse_claude_json("not json at all")
    assert text == "not json at all"
    assert sid is None


def test_chunk_short_text_single():
    assert list(bot.chunk_text("hello", size=100)) == ["hello"]


def test_chunk_long_text_splits():
    text = ("a" * 60 + "\n\n") * 5  # ~310 chars
    chunks = list(bot.chunk_text(text, size=100))
    assert len(chunks) > 1
    assert all(len(c) <= 100 for c in chunks)


# --- stream-json parsing (streaming run_claude) ------------------------------


def test_parse_stream_line_ok():
    ev = bot._parse_stream_line('{"type":"system","session_id":"s1"}')
    assert ev == {"type": "system", "session_id": "s1"}


def test_parse_stream_line_blank_garbage_and_non_dict():
    assert bot._parse_stream_line("") is None
    assert bot._parse_stream_line("   \n") is None
    assert bot._parse_stream_line("not json") is None
    assert bot._parse_stream_line("[1, 2, 3]") is None  # valid JSON but not a dict


def test_result_from_event_success():
    ev = {"type": "result", "subtype": "success", "result": "ciao", "session_id": "s"}
    assert bot._result_from_event(ev) == "ciao"


def test_result_from_event_empty_is_placeholder():
    assert bot._result_from_event({"type": "result", "subtype": "success", "result": ""}) == (
        "(risposta vuota)"
    )


def test_result_from_event_error_subtype():
    out = bot._result_from_event({"type": "result", "subtype": "error_max_turns", "is_error": True})
    assert out.startswith("❌") and "error_max_turns" in out


def test_event_progress_tool_use_shows_tool_name():
    ev = {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Bash"}]}}
    assert bot._event_progress(ev) == "🔧 Bash"


def test_event_progress_text_toolresult_and_ignored():
    assert bot._event_progress(
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "hi"}]}}
    ) == "✍️ scrivo la risposta…"
    assert bot._event_progress({"type": "user"}) == "📥 elaboro un risultato…"
    assert bot._event_progress({"type": "system", "subtype": "init"}) is None
