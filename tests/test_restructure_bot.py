# tests/test_restructure_bot.py
import bot


def test_parse_restructure_args_returns_first_token_or_none():
    assert bot._parse_restructure_args("cosmos") == "cosmos"
    assert bot._parse_restructure_args("cosmos extra") == "cosmos"   # first token only
    assert bot._parse_restructure_args("") is None
    assert bot._parse_restructure_args("   ") is None
