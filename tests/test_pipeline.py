# tests/test_pipeline.py
import asyncio

import pipeline
import pipeline_stages as ps
import pipeline_store as store


def _run(coro):
    return asyncio.run(coro)


class _FakeClient:
    """Stand-in for HubClient: records dispatches, returns canned conversations."""
    def __init__(self):
        self.created = []          # (to, subject, context)
        self.posts = []            # (conv_id, body, needs_human)
        self._next_conv = 100
        self._convs = {}           # conv_id -> conv dict (for fetch)

    async def create_conversation(self, to, subject, body, priority="normal", context=None):
        self._next_conv += 1
        cid = self._next_conv
        self.created.append((to, subject, context))
        self._convs[cid] = {"id": cid, "status": "open", "messages": []}
        return {"id": cid}

    async def send_message(self, conv_id, body, needs_human=False, context=None):
        self.posts.append((conv_id, body, needs_human))
        return {"id": 9}

    async def get_conversation(self, conv_id):
        return self._convs[conv_id]

    # test helper: simulate the stage agent replying with a typed verdict
    def add_verdict(self, conv_id, msg_id, pid, stage, ok, sha=None):
        self._convs[conv_id]["messages"].append({
            "id": msg_id, "from_agent": "agent",
            "context": {"pipeline_verdict": {
                "id": pid, "stage": stage, "ok": ok, "summary": "s", "sha": sha}},
        })

    # test helper: simulate the stage agent replying with a blocked verdict
    def add_blocked(self, conv_id, msg_id, pid, stage,
                    summary="GDPR", decision_needed="Autorizzi?"):
        self._convs[conv_id]["messages"].append({
            "id": msg_id, "from_agent": "agent",
            "context": {"pipeline_verdict": {
                "id": pid, "stage": stage, "blocked": True,
                "summary": summary, "decision_needed": decision_needed}},
        })


class _Harness:
    def __init__(self):
        self.client = _FakeClient()
        self.topics = []           # (pid, title)
        self.threads = []          # (chat, topic, text)
        self.gates = []            # (pid, chat, topic, text)

    async def open_topic(self, pid, title):
        self.topics.append((pid, title))
        return (555, 666)

    async def send_thread(self, bot, chat_id, topic_id, text):
        self.threads.append((chat_id, topic_id, text))

    async def post_gate(self, pid, chat_id, topic_id, text):
        self.gates.append((pid, chat_id, topic_id, text))


class _Cfg:
    telegram_agent = "owner-telegram"
    pipeline_gate_timeout_h = 24


def _setup(tmp_path):
    store.PIPELINE_STORE_PATH = str(tmp_path / "p.json")
    h = _Harness()
    pipeline.configure(
        client=lambda tenant, agent: h.client,
        open_topic=h.open_topic, send_thread=h.send_thread,
        post_gate=h.post_gate, bot=object(), config=_Cfg(),
    )
    return h


def test_start_pipeline_dispatches_first_stage(tmp_path):
    h = _setup(tmp_path)
    stages = [ps.PipelineStageDecl("test", "test", "demo-dev"),
              ps.PipelineStageDecl("gate", "gate")]
    pid = _run(pipeline.start_pipeline("acme", "demo", "do it", stages=stages))
    rec = store.get(pid)
    assert rec["status"] == "running"
    assert h.client.created[0][0] == ["demo-dev"]                    # dispatched to dev
    assert h.client.created[0][2]["pipeline"]["stage"] == "test"     # typed command
    assert store.conv_to_pipeline(101) == (pid, "test")             # conv recorded


def test_verdict_ok_advances_to_next_stage(tmp_path):
    h = _setup(tmp_path)
    stages = [ps.PipelineStageDecl("implement", "implement", "demo-dev"),
              ps.PipelineStageDecl("test", "test", "demo-dev")]
    pid = _run(pipeline.start_pipeline("acme", "demo", "do it", stages=stages))
    conv_impl = store.get(pid)["conv"]["implement"]
    h.client.add_verdict(conv_impl, 5, pid, "implement", ok=True)
    _run(pipeline.on_verdict(conv_impl, 5))
    rec = store.get(pid)
    assert rec["stage_index"] == 1
    assert rec["results"]["implement"]["ok"] is True
    assert "test" in rec["conv"]                                    # second stage dispatched


def test_pipeline_finishes_after_last_stage(tmp_path):
    h = _setup(tmp_path)
    stages = [ps.PipelineStageDecl("test", "test", "demo-dev")]
    pid = _run(pipeline.start_pipeline("acme", "demo", "do it", stages=stages))
    conv = store.get(pid)["conv"]["test"]
    h.client.add_verdict(conv, 5, pid, "test", ok=True)
    _run(pipeline.on_verdict(conv, 5))
    assert store.get(pid)["status"] == "done"
    assert any("✅" in t[2] for t in h.threads)                      # success posted


def test_verdict_dedup_does_not_double_advance(tmp_path):
    h = _setup(tmp_path)
    stages = [ps.PipelineStageDecl("implement", "implement", "demo-dev"),
              ps.PipelineStageDecl("test", "test", "demo-dev")]
    pid = _run(pipeline.start_pipeline("acme", "demo", "do it", stages=stages))
    conv = store.get(pid)["conv"]["implement"]
    h.client.add_verdict(conv, 5, pid, "implement", ok=True)
    _run(pipeline.on_verdict(conv, 5))
    _run(pipeline.on_verdict(conv, 5))            # redelivery
    assert store.get(pid)["stage_index"] == 1     # advanced once, not twice


def test_verdict_for_unknown_conv_is_ignored(tmp_path):
    _setup(tmp_path)
    assert _run(pipeline.on_verdict(999999, 1)) is False


def test_gate_stage_posts_buttons_and_blocks(tmp_path):
    h = _setup(tmp_path)
    stages = [ps.PipelineStageDecl("gate", "gate"),
              ps.PipelineStageDecl("deploy", "deploy", "deployer-x")]
    pid = _run(pipeline.start_pipeline("acme", "demo", "do it", stages=stages))
    assert store.get(pid)["status"] == "awaiting_gate"
    assert h.gates and h.gates[0][0] == pid          # gate buttons posted
    assert not h.client.created                       # nothing dispatched yet


def test_gate_approve_advances_to_deploy(tmp_path):
    h = _setup(tmp_path)
    stages = [ps.PipelineStageDecl("gate", "gate"),
              ps.PipelineStageDecl("deploy", "deploy", "deployer-x")]
    pid = _run(pipeline.start_pipeline("acme", "demo", "do it", stages=stages))
    _run(pipeline.on_gate_decision(pid, "ok"))
    rec = store.get(pid)
    assert rec["gate"] == "ok"
    assert rec["status"] == "running"
    assert h.client.created[0][0] == ["deployer-x"]   # deploy dispatched


def test_gate_reject_aborts(tmp_path):
    h = _setup(tmp_path)
    stages = [ps.PipelineStageDecl("gate", "gate"),
              ps.PipelineStageDecl("deploy", "deploy", "deployer-x")]
    pid = _run(pipeline.start_pipeline("acme", "demo", "do it", stages=stages))
    _run(pipeline.on_gate_decision(pid, "no"))
    assert store.get(pid)["status"] == "aborted"
    assert not h.client.created
    assert any("❌" in t[2] for t in h.threads)


def test_dev_stage_failure_aborts(tmp_path):
    h = _setup(tmp_path)
    stages = [ps.PipelineStageDecl("test", "test", "demo-dev"),
              ps.PipelineStageDecl("deploy", "deploy", "deployer-x")]
    pid = _run(pipeline.start_pipeline("acme", "demo", "do it", stages=stages))
    conv = store.get(pid)["conv"]["test"]
    h.client.add_verdict(conv, 5, pid, "test", ok=False)
    _run(pipeline.on_verdict(conv, 5))
    assert store.get(pid)["status"] == "aborted"
    assert not any(c[0] == ["deployer-x"] for c in h.client.created)  # never deployed


def test_deploy_failure_rolls_back_when_sha_known(tmp_path):
    h = _setup(tmp_path)
    stages = [ps.PipelineStageDecl("test", "test", "demo-dev"),
              ps.PipelineStageDecl("deploy", "deploy", "deployer-x")]
    pid = _run(pipeline.start_pipeline("acme", "demo", "do it", stages=stages))
    ctest = store.get(pid)["conv"]["test"]
    h.client.add_verdict(ctest, 5, pid, "test", ok=True, sha="goodsha")
    _run(pipeline.on_verdict(ctest, 5))
    assert store.get(pid)["sha_before"] == "goodsha"
    cdep = store.get(pid)["conv"]["deploy"]
    h.client.add_verdict(cdep, 6, pid, "deploy", ok=False)
    _run(pipeline.on_verdict(cdep, 6))
    rollback = h.client.created[-1]
    assert rollback[0] == ["deployer-x"]
    assert rollback[2]["pipeline"]["args"]["rollback"] is True
    assert rollback[2]["pipeline"]["args"]["to_sha"] == "goodsha"


def test_deploy_failure_without_sha_escalates(tmp_path):
    h = _setup(tmp_path)
    stages = [ps.PipelineStageDecl("deploy", "deploy", "deployer-x")]
    pid = _run(pipeline.start_pipeline("acme", "demo", "do it", stages=stages))
    cdep = store.get(pid)["conv"]["deploy"]
    h.client.add_verdict(cdep, 6, pid, "deploy", ok=False)
    _run(pipeline.on_verdict(cdep, 6))
    assert store.get(pid)["status"] == "failed"
    assert any(p[2] for p in h.client.posts)          # needs_human posted


def test_resume_pending_redispatches_running_stage(tmp_path):
    h = _setup(tmp_path)
    stages = [ps.PipelineStageDecl("test", "test", "demo-dev")]
    pid = _run(pipeline.start_pipeline("acme", "demo", "do it", stages=stages))
    h.client.created.clear()                          # simulate restart
    _run(pipeline.resume_pending())
    assert h.client.created[0][0] == ["demo-dev"]     # stage re-dispatched


def test_blocked_verdict_pauses_awaiting_human(tmp_path):
    h = _setup(tmp_path)
    stages = [ps.PipelineStageDecl("implement", "implement", "demo-dev")]
    pid = _run(pipeline.start_pipeline("acme", "demo", "do it", stages=stages))
    conv = store.get(pid)["conv"]["implement"]
    h.client.add_blocked(conv, 5, pid, "implement",
                         summary="GDPR", decision_needed="Autorizzi?")
    _run(pipeline.on_verdict(conv, 5))
    rec = store.get(pid)
    assert rec["status"] == "awaiting_human"
    assert rec["stage_index"] == 0                    # not advanced
    blocked = store.get_blocked(pid)
    assert blocked and blocked["stage"] == "implement"
    # verdict NOT recorded -> stage not advanced
    assert "implement" not in (rec.get("results") or {})
    # the pipeline topic was notified about the block
    assert any("BLOCCATO" in t[2] for t in h.threads)


def test_human_decision_procedi_redispatches_stage(tmp_path):
    h = _setup(tmp_path)
    stages = [ps.PipelineStageDecl("implement", "implement", "demo-dev")]
    pid = _run(pipeline.start_pipeline("acme", "demo", "do it", stages=stages))
    conv = store.get(pid)["conv"]["implement"]
    h.client.add_blocked(conv, 5, pid, "implement")
    _run(pipeline.on_verdict(conv, 5))
    assert store.get(pid)["status"] == "awaiting_human"
    h.client.created.clear()
    handled = _run(pipeline.on_human_decision(555, 666, "procedi"))
    assert handled is True
    rec = store.get(pid)
    assert rec["status"] == "running"
    assert store.get_blocked(pid) is None
    assert rec["stage_index"] == 0                    # same stage, re-dispatched
    assert h.client.created[0][0] == ["demo-dev"]      # new conversation created


def test_human_decision_no_aborts(tmp_path):
    h = _setup(tmp_path)
    stages = [ps.PipelineStageDecl("implement", "implement", "demo-dev")]
    pid = _run(pipeline.start_pipeline("acme", "demo", "do it", stages=stages))
    conv = store.get(pid)["conv"]["implement"]
    h.client.add_blocked(conv, 5, pid, "implement")
    _run(pipeline.on_verdict(conv, 5))
    handled = _run(pipeline.on_human_decision(555, 666, "no"))
    assert handled is True
    assert store.get(pid)["status"] == "aborted"
    assert store.get_blocked(pid) is None


def test_human_decision_ambiguous_reasks(tmp_path):
    h = _setup(tmp_path)
    stages = [ps.PipelineStageDecl("implement", "implement", "demo-dev")]
    pid = _run(pipeline.start_pipeline("acme", "demo", "do it", stages=stages))
    conv = store.get(pid)["conv"]["implement"]
    h.client.add_blocked(conv, 5, pid, "implement")
    _run(pipeline.on_verdict(conv, 5))
    handled = _run(pipeline.on_human_decision(555, 666, "boh"))
    assert handled is True
    assert store.get(pid)["status"] == "awaiting_human"
    assert any("Non ho capito" in t[2] for t in h.threads)


def test_human_decision_unknown_topic_falls_through(tmp_path):
    _setup(tmp_path)
    assert _run(pipeline.on_human_decision(12345, 67890, "procedi")) is False


def test_human_decision_malformed_ids_returns_false(tmp_path):
    _setup(tmp_path)
    assert _run(pipeline.on_human_decision("x", None, "procedi")) is False


def test_topic_to_pipeline_reverse_lookup(tmp_path):
    h = _setup(tmp_path)
    stages = [ps.PipelineStageDecl("implement", "implement", "demo-dev")]
    pid = _run(pipeline.start_pipeline("acme", "demo", "do it", stages=stages))
    assert store.topic_to_pipeline(555, 666) == pid
    assert store.topic_to_pipeline(1, 2) is None


def test_resume_pending_resurfaces_blocked_question(tmp_path):
    h = _setup(tmp_path)
    stages = [ps.PipelineStageDecl("implement", "implement", "demo-dev")]
    pid = _run(pipeline.start_pipeline("acme", "demo", "do it", stages=stages))
    conv = store.get(pid)["conv"]["implement"]
    h.client.add_blocked(conv, 5, pid, "implement", decision_needed="Autorizzi?")
    _run(pipeline.on_verdict(conv, 5))
    h.client.created.clear()
    h.threads.clear()
    _run(pipeline.resume_pending())
    assert h.client.created == []                     # never re-dispatched the stage
    assert any("BLOCCATO" in t[2] for t in h.threads)  # question re-surfaced
