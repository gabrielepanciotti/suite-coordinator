# pipeline_store.py
"""Persistent pipeline state. Atomic JSON writes (temp+rename), mirroring
hub_store.py / notif_store.py. Survives Coordinator restarts so list_pending()
can resume in-flight pipelines.

Schema:
  {"counter": int,
   "pipelines": {"<id>": {
       id, tenant, project, task, status, stage_index, stages[],
       results{stage->{ok,summary,sha}}, conv{stage->conv_id}, blocked{stage,
       reason,decision_needed}, gate, chat_id, topic_id, sha_before,
       processed[]}}}
status: running | awaiting_gate | awaiting_human | done | aborted | failed
"""

from __future__ import annotations

import json
import os
import tempfile

from atomic_io import replace_with_retry

PIPELINE_STORE_PATH = os.environ.get("PIPELINE_STORE_PATH", "/app/data/pipelines.json")

_TERMINAL = ("done", "aborted", "failed")


def _load() -> dict:
    try:
        with open(PIPELINE_STORE_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        data = {}
    data.setdefault("counter", 0)
    data.setdefault("pipelines", {})
    return data


def _save(data: dict) -> None:
    d = os.path.dirname(PIPELINE_STORE_PATH) or "."
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
        replace_with_retry(tmp, PIPELINE_STORE_PATH)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def next_id() -> int:
    data = _load()
    data["counter"] = int(data.get("counter", 0)) + 1
    _save(data)
    return data["counter"]


def create(
    pid: int, *, tenant: str, project: str, task: str, stages: list[dict]
) -> None:
    data = _load()
    data["pipelines"][str(pid)] = {
        "id": pid,
        "tenant": tenant,
        "project": project,
        "task": task,
        "status": "running",
        "stage_index": 0,
        "stages": stages,
        "results": {},
        "conv": {},
        "gate": None,
        "chat_id": None,
        "topic_id": None,
        "sha_before": None,
        "processed": [],
    }
    _save(data)


def get(pid: int) -> dict | None:
    return _load()["pipelines"].get(str(pid))


def _mutate(pid: int, fn) -> None:
    data = _load()
    rec = data["pipelines"].get(str(pid))
    if rec is None:
        return
    fn(rec)
    _save(data)


def set_status(pid: int, status: str) -> None:
    _mutate(pid, lambda r: r.__setitem__("status", status))


def set_stage_index(pid: int, idx: int) -> None:
    _mutate(pid, lambda r: r.__setitem__("stage_index", idx))


def set_stage_result(pid: int, stage_name: str, result: dict) -> None:
    _mutate(pid, lambda r: r["results"].__setitem__(stage_name, result))


def set_conv(pid: int, stage_name: str, conv_id: int) -> None:
    _mutate(pid, lambda r: r["conv"].__setitem__(stage_name, int(conv_id)))


def set_gate(pid: int, decision: str) -> None:
    _mutate(pid, lambda r: r.__setitem__("gate", decision))


def set_thread(pid: int, chat_id: int, topic_id: int) -> None:
    def _f(r):
        r["chat_id"] = chat_id
        r["topic_id"] = topic_id

    _mutate(pid, _f)


def set_sha_before(pid: int, sha: str | None) -> None:
    _mutate(pid, lambda r: r.__setitem__("sha_before", sha))


def mark_processed(pid: int, msg_id: int) -> bool:
    """Record msg_id; return True the first time, False if already seen (dedup)."""
    data = _load()
    rec = data["pipelines"].get(str(pid))
    if rec is None:
        return False
    if int(msg_id) in rec["processed"]:
        return False
    rec["processed"].append(int(msg_id))
    _save(data)
    return True


def conv_to_pipeline(conv_id: int) -> tuple[int, str] | None:
    """Map a dispatched conversation id back to (pipeline_id, stage_name)."""
    for rec in _load()["pipelines"].values():
        for stage_name, cid in rec.get("conv", {}).items():
            if int(cid) == int(conv_id):
                return int(rec["id"]), stage_name
    return None


def list_pending() -> list[dict]:
    return [r for r in _load()["pipelines"].values() if r["status"] not in _TERMINAL]


def set_blocked(pid: int, stage_name: str, reason: str, decision_needed: str) -> None:
    """Mark pipeline `pid` as awaiting a human decision.

    Sets status to "awaiting_human" and stores rec["blocked"] with the
    supplied fields. No-op if pid is unknown.
    """

    def _f(r):
        r["status"] = "awaiting_human"
        r["blocked"] = {
            "stage": stage_name,
            "reason": reason,
            "decision_needed": decision_needed,
        }

    _mutate(pid, _f)


def get_blocked(pid: int) -> dict | None:
    """Return the stored blocked dict or None.

    Returns None if the pipeline is unknown or not currently in
    "awaiting_human" status (i.e. the "blocked" key is absent).
    """
    rec = get(pid)
    if rec is None:
        return None
    return rec.get("blocked")


def clear_blocked(pid: int) -> None:
    """Remove rec["blocked"] and set status back to "running".

    No-op if pid is unknown.
    """

    def _f(r):
        r.pop("blocked", None)
        r["status"] = "running"

    _mutate(pid, _f)


def topic_to_pipeline(chat_id: int, topic_id: int) -> int | None:
    """Reverse lookup: return the pipeline id whose (chat_id, topic_id) matches.

    Compares as ints. Ignores records where chat_id or topic_id is None.
    Returns None if no match is found.
    """
    for rec in _load()["pipelines"].values():
        if rec.get("chat_id") is None or rec.get("topic_id") is None:
            continue
        if int(rec["chat_id"]) == int(chat_id) and int(rec["topic_id"]) == int(
            topic_id
        ):
            return int(rec["id"])
    return None
