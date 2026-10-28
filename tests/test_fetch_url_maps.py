"""Maps coordinate recovery in tools/fetch_url.py.

Regression for shared Maps links that redirect to `?q=<name|plus code>&ftid=...`
with NO `!3d!4d`/`@lat,lng` marker: coordinates must still be recovered, via a
Plus Code decode or by geocoding the place name. All network calls are stubbed —
the tests are offline and deterministic.
"""
from types import SimpleNamespace

from tools import fetch_url as fu


def test_plus_code_regex_matches_space_and_plus_forms():
    # Google query-decodes the code's '+' to a space; both forms must match.
    assert fu._PLUS_CODE_RE.search("4M6R G7 Cascate")
    assert fu._PLUS_CODE_RE.search("4M6R+G7 Cascate")


def test_reference_candidates_broaden_to_bare_town():
    cands = fu._reference_candidates("Cascate santuario tre fossi, 06181 Assisi PG")
    assert "Cascate santuario tre fossi, 06181 Assisi PG" in cands
    assert "06181 Assisi PG" in cands
    assert "Assisi" in cands  # postal code + province stripped
    assert cands.index("Cascate santuario tre fossi, 06181 Assisi PG") < cands.index("Assisi")


def test_decode_full_plus_code_no_network():
    # A full code decodes directly — no geocoding needed.
    coords = fu._decode_plus_code("8FMJ4M6R+G7 whatever", [])
    assert coords is not None
    lat, lng = coords
    assert round(lat, 3) == 43.111
    assert round(lng, 3) == 12.691


def test_decode_short_plus_code_uses_geocoded_reference(monkeypatch):
    # Short code: locality geocoding supplies the reference for recoverNearest.
    monkeypatch.setattr(fu, "_geocode_query", lambda q, w: (43.07, 12.62) if "Assisi" in q else None)
    coords = fu._decode_plus_code("4M6R G7 Cascate santuario tre fossi, 06181 Assisi PG", [])
    assert coords is not None
    lat, lng = coords
    assert round(lat, 3) == 43.111
    assert round(lng, 3) == 12.691


def test_short_plus_code_unresolvable_locality_returns_none(monkeypatch):
    monkeypatch.setattr(fu, "_geocode_query", lambda q, w: None)
    warnings = []
    assert fu._decode_plus_code("4M6R G7 nowhere", warnings) is None
    assert any("locality could not be geocoded" in w for w in warnings)


def _fake_resp(final_url: str, text: str = ""):
    return SimpleNamespace(url=final_url, text=text, status_code=200)


def test_handle_maps_recovers_coords_by_geocoding_name(monkeypatch):
    url = "https://maps.app.goo.gl/abc"
    final = "https://maps.google.com/maps?q=Ponte+Marchetto,+06181+Assisi+PG&ftid=0x132c:0x111"
    monkeypatch.setattr(fu, "fetch_page", lambda u, w: _fake_resp(final))
    monkeypatch.setattr(fu, "_geocode_query", lambda q, w: (43.0918, 12.6827))
    result = fu.build_result(url)
    fu.handle_maps(url, result)
    assert result["extra"]["lat"] == 43.0918
    assert result["extra"]["lng"] == 12.6827
    assert result["extra"]["place"] == "Ponte Marchetto, 06181 Assisi PG"


def test_handle_maps_strips_plus_code_from_place_name(monkeypatch):
    url = "https://maps.app.goo.gl/xyz"
    final = "https://maps.google.com/maps?q=4M6R+G7+Cascate+tre+fossi,+Assisi&ftid=0x1:0x2"
    monkeypatch.setattr(fu, "fetch_page", lambda u, w: _fake_resp(final))
    monkeypatch.setattr(fu, "_decode_plus_code", lambda q, w: (43.111, 12.691))
    result = fu.build_result(url)
    fu.handle_maps(url, result)
    assert result["extra"]["place"] == "Cascate tre fossi, Assisi"
    assert result["extra"]["lat"] == 43.111
