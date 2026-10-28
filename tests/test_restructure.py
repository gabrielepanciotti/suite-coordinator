# tests/test_restructure.py
import asyncio

import restructure
import restructure_store as store


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

    def add_verdict(self, conv_id, msg_id, rid, agent, ok,
                    persona_name=None, agents=None):
        persona_name = agent if persona_name is None else persona_name
        agents = ([f"{agent}-backend-impl", persona_name] if agents is None else agents)
        self._convs[conv_id]["messages"].append({
            "id": msg_id, "from_agent": agent,
            "context": {"restructure_verdict": {
                "id": rid, "agent": agent, "ok": ok, "persona_name": persona_name,
                "agents": agents, "removed_candidates": [], "summary": "s"}}})

    def add_blocked(self, conv_id, msg_id, rid, agent,
                    summary="legacy", decision_needed="Delete?"):
        self._convs[conv_id]["messages"].append({
            "id": msg_id, "from_agent": agent,
            "context": {"restructure_verdict": {
                "id": rid, "agent": agent, "blocked": True,
                "summary": summary, "decision_needed": decision_needed}}})


class _Harness:
    def __init__(self):
        self.client = _FakeClient()
        self.threads = []

    async def open_topic(self, rid, title):
        return (900, 901)

    async def send_thread(self, bot, chat_id, topic_id, text):
        self.threads.append(text)


class _Cfg:
    telegram_agent = "owner-telegram"


def _setup(tmp_path):
    store.RESTRUCTURE_STORE_PATH = str(tmp_path / "r.json")
    h = _Harness()
    restructure.configure(client=lambda t, a: h.client, open_topic=h.open_topic,
                          send_thread=h.send_thread, bot=object(), config=_Cfg())
    return h


def _targets():
    return [{"agent": "cosmos", "host": "cosmos"},
            {"agent": "network", "host": "network"}]


def _conv_for(h, agent, rid=1):
    for cid, info in store.get(rid)["conv"].items():
        if info["agent"] == agent:
            return int(cid)
    raise AssertionError("no conv")


def test_start_fans_out_to_all_targets(tmp_path):
    h = _setup(tmp_path)
    rid = _run(restructure.start_restructure("personale", targets=_targets()))
    assert store.get(rid)["status"] == "running"
    assert len(h.client.created) == 2
    tos = {c[0][0] for c in h.client.created}
    assert tos == {"cosmos", "network"}
    # every dispatch carried the typed directive
    assert all("agents_restructure" in (c[1] or {}) for c in h.client.created)


def test_barrier_waits_for_all_then_done(tmp_path):
    h = _setup(tmp_path)
    rid = _run(restructure.start_restructure("personale", targets=_targets()))
    c1 = _conv_for(h, "cosmos")
    h.client.add_verdict(c1, 10, rid, "cosmos", ok=True)
    _run(restructure.on_verdict(c1, 10))
    assert store.get(rid)["status"] == "running"          # still waiting on network
    c2 = _conv_for(h, "network")
    h.client.add_verdict(c2, 11, rid, "network", ok=True)
    _run(restructure.on_verdict(c2, 11))
    assert store.get(rid)["status"] == "done"
    assert any("✅" in t for t in h.threads)


def test_failure_escalates_partial(tmp_path):
    h = _setup(tmp_path)
    rid = _run(restructure.start_restructure("personale", targets=_targets()))
    c1 = _conv_for(h, "cosmos")
    c2 = _conv_for(h, "network")
    h.client.add_verdict(c1, 10, rid, "cosmos", ok=True)
    h.client.add_verdict(c2, 11, rid, "network", ok=False)
    _run(restructure.on_verdict(c1, 10))
    _run(restructure.on_verdict(c2, 11))
    assert store.get(rid)["status"] == "partial"
    assert any(needs_human for _, _, needs_human in h.client.posts)


def test_incoherent_manifest_counts_as_failure(tmp_path):
    h = _setup(tmp_path)
    rid = _run(restructure.start_restructure("personale", targets=[
        {"agent": "cosmos", "host": "cosmos"}]))
    c1 = _conv_for(h, "cosmos")
    # ok=True but persona NOT in the reported roster -> verify_manifest False
    h.client.add_verdict(c1, 10, rid, "cosmos", ok=True,
                         persona_name="cosmos", agents=["cosmos-backend-impl"])
    _run(restructure.on_verdict(c1, 10))
    assert store.get(rid)["status"] == "partial"
    assert store.results(rid)["cosmos"]["ok"] is False


def test_verdict_dedup_does_not_double_count(tmp_path):
    h = _setup(tmp_path)
    rid = _run(restructure.start_restructure("personale", targets=_targets()))
    c1 = _conv_for(h, "cosmos")
    h.client.add_verdict(c1, 10, rid, "cosmos", ok=True)
    _run(restructure.on_verdict(c1, 10))
    _run(restructure.on_verdict(c1, 10))
    assert store.get(rid)["status"] == "running"


def test_unknown_conv_ignored(tmp_path):
    _setup(tmp_path)
    assert _run(restructure.on_verdict(123456, 1)) is False


def test_resume_redispatches_missing_targets_only(tmp_path):
    h = _setup(tmp_path)
    rid = _run(restructure.start_restructure("personale", targets=_targets()))
    c1 = _conv_for(h, "cosmos")
    h.client.add_verdict(c1, 10, rid, "cosmos", ok=True)
    _run(restructure.on_verdict(c1, 10))
    h.client.created.clear()
    _run(restructure.resume_pending())
    assert {c[0][0] for c in h.client.created} == {"network"}


def test_blocked_pauses_awaiting_human(tmp_path):
    h = _setup(tmp_path)
    rid = _run(restructure.start_restructure("personale", targets=_targets()))
    c1 = _conv_for(h, "cosmos")
    h.client.add_blocked(c1, 10, rid, "cosmos")
    _run(restructure.on_verdict(c1, 10))
    rec = store.get(rid)
    assert rec["status"] == "awaiting_human"
    blocked = store.get_blocked(rid)
    assert blocked and blocked["agent"] == "cosmos"
    assert "cosmos" not in store.results(rid)          # verdict NOT recorded
    assert any("BLOCCATO" in t for t in h.threads)


def test_human_decision_procedi_redispatches_authorized(tmp_path):
    h = _setup(tmp_path)
    rid = _run(restructure.start_restructure("personale", targets=_targets()))
    c1 = _conv_for(h, "cosmos")
    h.client.add_blocked(c1, 10, rid, "cosmos")
    _run(restructure.on_verdict(c1, 10))
    h.client.created.clear()
    handled = _run(restructure.on_human_decision(900, 901, "procedi"))
    assert handled is True
    assert store.get(rid)["status"] == "running"
    assert store.get_blocked(rid) is None
    assert "cosmos" in {c[0][0] for c in h.client.created}


def test_human_decision_no_aborts(tmp_path):
    h = _setup(tmp_path)
    rid = _run(restructure.start_restructure("personale", targets=_targets()))
    c1 = _conv_for(h, "cosmos")
    h.client.add_blocked(c1, 10, rid, "cosmos")
    _run(restructure.on_verdict(c1, 10))
    handled = _run(restructure.on_human_decision(900, 901, "no"))
    assert handled is True
    assert store.get(rid)["status"] == "aborted"


def test_human_decision_ambiguous_reasks(tmp_path):
    h = _setup(tmp_path)
    rid = _run(restructure.start_restructure("personale", targets=_targets()))
    c1 = _conv_for(h, "cosmos")
    h.client.add_blocked(c1, 10, rid, "cosmos")
    _run(restructure.on_verdict(c1, 10))
    handled = _run(restructure.on_human_decision(900, 901, "boh"))
    assert handled is True
    assert store.get(rid)["status"] == "awaiting_human"
    assert any("Non ho capito" in t for t in h.threads)


def test_human_decision_malformed_ids_returns_false(tmp_path):
    _setup(tmp_path)
    assert _run(restructure.on_human_decision("x", None, "procedi")) is False


def test_human_decision_unknown_topic_falls_through(tmp_path):
    _setup(tmp_path)
    assert _run(restructure.on_human_decision(12345, 67890, "procedi")) is False


def test_resume_redispatch_keeps_custom_persona_archetype(tmp_path):
    h = _setup(tmp_path)
    rid = _run(restructure.start_restructure(
        "personale", targets=[{"agent": "cosmos", "host": "cosmos"}],
        persona_archetype="node-persona-x"))
    h.client.created.clear()
    _run(restructure.resume_pending())
    assert len(h.client.created) == 1
    c = h.client.created[0]
    assert c[1] == {"agents_restructure": {
        "id": rid, "target": "canonical", "persona_archetype": "node-persona-x"}}


def test_resume_skips_awaiting_human(tmp_path):
    h = _setup(tmp_path)
    rid = _run(restructure.start_restructure("personale", targets=_targets()))
    c1 = _conv_for(h, "cosmos")
    h.client.add_blocked(c1, 10, rid, "cosmos")
    _run(restructure.on_verdict(c1, 10))
    h.client.created.clear()
    _run(restructure.resume_pending())
    assert h.client.created == []
