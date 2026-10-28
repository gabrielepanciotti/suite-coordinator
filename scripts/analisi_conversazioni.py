#!/usr/bin/env python3
"""Estrae segnali grezzi (voti bassi, turni falliti, richieste senza
risposta) dalle conversazioni Telegram di ComUnity, dall'ultimo cursore.
Nessuna sintesi AI qui: produce solo dati correlabili per il passo
successivo (invocazione Claude in run_analisi_comunity.sh)."""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, ".."))
from competenza_routing import settore_per_competenza  # noqa: E402

DATA_DIR = os.environ.get("COORDINATOR_DATA_DIR", os.path.join(_HERE, "..", "data"))
CONV_LOG_PATH = os.environ.get("CONV_LOG_PATH", os.path.join(DATA_DIR, "conversations.jsonl"))
MSG_LOG_PATH = os.environ.get("MSG_LOG_PATH", os.path.join(DATA_DIR, "messages.jsonl"))
RATING_LOG_PATH = os.environ.get("RATING_LOG_PATH", os.path.join(DATA_DIR, "ratings.jsonl"))

UNANSWERED_WINDOW_SEC = 30 * 60
TIPI = ("errore_tecnico", "voto_basso", "richiesta_senza_risposta")


def _parse_ts(s: str) -> datetime:
    return datetime.fromisoformat(s)


def _read_jsonl(path: str) -> list[dict]:
    if not os.path.isfile(path):
        return []
    rows = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def _dedup_ratings(rows: list[dict]) -> list[dict]:
    latest: dict[tuple, dict] = {}
    for r in rows:
        key = (r.get("message_id"), r.get("voter_id"))
        prev = latest.get(key)
        if prev is None or r.get("ts", "") > prev.get("ts", ""):
            latest[key] = r
    return list(latest.values())


def _testo_messaggio(m: dict) -> str:
    """Il testo del messaggio: `testo`, poi text/caption del raw, poi un
    segnaposto col tipo (foto, vocale...) così l'episodio non è mai muto."""
    t = (m.get("testo") or "").strip()
    if t:
        return t
    raw = m.get("raw") or {}
    t = (raw.get("text") or raw.get("caption") or "").strip()
    if t:
        return t
    tipo = m.get("tipo")
    return f"[{tipo}]" if tipo and tipo != "testo" else ""


def _dedup_messaggi(rows: list[dict]) -> list[dict]:
    """Un messaggio = un episodio. Lo stesso messaggio arriva più volte (due bot
    nello stesso gruppo, modifiche, replay): chiave (chat, msg_id), o in
    mancanza di msg_id (chat, topic, ts, autore). Vince la copia con testo, a
    parità l'ultima."""
    best: dict[tuple, dict] = {}
    for m in rows:
        key = ((m.get("chat"), m.get("msg_id")) if m.get("msg_id") is not None
               else (m.get("chat"), m.get("topic"), m.get("ts"), m.get("da_id")))
        prev = best.get(key)
        if prev is None or bool(_testo_messaggio(m)) >= bool(_testo_messaggio(prev)):
            best[key] = m
    return list(best.values())


def estrai(since: datetime) -> tuple[list[dict], datetime]:
    max_ts = since
    episodi: list[dict] = []

    convs = [c for c in _read_jsonl(CONV_LOG_PATH) if c.get("project") == "comunity"]
    convs = [c for c in convs if _parse_ts(c["ts"]) > since]
    for c in convs:
        max_ts = max(max_ts, _parse_ts(c["ts"]))
        if c.get("ok") is False:
            episodi.append({
                "tipo": "errore_tecnico",
                "ts": c["ts"],
                "topic": c.get("topic"),
                "agente": c.get("agent"),
                "richiesta": (c.get("in") or "")[:300],
                "risposta": (c.get("out") or "")[:300],
            })

    ratings = [r for r in _read_jsonl(RATING_LOG_PATH) if r.get("project") == "comunity"]
    ratings = [r for r in ratings if _parse_ts(r["ts"]) > since]
    for r in _dedup_ratings(ratings):
        max_ts = max(max_ts, _parse_ts(r["ts"]))
        if (r.get("rating") if r.get("rating") is not None else 5) <= 2:
            episodi.append({
                "tipo": "voto_basso",
                "ts": r["ts"],
                "topic": r.get("topic_id"),
                "message_id": r.get("message_id"),
                "voto": r.get("rating"),
                "votante": r.get("voter_name"),
            })

    msgs = [m for m in _read_jsonl(MSG_LOG_PATH)
            if m.get("chat_nome") == "ComUnity"
            and m.get("tipo") not in ("comando", "servizio")]
    msgs = _dedup_messaggi([m for m in msgs if _parse_ts(m["ts"]) > since])
    for m in msgs:
        max_ts = max(max_ts, _parse_ts(m["ts"]))
        t0 = _parse_ts(m["ts"])
        risposto = any(
            c.get("topic") == m.get("topic")
            and 0 <= (_parse_ts(c["ts"]) - t0).total_seconds() <= UNANSWERED_WINDOW_SEC
            for c in convs
        )
        if not risposto:
            testo = _testo_messaggio(m)
            ep = {
                "tipo": "richiesta_senza_risposta",
                "ts": m["ts"],
                "topic": m.get("topic"),
                "richiesta": testo[:300],
            }
            settore = settore_per_competenza(testo)
            if settore:
                ep["offerta_competenza"] = True
                ep["settore_suggerito"] = settore
            episodi.append(ep)

    episodi.sort(key=lambda e: e["ts"])
    return episodi, max_ts


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    since = _parse_ts(args.since)
    episodi, max_ts = estrai(since)

    out = {
        "since": args.since,
        "generato": datetime.now(timezone.utc).isoformat(),
        "episodi": episodi,
        "conteggi": {
            "totale": len(episodi),
            "per_tipo": {t: sum(1 for e in episodi if e["tipo"] == t) for t in TIPI},
        },
    }
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(out, fh, ensure_ascii=False, indent=2)

    new_cursor = max_ts if max_ts > since else since
    print(f"NEW_CURSOR={new_cursor.isoformat()}")


if __name__ == "__main__":
    main()
