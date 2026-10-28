# tests/test_migrate_store.py
import migrate_store as store


def _fresh(tmp_path):
    store.MIGRATE_STORE_PATH = str(tmp_path / "migrations.json")
    return store


def _targets():
    return [{"agent": "db-prod", "host": "edge"},
            {"agent": "db-dev-linux", "host": "core"}]


def test_next_id_increments(tmp_path):
    s = _fresh(tmp_path)
    assert s.next_id() == 1
    assert s.next_id() == 2


def test_create_and_get(tmp_path):
    s = _fresh(tmp_path)
    mid = s.next_id()
    s.create(mid, tenant="acme", project="db", rev="head", targets=_targets())
    rec = s.get(mid)
    assert rec["status"] == "running"
    assert rec["phase"] == "prepare"
    assert rec["rev"] == "head"
    assert rec["targets"] == _targets()


def test_record_verdict_and_phase_results(tmp_path):
    s = _fresh(tmp_path)
    mid = s.next_id()
    s.create(mid, tenant="v", project="db", rev="head", targets=_targets())
    s.record_verdict(mid, "prepare", "db-prod", {"ok": True, "summary": "x"})
    s.record_verdict(mid, "prepare", "db-dev-linux", {"ok": False, "summary": "y"})
    res = s.phase_results(mid, "prepare")
    assert res["db-prod"]["ok"] is True
    assert res["db-dev-linux"]["ok"] is False
    assert s.phase_results(mid, "verify") == {}


def test_conv_routing_carries_phase(tmp_path):
    s = _fresh(tmp_path)
    mid = s.next_id()
    s.create(mid, tenant="v", project="db", rev="head", targets=_targets())
    s.set_conv(mid, "db-prod", "prepare", 555)
    assert s.conv_to_migration(555) == (mid, "db-prod", "prepare")
    assert s.conv_to_migration(999) is None


def test_mark_processed_idempotent(tmp_path):
    s = _fresh(tmp_path)
    mid = s.next_id()
    s.create(mid, tenant="v", project="db", rev="head", targets=_targets())
    assert s.mark_processed(mid, 3) is True
    assert s.mark_processed(mid, 3) is False


def test_set_phase_status_thread(tmp_path):
    s = _fresh(tmp_path)
    mid = s.next_id()
    s.create(mid, tenant="v", project="db", rev="head", targets=_targets())
    s.set_phase(mid, "commit")
    s.set_status(mid, "partial")
    s.set_thread(mid, 11, 22)
    rec = s.get(mid)
    assert rec["phase"] == "commit"
    assert rec["status"] == "partial"
    assert (rec["chat_id"], rec["topic_id"]) == (11, 22)


def test_list_pending_filters_terminal(tmp_path):
    s = _fresh(tmp_path)
    a, b, c = s.next_id(), s.next_id(), s.next_id()
    for mid in (a, b, c):
        s.create(mid, tenant="v", project="db", rev="head", targets=_targets())
    s.set_status(b, "done")
    s.set_status(c, "aborted")
    assert {m["id"] for m in s.list_pending()} == {a}


# ---------------------------------------------------------------------------
# awaiting_human / blocked helpers
# ---------------------------------------------------------------------------

def test_set_blocked_status_and_get(tmp_path):
    s = _fresh(tmp_path)
    mid = s.next_id()
    s.create(mid, tenant="v", project="db", rev="head", targets=_targets())
    s.set_blocked(mid, phase="prepare", agent="db-prod",
                  reason="needs DBA sign-off", decision_needed="proceed or abort")
    rec = s.get(mid)
    assert rec["status"] == "awaiting_human"
    blocked = s.get_blocked(mid)
    assert blocked == {
        "phase": "prepare",
        "agent": "db-prod",
        "reason": "needs DBA sign-off",
        "decision_needed": "proceed or abort",
    }


def test_awaiting_human_in_list_pending_not_list_resumable(tmp_path):
    s = _fresh(tmp_path)
    a, b = s.next_id(), s.next_id()
    for mid in (a, b):
        s.create(mid, tenant="v", project="db", rev="head", targets=_targets())
    s.set_blocked(a, phase="prepare", agent="db-prod",
                  reason="auth required", decision_needed="yes/no")
    # list_pending still sees awaiting_human (it is non-terminal)
    pending_ids = {m["id"] for m in s.list_pending()}
    assert a in pending_ids
    assert b in pending_ids
    # list_resumable excludes awaiting_human
    resumable_ids = {m["id"] for m in s.list_resumable()}
    assert a not in resumable_ids
    assert b in resumable_ids


def test_clear_blocked_restores_running(tmp_path):
    s = _fresh(tmp_path)
    mid = s.next_id()
    s.create(mid, tenant="v", project="db", rev="head", targets=_targets())
    s.set_blocked(mid, phase="prepare", agent="db-prod",
                  reason="auth required", decision_needed="yes/no")
    assert s.get(mid)["status"] == "awaiting_human"
    s.clear_blocked(mid)
    rec = s.get(mid)
    assert rec["status"] == "running"
    assert s.get_blocked(mid) is None
    # now appears in list_resumable again
    assert any(m["id"] == mid for m in s.list_resumable())


def test_topic_to_migration_found(tmp_path):
    s = _fresh(tmp_path)
    mid = s.next_id()
    s.create(mid, tenant="v", project="db", rev="head", targets=_targets())
    s.set_thread(mid, 1001, 42)
    assert s.topic_to_migration(1001, 42) == mid


def test_topic_to_migration_unknown(tmp_path):
    s = _fresh(tmp_path)
    mid = s.next_id()
    s.create(mid, tenant="v", project="db", rev="head", targets=_targets())
    s.set_thread(mid, 1001, 42)
    assert s.topic_to_migration(9999, 42) is None
    assert s.topic_to_migration(1001, 99) is None


def test_topic_to_migration_skips_null_thread(tmp_path):
    s = _fresh(tmp_path)
    mid = s.next_id()
    s.create(mid, tenant="v", project="db", rev="head", targets=_targets())
    # chat_id and topic_id are None (default after create)
    assert s.topic_to_migration(0, 0) is None


def test_get_blocked_unknown_mid(tmp_path):
    s = _fresh(tmp_path)
    assert s.get_blocked(9999) is None


def test_get_blocked_running_migration(tmp_path):
    s = _fresh(tmp_path)
    mid = s.next_id()
    s.create(mid, tenant="v", project="db", rev="head", targets=_targets())
    # never blocked — no "blocked" key in record
    assert s.get_blocked(mid) is None


def test_set_blocked_unknown_mid_noop(tmp_path):
    s = _fresh(tmp_path)
    # should not raise
    s.set_blocked(9999, phase="prepare", agent="x", reason="r", decision_needed="d")


def test_clear_blocked_unknown_mid_noop(tmp_path):
    s = _fresh(tmp_path)
    # should not raise
    s.clear_blocked(9999)


def test_list_resumable_excludes_terminal(tmp_path):
    s = _fresh(tmp_path)
    a, b, c = s.next_id(), s.next_id(), s.next_id()
    for mid in (a, b, c):
        s.create(mid, tenant="v", project="db", rev="head", targets=_targets())
    s.set_status(b, "done")
    s.set_status(c, "partial")
    ids = {m["id"] for m in s.list_resumable()}
    assert ids == {a}  # b and c are terminal
