from taskgen import parse_suggestions, apply_policy, Dedup, resolve_category_id
from taskgen_config import TrustPolicy, Suggestion

def test_parse_extracts_json_block():
    text = 'Ecco:\n```json\n{"suggestions":[{"title":"Cena","category":"cucina",' \
           '"when_iso":"2026-07-07T20:00:00+02:00","duration_min":45,' \
           '"description":null,"why":"zucchine"}]}\n```\nfine'
    out = parse_suggestions(text)
    assert len(out) == 1
    assert out[0].title == "Cena"
    assert out[0].category == "cucina"

def test_parse_garbage_returns_empty():
    assert parse_suggestions("nessun json qui") == []
    assert parse_suggestions("") == []

def test_policy_unknown_category_defaults_to_propose():
    p = TrustPolicy(categories={"cucina": "trusted"}, daily_cap=2)
    s = Suggestion("X", "sport", "2026-07-07T10:00:00+02:00", 30, None, None)
    assert apply_policy(s, p, emitted_today=0) == "propose"

def test_policy_trusted_within_cap():
    p = TrustPolicy(categories={"cucina": "trusted"}, daily_cap=2)
    s = Suggestion("X", "cucina", "2026-07-07T10:00:00+02:00", 30, None, None)
    assert apply_policy(s, p, emitted_today=1) == "trusted"

def test_policy_trusted_over_cap_degrades_to_propose():
    p = TrustPolicy(categories={"cucina": "trusted"}, daily_cap=2)
    s = Suggestion("X", "cucina", "2026-07-07T10:00:00+02:00", 30, None, None)
    assert apply_policy(s, p, emitted_today=2) == "propose"

def test_dedup_skips_repeat():
    d = Dedup()
    s = Suggestion("Cena", "cucina", "2026-07-07T20:00:00+02:00", 45, None, None)
    now = 1_000_000.0
    assert d.seen(s, now=now) is False
    d.record(s, now=now)
    assert d.seen(s, now=now + 3600) is True   # within TTL
    assert d.seen(s, now=now + 90000) is False  # TTL (24h) expired

def test_resolve_category_id_case_insensitive():
    cats = [{"id": 7, "name": "Cucina"}, {"id": 3, "name": "sport"}]
    assert resolve_category_id("cucina", cats, None) == 7
    assert resolve_category_id("SPORT", cats, None) == 3

def test_resolve_category_id_unmatched_falls_back():
    assert resolve_category_id("boh", [{"id": 7, "name": "cucina"}], 1) == 1
    assert resolve_category_id("boh", [], None) is None
