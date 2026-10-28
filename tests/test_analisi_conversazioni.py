from pathlib import Path
"""Test dello script di estrazione segnali (nessuna AI qui: solo dati grezzi)."""
import json
import sys
from datetime import datetime, timezone

import pytest

sys.path.insert(0, "scripts")
import analisi_conversazioni as ac


def _write_jsonl(path, rows):
    with open(path, "w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")


@pytest.fixture
def datadir(tmp_path, monkeypatch):
    conv = tmp_path / "conversations.jsonl"
    msg = tmp_path / "messages.jsonl"
    rating = tmp_path / "ratings.jsonl"
    monkeypatch.setattr(ac, "CONV_LOG_PATH", str(conv))
    monkeypatch.setattr(ac, "MSG_LOG_PATH", str(msg))
    monkeypatch.setattr(ac, "RATING_LOG_PATH", str(rating))
    return conv, msg, rating


def test_flags_failed_turn(datadir):
    conv, msg, rating = datadir
    _write_jsonl(conv, [
        {"ts": "2026-09-01T10:00:00+02:00", "project": "comunity", "topic": 2,
         "agent": "settore-dev", "ok": False, "in": "fai x", "out": "⏱ timeout"},
        {"ts": "2026-09-01T10:05:00+02:00", "project": "altro", "topic": 1,
         "agent": "x", "ok": False, "in": "y", "out": "z"},
    ])
    episodi, _ = ac.estrai(datetime.fromisoformat("2026-08-01T00:00:00+00:00"))
    tipi = [e["tipo"] for e in episodi]
    assert tipi.count("errore_tecnico") == 1  # il progetto "altro" è escluso


def test_flags_low_rating_and_dedups_by_voter(datadir):
    conv, msg, rating = datadir
    _write_jsonl(rating, [
        {"ts": "2026-09-01T10:00:00+02:00", "project": "comunity", "message_id": 1,
         "voter_id": 5, "voter_name": "the owner", "rating": 4, "topic_id": 2},
        {"ts": "2026-09-01T10:01:00+02:00", "project": "comunity", "message_id": 1,
         "voter_id": 5, "voter_name": "the owner", "rating": 1, "topic_id": 2},
    ])
    episodi, _ = ac.estrai(datetime.fromisoformat("2026-08-01T00:00:00+00:00"))
    assert len(episodi) == 1
    assert episodi[0]["tipo"] == "voto_basso"
    assert episodi[0]["voto"] == 1  # l'ultimo voto vince, non il primo


def test_flags_unanswered_message(datadir):
    conv, msg, rating = datadir
    _write_jsonl(msg, [
        {"ts": "2026-09-01T10:00:00+02:00", "chat_nome": "ComUnity", "topic": 2,
         "tipo": "testo", "testo": "qualcuno mi aiuta?"},
    ])
    episodi, _ = ac.estrai(datetime.fromisoformat("2026-08-01T00:00:00+00:00"))
    assert any(e["tipo"] == "richiesta_senza_risposta" for e in episodi)


def test_does_not_flag_answered_message(datadir):
    conv, msg, rating = datadir
    _write_jsonl(msg, [
        {"ts": "2026-09-01T10:00:00+02:00", "chat_nome": "ComUnity", "topic": 2,
         "tipo": "testo", "testo": "qualcuno mi aiuta?"},
    ])
    _write_jsonl(conv, [
        {"ts": "2026-09-01T10:05:00+02:00", "project": "comunity", "topic": 2,
         "agent": "settore-dev", "ok": True, "in": "qualcuno mi aiuta?", "out": "certo"},
    ])
    episodi, _ = ac.estrai(datetime.fromisoformat("2026-08-01T00:00:00+00:00"))
    assert not any(e["tipo"] == "richiesta_senza_risposta" for e in episodi)


def test_since_filters_old_rows(datadir):
    conv, msg, rating = datadir
    _write_jsonl(conv, [
        {"ts": "2026-01-01T10:00:00+02:00", "project": "comunity", "topic": 2,
         "agent": "x", "ok": False, "in": "vecchio", "out": "vecchio"},
    ])
    episodi, new_cursor = ac.estrai(datetime.fromisoformat("2026-08-01T00:00:00+00:00"))
    assert episodi == []
    assert new_cursor == datetime.fromisoformat("2026-08-01T00:00:00+00:00")


def _since():
    return datetime.fromisoformat("2026-08-01T00:00:00+00:00")


def test_same_message_logged_twice_is_one_episode_with_text(datadir):
    """ComUnity#178: due copie dello stesso msg (una vuota) = un solo episodio."""
    conv, msg, rating = datadir
    base = {"ts": "2026-09-08T23:50:02+02:00", "chat": -1, "chat_nome": "ComUnity",
            "topic": 72, "msg_id": 900, "tipo": "testo"}
    _write_jsonl(msg, [
        {**base, "testo": ""},
        {**base, "testo": "Ciao sarai il nostro supporto in tutto l'aspetto di legalità e branding."},
    ])
    episodi, _ = ac.estrai(_since())
    assert len(episodi) == 1
    assert "legalità" in episodi[0]["richiesta"]


def test_empty_text_recovered_from_raw_or_type_placeholder(datadir):
    conv, msg, rating = datadir
    _write_jsonl(msg, [
        {"ts": "2026-09-07T18:24:29+02:00", "chat": -1, "chat_nome": "ComUnity", "topic": 61,
         "msg_id": 1, "tipo": "foto", "testo": "", "raw": {"caption": "guardate qui"}},
        {"ts": "2026-09-07T18:24:29+02:00", "chat": -1, "chat_nome": "ComUnity", "topic": 62,
         "msg_id": 2, "tipo": "vocale", "testo": ""},
    ])
    episodi, _ = ac.estrai(_since())
    assert [e["richiesta"] for e in episodi] == ["guardate qui", "[vocale]"]


def test_service_messages_are_not_unanswered_requests(datadir):
    conv, msg, rating = datadir
    _write_jsonl(msg, [{"ts": "2026-09-07T18:00:00+02:00", "chat": -1, "chat_nome": "ComUnity",
                        "topic": 3, "msg_id": 5, "tipo": "servizio", "testo": ""}])
    episodi, _ = ac.estrai(_since())
    assert episodi == []


@pytest.mark.skipif(not (Path(__file__).resolve().parents[1] / "locale" / "defaults.json").exists(),
                    reason="needs the private locale/defaults.json (not published)")
def test_competence_offer_is_routed_to_sector(datadir):
    conv, msg, rating = datadir
    _write_jsonl(msg, [{"ts": "2026-09-08T23:50:02+02:00", "chat": -1, "chat_nome": "ComUnity",
                        "topic": 72, "msg_id": 900, "tipo": "testo",
                        "testo": "Ciao sarai il nostro supporto in tutto l'aspetto di legalità e branding."}])
    episodi, _ = ac.estrai(_since())
    assert episodi[0]["offerta_competenza"] is True
    assert episodi[0]["settore_sugmemberto"] == "settore-azienda"


@pytest.mark.parametrize("testo,atteso", [
    ("Sono una grafica, mi occupo di branding e logo", "settore-marketing"),
    ("Mi occupo di privacy e contratti, vi offro supporto", "settore-azienda"),
    ("Sono uno sviluppatore, mi metto a disposizione", "settore-dev"),
    ("qualcuno mi aiuta con la password?", None),
    ("il logo è bello", None),
    ("", None),
])
def test_settore_per_competenza(testo, atteso):
    from competenza_routing import settore_per_competenza
    assert settore_per_competenza(testo) == atteso
