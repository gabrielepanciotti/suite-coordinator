# restructure_store.py
"""Persistent per-node restructure state. Atomic JSON writes (temp+rename),
mirroring migrate_store.py. Single-phase (no prepare/verify/commit): each target
completes independently.

Schema:
  {"counter": int,
   "restructures": {"<id>": {
       id, tenant, targets[{agent,host}], status, results{agent->verdict},
       conv{str(conv_id)->{agent}}, processed[], chat_id, topic_id, blocked?}}}
status: running | done | aborted | partial | awaiting_human
"""

from __future__ import annotations

import json
import os
import tempfile

from atomic_io import replace_with_retry

RESTRUCTURE_STORE_PATH = os.environ.get(
    "RESTRUCTURE_STORE_PATH", "/app/data/restructures.json"
)

_TERMINAL = ("done", "aborted", "partial")
_AUTO_RESUME_BLOCKED = ("awaiting_human",)


def _load() -> dict:
    try:
        with open(RESTRUCTURE_STORE_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        data = {}
    data.setdefault("counter", 0)
    data.setdefault("restructures", {})
    return data


def _save(data: dict) -> None:
    d = os.path.dirname(RESTRUCTURE_STORE_PATH) or "."
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
        replace_with_retry(tmp, RESTRUCTURE_STORE_PATH)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def next_id() -> int:
    data = _load()
    data["counter"] = int(data.get("counter", 0)) + 1
    _save(data)
    return data["counter"]


def create(
    rid: int,
    *,
    tenant: str,
    targets: list[dict],
    persona_archetype: str = "node-persona",
) -> None:
    data = _load()
    data["restructures"][str(rid)] = {
        "id": rid,
        "tenant": tenant,
        "targets": targets,
        "status": "running",
        "results": {},
        "conv": {},
        "processed": [],
        "chat_id": None,
        "topic_id": None,
        "persona_archetype": persona_archetype,
    }
    _save(data)


def get(rid: int) -> dict | None:
    return _load()["restructures"].get(str(rid))


def _mutate(rid: int, fn) -> None:
    data = _load()
    rec = data["restructures"].get(str(rid))
    if rec is None:
        return
    fn(rec)
    _save(data)


def set_status(rid: int, status: str) -> None:
    _mutate(rid, lambda r: r.__setitem__("status", status))


def set_thread(rid: int, chat_id, topic_id) -> None:
    def _f(r):
        r["chat_id"] = chat_id
        r["topic_id"] = topic_id

    _mutate(rid, _f)


def record_verdict(rid: int, agent: str, verdict: dict) -> None:
    _mutate(rid, lambda r: r["results"].__setitem__(agent, verdict))


def results(rid: int) -> dict:
    rec = get(rid)
    return dict(rec["results"]) if rec else {}


def set_conv(rid: int, agent: str, conv_id: int) -> None:
    def _f(r):
        r["conv"] = {c: i for c, i in r["conv"].items() if i.get("agent") != agent}
        r["conv"][str(int(conv_id))] = {"agent": agent}

    _mutate(rid, _f)


def conv_to_restructure(conv_id: int) -> tuple[int, str] | None:
    for rec in _load()["restructures"].values():
        info = rec.get("conv", {}).get(str(int(conv_id)))
        if info is not None:
            return int(rec["id"]), info["agent"]
    return None


def mark_processed(rid: int, msg_id: int) -> bool:
    data = _load()
    rec = data["restructures"].get(str(rid))
    if rec is None:
        return False
    if int(msg_id) in rec["processed"]:
        return False
    rec["processed"].append(int(msg_id))
    _save(data)
    return True


def list_pending() -> list[dict]:
    return [r for r in _load()["restructures"].values() if r["status"] not in _TERMINAL]


def set_blocked(rid: int, agent: str, reason: str, decision_needed: str) -> None:
    def _f(r):
        r["status"] = "awaiting_human"
        r["blocked"] = {
            "agent": agent,
            "reason": reason,
            "decision_needed": decision_needed,
        }

    _mutate(rid, _f)


def get_blocked(rid: int) -> dict | None:
    rec = get(rid)
    if rec is None:
        return None
    return rec.get("blocked")


def clear_blocked(rid: int) -> None:
    def _f(r):
        r.pop("blocked", None)
        r["status"] = "running"

    _mutate(rid, _f)


def topic_to_restructure(chat_id, topic_id) -> int | None:
    for rec in _load()["restructures"].values():
        if rec.get("chat_id") is None or rec.get("topic_id") is None:
            continue
        if int(rec["chat_id"]) == int(chat_id) and int(rec["topic_id"]) == int(
            topic_id
        ):
            return int(rec["id"])
    return None


def list_resumable() -> list[dict]:
    return [
        r
        for r in _load()["restructures"].values()
        if r["status"] not in _TERMINAL and r["status"] not in _AUTO_RESUME_BLOCKED
    ]
