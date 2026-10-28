import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fetch_url import _extract_list_places, classify


def test_classify_placelists_url():
    assert classify("https://www.google.com/maps/placelists/list/ABC123") == "maps-list"


def test_extract_list_places_from_blob():
    # forma stabile: name string seguita entro la finestra da !3d<lat>!4d<lng>
    html = (
        'garbage "Pastéis de Belém" more !3d38.6975!4d-9.2034 garbage '
        '"Castelo de São Jorge" x !3d38.7139!4d-9.1335 tail'
    )
    places = _extract_list_places(html)
    names = [p["place"] for p in places]
    assert "Pastéis de Belém" in names
    assert "Castelo de São Jorge" in names
    pb = next(p for p in places if p["place"] == "Pastéis de Belém")
    assert abs(pb["lat"] - 38.6975) < 1e-6 and abs(pb["lng"] + 9.2034) < 1e-6


def test_extract_list_places_empty():
    assert _extract_list_places("nothing here") == []


def test_classify_single_place_not_list():
    assert classify("https://www.google.com/maps/place/Colosseo/@41.89,12.49,17z") == "maps"


def test_classify_non_google_placelists_is_article():
    # path contains /maps/placelists/ but host is not Google → must NOT be maps-list
    assert classify("https://evil.example.com/maps/placelists/list/X") == "article"


def test_classify_instagram_unaffected():
    assert classify("https://www.instagram.com/p/Abc123/") == "instagram"
