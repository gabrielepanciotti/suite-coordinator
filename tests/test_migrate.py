# tests/test_migrate.py
import asyncio

import migrate
import migrate_store as store


def _run(coro):
    return asyncio.run(coro)


class _FakeClient:
    def __init__(self):
        self.created = []          # (to, context)
        self.posts = []            # (conv_id, body, needs_human)
        self._next = 500
        self._convs = {}

    async def create_conversation(self, to, subject, body, priority="normal", context=None):
        self._next += 1
        cid = self._next
        self.created.append((to, context))
        self._convs[cid] = {"id": cid, "status": "open", "messages": []}
        return {"id": cid}

    async def send_message(self, conv_id, body, needs_human=False, context=None):
        self.posts.append((conv_id, body, needs_human))
        return {"id": 1}

    async def get_conversation(self, conv_id):
        return self._convs[conv_id]

    def add_verdict(self, conv_id, msg_id, mid, phase, agent, ok):
        self._convs[conv_id]["messages"].append({
            "id": msg_id, "from_agent": agent,
            "context": {"migrate_verdict": {
                "id": mid, "phase": phase, "agent": agent, "ok": ok, "summary": "s"}}})

    def add_blocked(self, conv_id, msg_id, mid, phase, agent,
                    summary="GDPR", decision_needed="Autorizzi?"):
        self._convs[conv_id]["messages"].append({
            "id": msg_id, "from_agent": agent,
            "context": {"migrate_verdict": {
                "id": mid, "phase": phase, "agent": agent, "blocked": True,
                "summary": summary, "decision_needed": decision_needed}}})


class _Harness:
    def __init__(self):
        self.client = _FakeClient()
        self.threads = []

    async def open_topic(self, mid, title):
        return (900, 901)

    async def send_thread(self, bot, chat_id, topic_id, text):
        self.threads.append(text)


class _Cfg:
    telegram_agent = "owner-telegram"


def _setup(tmp_path):
    store.MIGRATE_STORE_PATH = str(tmp_path / "m.json")
    h = _Harness()
    migrate.configure(client=lambda t, a: h.client, open_topic=h.open_topic,
                      send_thread=h.send_thread, bot=object(), config=_Cfg())
    return h


def _targets():
    from manifest import DbTargetDecl
    return [DbTargetDecl("db-prod", "edge"), DbTargetDecl("db-core", "core")]


def test_start_fans_out_prepare_to_all_targets(tmp_path):
    h = _setup(tmp_path)
    mid = _run(migrate.start_migration("acme", "db", "head", targets=_targets()))
    rec = store.get(mid)
    assert rec["phase"] == "prepare"
    assert len(h.client.created) == 2
    tos = {c[0][0] for c in h.client.created}
    assert tos == {"db-prod", "db-core"}


def _conv_for(h, agent):
    for cid, info in store.get(1)["conv"].items():
        if info["agent"] == agent:
            return int(cid)
    raise AssertionError("no conv")


def test_barrier_waits_for_all_then_advances(tmp_path):
    h = _setup(tmp_path)
    mid = _run(migrate.start_migration("acme", "db", "head", targets=_targets()))
    c1 = _conv_for(h, "db-prod")
    h.client.add_verdict(c1, 10, mid, "prepare", "db-prod", ok=True)
    _run(migrate.on_verdict(c1, 10))
    assert store.get(mid)["phase"] == "prepare"
    c2 = _conv_for(h, "db-core")
    h.client.add_verdict(c2, 11, mid, "prepare", "db-core", ok=True)
    _run(migrate.on_verdict(c2, 11))
    assert store.get(mid)["phase"] == "verify"


def test_prepare_failure_aborts_no_commit(tmp_path):
    h = _setup(tmp_path)
    mid = _run(migrate.start_migration("acme", "db", "head", targets=_targets()))
    c1 = _conv_for(h, "db-prod")
    c2 = _conv_for(h, "db-core")
    h.client.add_verdict(c1, 10, mid, "prepare", "db-prod", ok=True)
    h.client.add_verdict(c2, 11, mid, "prepare", "db-core", ok=False)
    _run(migrate.on_verdict(c1, 10))
    _run(migrate.on_verdict(c2, 11))
    assert store.get(mid)["status"] == "aborted"
    assert len(h.client.created) == 2


def _drive_phase_ok(h, mid, phase, base_msg):
    for i, agent in enumerate(("db-prod", "db-core")):
        c = _conv_for(h, agent)
        h.client.add_verdict(c, base_msg + i, mid, phase, agent, ok=True)
        _run(migrate.on_verdict(c, base_msg + i))


def test_all_phases_ok_finishes(tmp_path):
    h = _setup(tmp_path)
    mid = _run(migrate.start_migration("acme", "db", "head", targets=_targets()))
    _drive_phase_ok(h, mid, "prepare", 10)
    _drive_phase_ok(h, mid, "verify", 20)
    _drive_phase_ok(h, mid, "commit", 30)
    assert store.get(mid)["status"] == "done"
    assert any("✅" in t for t in h.threads)


def test_commit_failure_escalates_partial(tmp_path):
    h = _setup(tmp_path)
    mid = _run(migrate.start_migration("acme", "db", "head", targets=_targets()))
    _drive_phase_ok(h, mid, "prepare", 10)
    _drive_phase_ok(h, mid, "verify", 20)
    c1 = _conv_for(h, "db-prod")
    c2 = _conv_for(h, "db-core")
    h.client.add_verdict(c1, 30, mid, "commit", "db-prod", ok=True)
    h.client.add_verdict(c2, 31, mid, "commit", "db-core", ok=False)
    _run(migrate.on_verdict(c1, 30))
    _run(migrate.on_verdict(c2, 31))
    assert store.get(mid)["status"] == "partial"
    assert any(p[2] for p in h.client.posts)


def test_verdict_dedup_does_not_double_count(tmp_path):
    h = _setup(tmp_path)
    mid = _run(migrate.start_migration("acme", "db", "head", targets=_targets()))
    c1 = _conv_for(h, "db-prod")
    h.client.add_verdict(c1, 10, mid, "prepare", "db-prod", ok=True)
    _run(migrate.on_verdict(c1, 10))
    _run(migrate.on_verdict(c1, 10))
    assert store.get(mid)["phase"] == "prepare"


def test_unknown_conv_ignored(tmp_path):
    _setup(tmp_path)
    assert _run(migrate.on_verdict(123456, 1)) is False


def test_resume_redispatches_missing_targets_only(tmp_path):
    h = _setup(tmp_path)
    mid = _run(migrate.start_migration("acme", "db", "head", targets=_targets()))
    c1 = _conv_for(h, "db-prod")
    h.client.add_verdict(c1, 10, mid, "prepare", "db-prod", ok=True)
    _run(migrate.on_verdict(c1, 10))
    h.client.created.clear()
    _run(migrate.resume_pending())
    redispatched = {c[0][0] for c in h.client.created}
    assert redispatched == {"db-core"}


def test_blocked_verdict_pauses_awaiting_human(tmp_path):
    h = _setup(tmp_path)
    mid = _run(migrate.start_migration("acme", "db", "head", targets=_targets()))
    c1 = _conv_for(h, "db-prod")
    h.client.add_blocked(c1, 10, mid, "prepare", "db-prod",
                         summary="GDPR", decision_needed="Autorizzi?")
    _run(migrate.on_verdict(c1, 10))
    rec = store.get(mid)
    assert rec["status"] == "awaiting_human"
    blocked = store.get_blocked(mid)
    assert blocked and blocked["agent"] == "db-prod" and blocked["phase"] == "prepare"
    # verdict NOT recorded -> barrier not advanced
    assert "db-prod" not in store.phase_results(mid, "prepare")
    # the migration topic was notified about the block
    assert any("BLOCCATO" in t for t in h.threads)


def test_human_decision_procedi_redispatches_authorized(tmp_path):
    h = _setup(tmp_path)
    mid = _run(migrate.start_migration("acme", "db", "head", targets=_targets()))
    c1 = _conv_for(h, "db-prod")
    h.client.add_blocked(c1, 10, mid, "prepare", "db-prod")
    _run(migrate.on_verdict(c1, 10))
    assert store.get(mid)["status"] == "awaiting_human"
    h.client.created.clear()
    handled = _run(migrate.on_human_decision(900, 901, "procedi"))
    assert handled is True
    rec = store.get(mid)
    assert rec["status"] == "running"
    assert store.get_blocked(mid) is None
    # re-dispatched (to the blocked agent + any other target still lacking a
    # verdict) with authorized args + body
    redispatched = {c[0][0] for c in h.client.created}
    assert "db-prod" in redispatched
    assert all(c[1]["migrate"]["args"].get("authorized") is True
               for c in h.client.created)


def test_blocked_with_no_topic_opens_one(tmp_path):
    h = _setup(tmp_path)
    # Block-path opens a DISTINCT topic so we can prove it (not start's) bound it.
    opened = []

    async def _open_topic(mid, title):
        opened.append((mid, title))
        return (700, 701)

    h.open_topic = _open_topic
    migrate.configure(client=lambda t, a: h.client, open_topic=h.open_topic,
                      send_thread=h.send_thread, bot=object(), config=_Cfg())
    mid = _run(migrate.start_migration("acme", "db", "head", targets=_targets()))
    # Strip the thread so the block arrives with no bindable topic.
    store.set_thread(mid, None, None)
    c1 = _conv_for(h, "db-prod")
    h.client.add_blocked(c1, 10, mid, "prepare", "db-prod")
    _run(migrate.on_verdict(c1, 10))
    # _on_blocked called open_topic to create a bindable topic.
    assert opened, "open_topic was not called on the block path"
    rec = store.get(mid)
    assert rec["chat_id"] == 700 and rec["topic_id"] == 701
    # A human reply on that topic now resolves back to the migration.
    assert store.topic_to_migration(700, 701) == mid


def test_blocked_with_double_topic_failure_escalates(tmp_path):
    # Both start_migration and _on_blocked fail to create a topic (non-retryable):
    # the block must NOT strand silently — it falls back to the escalation
    # conversation (needs_human) so a human is still reached (#13).
    h = _setup(tmp_path)

    async def _boom(mid, title):
        raise RuntimeError("chat is not a forum")

    h.open_topic = _boom
    migrate.configure(client=lambda t, a: h.client, open_topic=h.open_topic,
                      send_thread=h.send_thread, bot=object(), config=_Cfg())
    mid = _run(migrate.start_migration("acme", "db", "head", targets=_targets()))
    rec = store.get(mid)
    assert rec["chat_id"] is None and rec["topic_id"] is None  # start's topic failed
    c1 = _conv_for(h, "db-prod")
    h.client.add_blocked(c1, 10, mid, "prepare", "db-prod")
    _run(migrate.on_verdict(c1, 10))
    assert store.get(mid)["status"] == "awaiting_human"
    # Escalation conversation was opened and pinged the human.
    assert any(needs_human for _, _, needs_human in h.client.posts), \
        "double topic failure did not fall back to needs_human escalation"


def test_human_decision_malformed_ids_returns_false(tmp_path):
    _setup(tmp_path)
    assert _run(migrate.on_human_decision("x", None, "procedi")) is False


def test_human_decision_no_aborts(tmp_path):
    h = _setup(tmp_path)
    mid = _run(migrate.start_migration("acme", "db", "head", targets=_targets()))
    c1 = _conv_for(h, "db-prod")
    h.client.add_blocked(c1, 10, mid, "prepare", "db-prod")
    _run(migrate.on_verdict(c1, 10))
    handled = _run(migrate.on_human_decision(900, 901, "no"))
    assert handled is True
    assert store.get(mid)["status"] == "aborted"
    assert store.get_blocked(mid) is None


def test_human_decision_ambiguous_reasks(tmp_path):
    h = _setup(tmp_path)
    mid = _run(migrate.start_migration("acme", "db", "head", targets=_targets()))
    c1 = _conv_for(h, "db-prod")
    h.client.add_blocked(c1, 10, mid, "prepare", "db-prod")
    _run(migrate.on_verdict(c1, 10))
    handled = _run(migrate.on_human_decision(900, 901, "boh"))
    assert handled is True
    assert store.get(mid)["status"] == "awaiting_human"
    assert any("Non ho capito" in t for t in h.threads)


def test_human_decision_unknown_topic_falls_through(tmp_path):
    _setup(tmp_path)
    assert _run(migrate.on_human_decision(12345, 67890, "procedi")) is False


def test_resume_skips_awaiting_human(tmp_path):
    h = _setup(tmp_path)
    mid = _run(migrate.start_migration("acme", "db", "head", targets=_targets()))
    c1 = _conv_for(h, "db-prod")
    h.client.add_blocked(c1, 10, mid, "prepare", "db-prod")
    _run(migrate.on_verdict(c1, 10))
    assert store.get(mid)["status"] == "awaiting_human"
    h.client.created.clear()
    _run(migrate.resume_pending())
    assert h.client.created == []
