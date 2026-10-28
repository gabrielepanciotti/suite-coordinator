import importlib


def _store(tmp_path, monkeypatch):
    monkeypatch.setenv("HUB_TOPICS_PATH", str(tmp_path / "hub_topics.json"))
    import hub_store
    importlib.reload(hub_store)
    return hub_store


def test_upsert_and_get_conv(tmp_path, monkeypatch):
    s = _store(tmp_path, monkeypatch)
    s.upsert_conv(42, tenant="personale", chat_id=-100, topic_id=7)
    c = s.get_conv(42)
    assert c["tenant"] == "personale" and c["chat_id"] == -100 and c["topic_id"] == 7
    # No per-conversation session slot any more: sessions are per (conv, agent).
    assert c["turns"] == 0 and "session_id" not in c


def test_session_and_turns(tmp_path, monkeypatch):
    s = _store(tmp_path, monkeypatch)
    s.upsert_conv(42, tenant="personale", chat_id=-100, topic_id=7)
    s.set_session(42, "sid-1", agent_id="life", workspace="/ws/life")
    assert s.get_session(42, "life", "/ws/life") == "sid-1"
    assert s.incr_turn(42) == 1 and s.incr_turn(42) == 2


def test_sessions_are_per_agent_not_per_conversation(tmp_path, monkeypatch):
    # Two participants with a local listener on the same conversation (the real
    # case: 'life' and 'homeResearcher-minipc' on #33). One slot made them
    # alias, so each wake resumed the other's session in its own workspace.
    s = _store(tmp_path, monkeypatch)
    s.set_session(33, "sid-life", agent_id="life", workspace="/ws/life")
    s.set_session(33, "sid-hr", agent_id="homeResearcher", workspace="/ws/hr")
    assert s.get_session(33, "life", "/ws/life") == "sid-life"
    assert s.get_session(33, "homeResearcher", "/ws/hr") == "sid-hr"


def test_session_not_resumed_in_another_workspace(tmp_path, monkeypatch):
    s = _store(tmp_path, monkeypatch)
    s.set_session(33, "sid-life", agent_id="life", workspace="/ws/life")
    assert s.get_session(33, "life", "/ws/elsewhere") is None


def test_unknown_agent_has_no_session(tmp_path, monkeypatch):
    s = _store(tmp_path, monkeypatch)
    s.set_session(33, "sid-life", agent_id="life", workspace="/ws/life")
    assert s.get_session(33, "other", "/ws/other") is None


def test_legacy_shared_session_is_purged_and_never_read(tmp_path, monkeypatch):
    s = _store(tmp_path, monkeypatch)
    s.upsert_conv(33, tenant="personale", chat_id=-100, topic_id=7)
    # Simulate a pre-#30 record: one session for the whole conversation.
    data = s._load()
    data["conversations"]["33"]["session_id"] = "sid-poisoned"
    s._save(data)

    assert s.get_session(33, "life", "/ws/life") is None   # never read
    assert s.purge_legacy_sessions() == 1
    assert "session_id" not in s.get_conv(33)
    assert s.purge_legacy_sessions() == 0                  # idempotent


def test_lookup_by_topic(tmp_path, monkeypatch):
    s = _store(tmp_path, monkeypatch)
    s.upsert_conv(42, tenant="personale", chat_id=-100, topic_id=7)
    assert s.conv_id_for_topic(-100, 7) == 42
    assert s.conv_id_for_topic(-100, 999) is None


def test_cursor_roundtrip(tmp_path, monkeypatch):
    s = _store(tmp_path, monkeypatch)
    assert s.get_cursor("personale", "owner-telegram") is None
    s.set_cursor("personale", "owner-telegram", 17)
    assert s.get_cursor("personale", "owner-telegram") == 17


def test_persists_across_reload(tmp_path, monkeypatch):
    s = _store(tmp_path, monkeypatch)
    s.upsert_conv(1, tenant="personale", chat_id=-1, topic_id=2)
    s2 = _store(tmp_path, monkeypatch)
    assert s2.get_conv(1)["topic_id"] == 2


def test_set_wake_escalated_flag(tmp_path, monkeypatch):
    monkeypatch.setenv("HUB_TOPICS_PATH", str(tmp_path / "h.json"))
    import importlib
    importlib.reload(hub_store := __import__("hub_store"))

    # absent by default
    hub_store.upsert_conv(5, tenant="personale", chat_id=-100, topic_id=1)
    assert not (hub_store.get_conv(5) or {}).get("wake_escalated")

    # set persists and does not wipe existing fields
    hub_store.set_wake_escalated(5)
    rec = hub_store.get_conv(5)
    assert rec["wake_escalated"] is True
    assert rec["topic_id"] == 1

    # works for a conv never upserted before
    hub_store.set_wake_escalated(9)
    assert hub_store.get_conv(9)["wake_escalated"] is True


def test_wake_escalated_is_scoped_to_a_message_id(tmp_path, monkeypatch):
    """The gag remembers WHICH message it gave up on, so a newer one lifts it (#29)."""
    s = _store(tmp_path, monkeypatch)

    assert s.wake_escalated_at(33) is None            # not escalated
    s.set_wake_escalated(33, at_message_id=91)
    assert s.wake_escalated_at(33) == 91

    assert s.clear_wake_escalated(33) is True
    assert s.wake_escalated_at(33) is None
    assert s.clear_wake_escalated(33) is False        # idempotent


def test_clearing_the_gag_resets_the_turn_budget(tmp_path, monkeypatch):
    """Lifting the gag without resetting turns just swaps one permanent silencer
    for the other: the conversation un-gags and the turn cap drops every event."""
    s = _store(tmp_path, monkeypatch)
    for _ in range(13):
        s.incr_turn(7)
    s.set_wake_escalated(7, at_message_id=40)

    assert s.clear_wake_escalated(7) is True
    assert s.get_conv(7)["turns"] == 0

    # a conversation that was never gagged keeps its turns (the cap must hold)
    for _ in range(5):
        s.incr_turn(8)
    assert s.clear_wake_escalated(8) is False
    assert s.get_conv(8)["turns"] == 5


def test_legacy_escalated_record_reads_as_message_zero(tmp_path, monkeypatch):
    """Records written before #29 carry no id: they resolve to 0, so the first
    message that arrives afterwards un-gags them — no migration needed."""
    s = _store(tmp_path, monkeypatch)
    s.upsert_conv(33, tenant="personale", chat_id=-100, topic_id=1)
    rec = s._load()
    rec["conversations"]["33"]["wake_escalated"] = True   # no wake_escalated_at
    s._save(rec)

    assert s.wake_escalated_at(33) == 0


def test_epoch_roundtrip(tmp_path, monkeypatch):
    s = _store(tmp_path, monkeypatch)
    assert s.get_epoch("personale") is None
    s.set_epoch("personale", "1:2026-07-01T10:00:00+00:00")
    assert s.get_epoch("personale") == "1:2026-07-01T10:00:00+00:00"


def test_reset_bus_state_clears_stale_records_and_cursors(tmp_path, monkeypatch):
    s = _store(tmp_path, monkeypatch)
    # tenant-attributed record (from an upsert) + cursor
    s.upsert_conv(1, tenant="personale", chat_id=-100, topic_id=7)
    s.set_wake_escalated(1)
    s.set_cursor("personale", "cosmos-prod", 999)
    # an ambiguous record with NO tenant (created by a pure agent<->agent wake)
    s.set_wake_escalated(2)
    # a record firmly belonging to ANOTHER tenant must survive
    s.upsert_conv(3, tenant="acme", chat_id=-200, topic_id=8)
    s.set_cursor("acme", "offerte-prod", 42)

    removed = s.reset_bus_state("personale")

    assert removed == 2  # conv #1 (personale) + conv #2 (unattributed)
    assert s.get_conv(1) is None
    assert s.get_conv(2) is None
    assert s.get_cursor("personale", "cosmos-prod") is None
    # other tenant untouched
    assert s.get_conv(3)["topic_id"] == 8
    assert s.get_cursor("acme", "offerte-prod") == 42


def _store_with_claims(tmp_path, monkeypatch):
    monkeypatch.setenv("HUB_TOPICS_PATH", str(tmp_path / "hub_topics.json"))
    monkeypatch.setenv("HUB_CLAIMS_DIR", str(tmp_path / "claims"))
    import hub_store
    importlib.reload(hub_store)
    return hub_store


def test_is_watched_true_for_fresh_watch_source(tmp_path, monkeypatch):
    s = _store_with_claims(tmp_path, monkeypatch)
    s.claim("t", "a1", source="watch")
    assert s.is_watched("t", "a1") is True
    assert s.is_claimed("t", "a1") is True  # is_claimed stays source-agnostic


def test_is_watched_false_for_hook_source_even_if_fresh(tmp_path, monkeypatch):
    s = _store_with_claims(tmp_path, monkeypatch)
    s.claim("t", "a1")  # default source="hook"
    assert s.is_watched("t", "a1") is False
    assert s.is_claimed("t", "a1") is True  # a hook claim still satisfies is_claimed


def test_is_watched_false_for_missing_source_field(tmp_path, monkeypatch):
    # hub-auto-claim.sh writes the claim file directly in bash, bypassing
    # claim() entirely -- so it never sets "source". Must be treated as "hook".
    s = _store_with_claims(tmp_path, monkeypatch)
    import json
    import os
    import time
    os.makedirs(s.CLAIMS_DIR, exist_ok=True)
    path = s._claim_path("t", "a1")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"pid": 1, "host": "h", "ts": time.time()}, fh)
    assert s.is_watched("t", "a1") is False
    assert s.is_claimed("t", "a1") is True


def test_is_watched_false_when_expired(tmp_path, monkeypatch):
    s = _store_with_claims(tmp_path, monkeypatch)
    import json
    import os
    import time
    os.makedirs(s.CLAIMS_DIR, exist_ok=True)
    path = s._claim_path("t", "a1")
    stale_ts = time.time() - (s.WATCH_TTL + 5)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"pid": 1, "host": "h", "ts": stale_ts, "source": "watch"}, fh)
    assert s.is_watched("t", "a1") is False
