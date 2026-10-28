# tests/test_escalation.py
import escalation as esc


# ---------------------------------------------------------------------------
# build_block
# ---------------------------------------------------------------------------

def test_build_block_shape():
    result = esc.build_block("GDPR consent required", "Can we proceed?", origin="auditor")
    assert result == {"escalation": {
        "blocked": True,
        "reason": "GDPR consent required",
        "decision_needed": "Can we proceed?",
        "origin": "auditor",
    }}


def test_build_block_default_origin():
    result = esc.build_block("irreversible action", "go ahead?")
    assert result["escalation"]["origin"] == ""


def test_build_block_coerces_to_str():
    result = esc.build_block(42, 99, origin=0)
    e = result["escalation"]
    assert e["reason"] == "42"
    assert e["decision_needed"] == "99"
    assert e["origin"] == "0"


# ---------------------------------------------------------------------------
# parse_escalation — standalone envelope
# ---------------------------------------------------------------------------

def test_parse_escalation_roundtrip():
    block = esc.build_block("GDPR", "proceed?", origin="auth-agent")
    parsed = esc.parse_escalation(block)
    assert parsed == {"reason": "GDPR", "decision_needed": "proceed?", "origin": "auth-agent"}


def test_parse_escalation_returns_str_fields():
    ctx = {"escalation": {"blocked": True, "reason": 1, "decision_needed": 2, "origin": 3}}
    parsed = esc.parse_escalation(ctx)
    assert parsed == {"reason": "1", "decision_needed": "2", "origin": "3"}


# ---------------------------------------------------------------------------
# parse_escalation — None cases
# ---------------------------------------------------------------------------

def test_parse_escalation_none_for_non_dict():
    assert esc.parse_escalation(None) is None
    assert esc.parse_escalation("string") is None
    assert esc.parse_escalation(42) is None
    assert esc.parse_escalation([]) is None


def test_parse_escalation_none_for_empty_dict():
    assert esc.parse_escalation({}) is None


def test_parse_escalation_none_when_blocked_falsy():
    ctx = {"escalation": {"blocked": False, "reason": "x", "decision_needed": "y"}}
    assert esc.parse_escalation(ctx) is None


def test_parse_escalation_none_when_blocked_missing():
    ctx = {"escalation": {"reason": "x", "decision_needed": "y"}}
    assert esc.parse_escalation(ctx) is None


def test_parse_escalation_none_when_escalation_not_dict():
    assert esc.parse_escalation({"escalation": "blocked"}) is None
    assert esc.parse_escalation({"escalation": None}) is None


def test_parse_escalation_none_for_normal_verdict_without_blocked():
    ctx = {"migrate_verdict": {"id": 1, "phase": "prepare", "agent": "db", "ok": True, "summary": "ok"}}
    assert esc.parse_escalation(ctx) is None


# ---------------------------------------------------------------------------
# parse_escalation — typed verdict paths (migrate_verdict / pipeline_verdict)
# ---------------------------------------------------------------------------

def test_parse_escalation_migrate_verdict_blocked():
    ctx = {"migrate_verdict": {"id": 5, "phase": "commit", "agent": "db-prod",
                                "blocked": True, "summary": "needs human approval",
                                "decision_needed": "GDPR consent"}}
    parsed = esc.parse_escalation(ctx)
    assert parsed is not None
    assert parsed["origin"] == "migrate_verdict"
    assert parsed["reason"] == "needs human approval"
    assert parsed["decision_needed"] == "GDPR consent"


def test_parse_escalation_pipeline_verdict_blocked():
    ctx = {"pipeline_verdict": {"id": 9, "stage": "deploy", "blocked": True,
                                 "summary": "cannot verify identity",
                                 "decision_needed": "authorize deployment?"}}
    parsed = esc.parse_escalation(ctx)
    assert parsed is not None
    assert parsed["origin"] == "pipeline_verdict"
    assert parsed["reason"] == "cannot verify identity"
    assert parsed["decision_needed"] == "authorize deployment?"


def test_parse_escalation_pipeline_verdict_blocked_missing_decision_needed():
    ctx = {"pipeline_verdict": {"id": 9, "stage": "deploy", "blocked": True,
                                 "summary": "irreversible"}}
    parsed = esc.parse_escalation(ctx)
    assert parsed is not None
    assert parsed["decision_needed"] == ""


def test_parse_escalation_pipeline_verdict_not_blocked():
    ctx = {"pipeline_verdict": {"id": 9, "stage": "deploy", "blocked": False,
                                 "ok": True, "summary": "done"}}
    assert esc.parse_escalation(ctx) is None


def test_parse_escalation_migrate_verdict_not_blocked():
    ctx = {"migrate_verdict": {"id": 5, "phase": "prepare", "agent": "db",
                                "blocked": False, "ok": True, "summary": "done"}}
    assert esc.parse_escalation(ctx) is None


# ---------------------------------------------------------------------------
# interpret_decision
# ---------------------------------------------------------------------------

def test_interpret_decision_approve_words():
    for word in ("procedi", "ok", "sì", "si", "approva", "approvo", "vai", "autorizzo", "conferma"):
        assert esc.interpret_decision(word) == "approve", f"expected approve for {word!r}"


def test_interpret_decision_deny_words():
    for word in ("no", "annulla", "stop", "ferma", "nega", "rifiuta"):
        assert esc.interpret_decision(word) == "deny", f"expected deny for {word!r}"


def test_interpret_decision_approve_with_trailing_text():
    assert esc.interpret_decision("Procedi pure") == "approve"
    assert esc.interpret_decision("ok, grazie") == "approve"


def test_interpret_decision_deny_with_trailing_text():
    assert esc.interpret_decision("no, annulla") == "deny"
    assert esc.interpret_decision("annulla tutto") == "deny"
    assert esc.interpret_decision("stop adesso") == "deny"


def test_interpret_decision_case_insensitive():
    assert esc.interpret_decision("PROCEDI") == "approve"
    assert esc.interpret_decision("NO") == "deny"
    assert esc.interpret_decision("Ok") == "approve"


def test_interpret_decision_strips_punctuation():
    assert esc.interpret_decision("ok!") == "approve"
    assert esc.interpret_decision("no.") == "deny"
    assert esc.interpret_decision("procedi;") == "approve"


def test_interpret_decision_ambiguous_returns_none():
    assert esc.interpret_decision("forse") is None
    assert esc.interpret_decision("boh") is None
    assert esc.interpret_decision("magari") is None


def test_interpret_decision_empty_or_whitespace_returns_none():
    assert esc.interpret_decision("") is None
    assert esc.interpret_decision("   ") is None


def test_interpret_decision_none_input_returns_none():
    assert esc.interpret_decision(None) is None
