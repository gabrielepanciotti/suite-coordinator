# tests/test_restructure_store.py
import restructure_store as store


def _fresh(tmp_path):
    store.RESTRUCTURE_STORE_PATH = str(tmp_path / "restructures.json")
    return store


def _targets():
    return [{"agent": "cosmos", "host": "cosmos"},
            {"agent": "network", "host": "network"}]


def test_next_id_increments(tmp_path):
    s = _fresh(tmp_path)
    assert s.next_id() == 1
    assert s.next_id() == 2


def test_create_and_get(tmp_path):
    s = _fresh(tmp_path)
    rid = s.next_id()
    s.create(rid, tenant="personale", targets=_targets())
    rec = s.get(rid)
    assert rec["status"] == "running"
    assert rec["targets"] == _targets()
    assert rec["results"] == {}
    assert rec["chat_id"] is None and rec["topic_id"] is None


def test_create_persists_persona_archetype(tmp_path):
    s = _fresh(tmp_path)
    rid = s.next_id()
    s.create(rid, tenant="v", targets=_targets(), persona_archetype="node-persona-x")
    assert s.get(rid)["persona_archetype"] == "node-persona-x"


def test_create_default_persona_archetype(tmp_path):
    s = _fresh(tmp_path)
    rid = s.next_id()
    s.create(rid, tenant="v", targets=_targets())
    assert s.get(rid)["persona_archetype"] == "node-persona"


def test_record_verdict_and_results(tmp_path):
    s = _fresh(tmp_path)
    rid = s.next_id()
    s.create(rid, tenant="v", targets=_targets())
    s.record_verdict(rid, "cosmos", {"ok": True, "summary": "x"})
    s.record_verdict(rid, "network", {"ok": False, "summary": "y"})
    res = s.results(rid)
    assert res["cosmos"]["ok"] is True
    assert res["network"]["ok"] is False


def test_conv_routing(tmp_path):
    s = _fresh(tmp_path)
    rid = s.next_id()
    s.create(rid, tenant="v", targets=_targets())
    s.set_conv(rid, "cosmos", 555)
    assert s.conv_to_restructure(555) == (rid, "cosmos")
    assert s.conv_to_restructure(999) is None


def test_set_conv_replaces_prior_entry_for_agent(tmp_path):
    s = _fresh(tmp_path)
    rid = s.next_id()
    s.create(rid, tenant="v", targets=_targets())
    s.set_conv(rid, "cosmos", 111)
    s.set_conv(rid, "cosmos", 222)          # re-dispatch: newest conv wins
    assert s.conv_to_restructure(111) is None
    assert s.conv_to_restructure(222) == (rid, "cosmos")


def test_mark_processed_idempotent(tmp_path):
    s = _fresh(tmp_path)
    rid = s.next_id()
    s.create(rid, tenant="v", targets=_targets())
    assert s.mark_processed(rid, 3) is True
    assert s.mark_processed(rid, 3) is False


def test_set_status_thread(tmp_path):
    s = _fresh(tmp_path)
    rid = s.next_id()
    s.create(rid, tenant="v", targets=_targets())
    s.set_status(rid, "partial")
    s.set_thread(rid, 11, 22)
    rec = s.get(rid)
    assert rec["status"] == "partial"
    assert (rec["chat_id"], rec["topic_id"]) == (11, 22)


def test_list_pending_filters_terminal(tmp_path):
    s = _fresh(tmp_path)
    a, b, c = s.next_id(), s.next_id(), s.next_id()
    for rid in (a, b, c):
        s.create(rid, tenant="v", targets=_targets())
    s.set_status(b, "done")
    s.set_status(c, "aborted")
    assert {m["id"] for m in s.list_pending()} == {a}


def test_blocked_lifecycle(tmp_path):
    s = _fresh(tmp_path)
    rid = s.next_id()
    s.create(rid, tenant="v", targets=_targets())
    s.set_blocked(rid, agent="cosmos", reason="legacy agents", decision_needed="delete?")
    assert s.get(rid)["status"] == "awaiting_human"
    assert s.get_blocked(rid) == {
        "agent": "cosmos", "reason": "legacy agents", "decision_needed": "delete?"}
    # awaiting_human is pending but NOT resumable
    assert rid in {m["id"] for m in s.list_pending()}
    assert rid not in {m["id"] for m in s.list_resumable()}
    s.clear_blocked(rid)
    assert s.get(rid)["status"] == "running"
    assert s.get_blocked(rid) is None
    assert rid in {m["id"] for m in s.list_resumable()}


def test_get_blocked_unknown_and_running(tmp_path):
    s = _fresh(tmp_path)
    assert s.get_blocked(9999) is None
    rid = s.next_id()
    s.create(rid, tenant="v", targets=_targets())
    assert s.get_blocked(rid) is None


def test_set_clear_blocked_unknown_noop(tmp_path):
    s = _fresh(tmp_path)
    s.set_blocked(9999, agent="x", reason="r", decision_needed="d")  # no raise
    s.clear_blocked(9999)                                            # no raise


def test_topic_to_restructure(tmp_path):
    s = _fresh(tmp_path)
    rid = s.next_id()
    s.create(rid, tenant="v", targets=_targets())
    s.set_thread(rid, 1001, 42)
    assert s.topic_to_restructure(1001, 42) == rid
    assert s.topic_to_restructure(9999, 42) is None
    assert s.topic_to_restructure(1001, 99) is None


def test_topic_to_restructure_skips_null_thread(tmp_path):
    s = _fresh(tmp_path)
    rid = s.next_id()
    s.create(rid, tenant="v", targets=_targets())
    assert s.topic_to_restructure(0, 0) is None
