# migrate_store.py
"""Persistent cross-host migration state. Atomic JSON writes (temp+rename),
mirroring pipeline_store.py / hub_store.py.

Schema:
  {"counter": int,
   "migrations": {"<id>": {
       id, tenant, project, rev, targets[{agent,host}], status, phase,
       results{phase->{agent->{ok,summary}}}, conv{str(conv_id)->{agent,phase}},
       processed[], chat_id, topic_id}}}
status: running | done | aborted | partial
"""

from __future__ import annotations

import json
import os
import tempfile

from atomic_io import replace_with_retry

MIGRATE_STORE_PATH = os.environ.get("MIGRATE_STORE_PATH", "/app/data/migrations.json")

_TERMINAL = ("done", "aborted", "partial")
_AUTO_RESUME_BLOCKED = ("awaiting_human",)


def _load() -> dict:
    try:
        with open(MIGRATE_STORE_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        data = {}
    data.setdefault("counter", 0)
    data.setdefault("migrations", {})
    return data


def _save(data: dict) -> None:
    d = os.path.dirname(MIGRATE_STORE_PATH) or "."
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
        replace_with_retry(tmp, MIGRATE_STORE_PATH)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def next_id() -> int:
    data = _load()
    data["counter"] = int(data.get("counter", 0)) + 1
    _save(data)
    return data["counter"]


def create(
    mid: int, *, tenant: str, project: str, rev: str, targets: list[dict]
) -> None:
    data = _load()
    data["migrations"][str(mid)] = {
        "id": mid,
        "tenant": tenant,
        "project": project,
        "rev": rev,
        "targets": targets,
        "status": "running",
        "phase": "prepare",
        "results": {},
        "conv": {},
        "processed": [],
        "chat_id": None,
        "topic_id": None,
    }
    _save(data)


def get(mid: int) -> dict | None:
    return _load()["migrations"].get(str(mid))


def _mutate(mid: int, fn) -> None:
    data = _load()
    rec = data["migrations"].get(str(mid))
    if rec is None:
        return
    fn(rec)
    _save(data)


def set_status(mid: int, status: str) -> None:
    _mutate(mid, lambda r: r.__setitem__("status", status))


def set_phase(mid: int, phase: str) -> None:
    _mutate(mid, lambda r: r.__setitem__("phase", phase))


def set_thread(mid: int, chat_id: int, topic_id: int) -> None:
    def _f(r):
        r["chat_id"] = chat_id
        r["topic_id"] = topic_id

    _mutate(mid, _f)


def record_verdict(mid: int, phase: str, agent: str, verdict: dict) -> None:
    def _f(r):
        r["results"].setdefault(phase, {})[agent] = verdict

    _mutate(mid, _f)


def phase_results(mid: int, phase: str) -> dict:
    rec = get(mid)
    if rec is None:
        return {}
    return dict(rec["results"].get(phase, {}))


def set_conv(mid: int, agent: str, phase: str, conv_id: int) -> None:
    def _f(r):
        r["conv"] = {c: i for c, i in r["conv"].items() if i.get("agent") != agent}
        r["conv"][str(int(conv_id))] = {"agent": agent, "phase": phase}

    _mutate(mid, _f)


def conv_to_migration(conv_id: int) -> tuple[int, str, str] | None:
    for rec in _load()["migrations"].values():
        info = rec.get("conv", {}).get(str(int(conv_id)))
        if info is not None:
            return int(rec["id"]), info["agent"], info["phase"]
    return None


def mark_processed(mid: int, msg_id: int) -> bool:
    data = _load()
    rec = data["migrations"].get(str(mid))
    if rec is None:
        return False
    if int(msg_id) in rec["processed"]:
        return False
    rec["processed"].append(int(msg_id))
    _save(data)
    return True


def list_pending() -> list[dict]:
    return [r for r in _load()["migrations"].values() if r["status"] not in _TERMINAL]


def set_blocked(
    mid: int, phase: str, agent: str, reason: str, decision_needed: str
) -> None:
    """Mark migration `mid` as awaiting a human decision.

    Sets status to "awaiting_human" and stores rec["blocked"] with the
    supplied fields. No-op if mid is unknown.
    """

    def _f(r):
        r["status"] = "awaiting_human"
        r["blocked"] = {
            "phase": phase,
            "agent": agent,
            "reason": reason,
            "decision_needed": decision_needed,
        }

    _mutate(mid, _f)


def get_blocked(mid: int) -> dict | None:
    """Return the stored blocked dict or None.

    Returns None if the migration is unknown or not currently in
    "awaiting_human" status (i.e. the "blocked" key is absent).
    """
    rec = get(mid)
    if rec is None:
        return None
    return rec.get("blocked")


def clear_blocked(mid: int) -> None:
    """Remove rec["blocked"] and set status back to "running".

    No-op if mid is unknown.
    """

    def _f(r):
        r.pop("blocked", None)
        r["status"] = "running"

    _mutate(mid, _f)


def topic_to_migration(chat_id: int, topic_id: int) -> int | None:
    """Reverse lookup: return the migration id whose (chat_id, topic_id) matches.

    Compares as ints. Ignores records where chat_id or topic_id is None.
    Returns None if no match is found.
    """
    for rec in _load()["migrations"].values():
        if rec.get("chat_id") is None or rec.get("topic_id") is None:
            continue
        if int(rec["chat_id"]) == int(chat_id) and int(rec["topic_id"]) == int(
            topic_id
        ):
            return int(rec["id"])
    return None


def list_resumable() -> list[dict]:
    """Like list_pending() but excluding awaiting_human migrations.

    The boot-resume loop must never auto-dispatch migrations that are
    paused waiting on a human decision.
    """
    return [
        r
        for r in _load()["migrations"].values()
        if r["status"] not in _TERMINAL and r["status"] not in _AUTO_RESUME_BLOCKED
    ]
