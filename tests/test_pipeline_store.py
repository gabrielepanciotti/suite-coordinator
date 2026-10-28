# tests/test_pipeline_store.py
import importlib

import pipeline_store as store


def _fresh(tmp_path):
    store.PIPELINE_STORE_PATH = str(tmp_path / "pipelines.json")
    return store


def _stages():
    return [{"name": "test", "type": "test", "agent": "demo-dev", "args": {}},
            {"name": "gate", "type": "gate", "agent": "", "args": {}}]


def test_next_id_increments(tmp_path):
    s = _fresh(tmp_path)
    assert s.next_id() == 1
    assert s.next_id() == 2


def test_create_and_get(tmp_path):
    s = _fresh(tmp_path)
    pid = s.next_id()
    s.create(pid, tenant="acme", project="demo", task="t", stages=_stages())
    rec = s.get(pid)
    assert rec["tenant"] == "acme"
    assert rec["project"] == "demo"
    assert rec["status"] == "running"
    assert rec["stage_index"] == 0
    assert rec["stages"] == _stages()


def test_stage_result_and_index_and_status(tmp_path):
    s = _fresh(tmp_path)
    pid = s.next_id()
    s.create(pid, tenant="v", project="d", task="t", stages=_stages())
    s.set_stage_result(pid, "test", {"ok": True, "summary": "53/53", "sha": "abc"})
    s.set_stage_index(pid, 1)
    s.set_status(pid, "awaiting_gate")
    rec = s.get(pid)
    assert rec["results"]["test"]["sha"] == "abc"
    assert rec["stage_index"] == 1
    assert rec["status"] == "awaiting_gate"


def test_conv_routing(tmp_path):
    s = _fresh(tmp_path)
    pid = s.next_id()
    s.create(pid, tenant="v", project="d", task="t", stages=_stages())
    s.set_conv(pid, "test", 1234)
    assert s.conv_to_pipeline(1234) == (pid, "test")
    assert s.conv_to_pipeline(9999) is None


def test_mark_processed_is_idempotent(tmp_path):
    s = _fresh(tmp_path)
    pid = s.next_id()
    s.create(pid, tenant="v", project="d", task="t", stages=_stages())
    assert s.mark_processed(pid, 7) is True
    assert s.mark_processed(pid, 7) is False   # already seen


def test_gate_thread_sha_setters(tmp_path):
    s = _fresh(tmp_path)
    pid = s.next_id()
    s.create(pid, tenant="v", project="d", task="t", stages=_stages())
    s.set_thread(pid, 111, 222)
    s.set_gate(pid, "ok")
    s.set_sha_before(pid, "oldsha")
    rec = s.get(pid)
    assert (rec["chat_id"], rec["topic_id"]) == (111, 222)
    assert rec["gate"] == "ok"
    assert rec["sha_before"] == "oldsha"


def test_list_pending_filters_terminal(tmp_path):
    s = _fresh(tmp_path)
    a, b, c = s.next_id(), s.next_id(), s.next_id()
    for pid in (a, b, c):
        s.create(pid, tenant="v", project="d", task="t", stages=_stages())
    s.set_status(b, "awaiting_gate")
    s.set_status(c, "done")
    ids = {p["id"] for p in s.list_pending()}
    assert ids == {a, b}      # running + awaiting_gate, not done


def test_survives_reimport(tmp_path):
    s = _fresh(tmp_path)
    pid = s.next_id()
    s.create(pid, tenant="v", project="d", task="t", stages=_stages())
    importlib.reload(store)
    store.PIPELINE_STORE_PATH = str(tmp_path / "pipelines.json")
    assert store.get(pid)["project"] == "d"
