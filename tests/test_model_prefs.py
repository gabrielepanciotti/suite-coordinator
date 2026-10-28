# tests/test_model_prefs.py
import model_prefs
from model_prefs import Prefs, parse_prefix


def test_prefix_model_only():
    prefs, rest = parse_prefix("opus: rifai il poller")
    assert (prefs.model, prefs.effort) == ("opus", None)
    assert rest == "rifai il poller"


def test_prefix_model_and_effort_either_order():
    for text in ("opus/high: fai X", "high/opus: fai X", "opus , high : fai X"):
        prefs, rest = parse_prefix(text)
        assert (prefs.model, prefs.effort) == ("opus", "high"), text
        assert rest == "fai X"


def test_prefix_effort_only_and_aliases():
    prefs, rest = parse_prefix("max: pensa bene")
    assert (prefs.model, prefs.effort) == (None, "max")
    assert rest == "pensa bene"
    prefs, _ = parse_prefix("s/medio: ciao")
    assert (prefs.model, prefs.effort) == ("sonnet", "medium")


def test_prefix_is_case_insensitive():
    prefs, rest = parse_prefix("OPUS/XHigh: vai")
    assert (prefs.model, prefs.effort) == ("opus", "xhigh")
    assert rest == "vai"


def test_unknown_words_are_not_a_prefix():
    for text in (
        "nota: comprare il latte",
        "TODO: sistemare",
        "https://esempio.it/x",
        "opus/sonnet: due modelli non hanno senso",
        "high/max: due effort non hanno senso",
    ):
        prefs, rest = parse_prefix(text)
        assert not prefs, text
        assert rest == text, text


def test_prefix_without_a_request_is_left_alone():
    # "opus:" alone is a question about the model, not an instruction.
    prefs, rest = parse_prefix("opus:")
    assert not prefs
    assert rest == "opus:"


def test_empty_and_plain_text_untouched():
    assert parse_prefix("") == (Prefs(), "")
    prefs, rest = parse_prefix("ciao come va")
    assert not prefs and rest == "ciao come va"


def test_merged_over_fills_gaps_without_overriding():
    inline = Prefs(model="opus")
    sticky = Prefs(model="sonnet", effort="low")
    merged = inline.merged_over(sticky)
    assert (merged.model, merged.effort) == ("opus", "low")


def test_prefs_bool_and_roundtrip():
    assert not Prefs()
    assert Prefs(effort="max")
    p = Prefs("opus", "high")
    assert Prefs.from_json(p.to_json()) == p
    assert Prefs.from_json(None) == Prefs()
    assert Prefs.from_json(["garbage", "nope"]) == Prefs()


def test_describe_mentions_env_default_when_unset():
    assert "sonnet (default)" in model_prefs.describe(Prefs(), default_model="sonnet")
    assert "opus" in model_prefs.describe(Prefs(model="opus"), default_model="sonnet")


# --- session-title hygiene (resume list) -------------------------------------
def test_session_title_skips_harness_noise():
    """A session opened with /clear starts with harness bookkeeping; the resume
    button must show what the human typed, not '<local-command-caveat>…'."""
    import projects

    assert projects._text_of("<local-command-caveat>Caveat: blah") is None
    assert projects._text_of("<command-name>/clear</command-name>") is None
    assert projects._text_of("  <system-reminder>x</system-reminder>") is None
    assert projects._text_of("sistemami il poller") == "sistemami il poller"
    # block form: skip the noisy block, keep the first real one
    blocks = [
        {"type": "text", "text": "<local-command-caveat>Caveat: blah"},
        {"type": "text", "text": "riprendi il lavoro di ieri"},
    ]
    assert projects._text_of(blocks) == "riprendi il lavoro di ieri"
