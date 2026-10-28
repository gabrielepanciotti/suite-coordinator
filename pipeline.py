# pipeline.py
"""Pipeline orchestrator: a sequential state machine over bus stages.

Telegram-free (like hub.py): all bus/Telegram I/O is injected via `_P` so the
logic unit-tests with fakes. Each non-gate stage dispatches a typed `pipeline`
command to its agent and awaits a typed `pipeline_verdict` reply, routed here by
hub.py via on_verdict(). The gate stage posts inline buttons (Task 5).

State lives in pipeline_store (atomic JSON) so resume_pending() (Task 5) can pick
up in-flight pipelines after a Coordinator restart.
"""
from __future__ import annotations

import logging

import escalation
import pipeline_stages as ps
import pipeline_store as store

logger = logging.getLogger("coordinator.pipeline")

_P: dict = {
    "client": None,       # (tenant, agent) -> HubClient-like
    "open_topic": None,   # async (pid, title) -> (chat_id, topic_id)
    "send_thread": None,  # async (bot, chat_id, topic_id, text) -> None
    "post_gate": None,    # async (pid, chat_id, topic_id, text) -> None
    "bot": None,
    "config": None,
}


def configure(*, client, open_topic, send_thread, post_gate, bot, config) -> None:
    _P.update(client=client, open_topic=open_topic, send_thread=send_thread,
              post_gate=post_gate, bot=bot, config=config)


def _stages(rec: dict) -> list[ps.PipelineStageDecl]:
    return [ps.PipelineStageDecl.from_dict(s) for s in rec["stages"]]


def stage_name_is_test(rec: dict, stage_name: str) -> bool:
    for s in _stages(rec):
        if s.name == stage_name:
            return s.type == "test"
    return False


async def _notify(rec: dict, text: str) -> None:
    """Post a plain-text line to the pipeline's Telegram topic (best-effort)."""
    chat_id, topic_id = rec.get("chat_id"), rec.get("topic_id")
    if chat_id is None or topic_id is None:
        return
    try:
        await _P["send_thread"](_P["bot"], chat_id, topic_id, text)
    except Exception:
        logger.exception("pipeline #%s: notify failed", rec["id"])


async def start_pipeline(tenant: str, project_key: str, task: str, *, stages) -> int:
    """Create a pipeline, open its Telegram topic, dispatch the first stage."""
    pid = store.next_id()
    store.create(pid, tenant=tenant, project=project_key, task=task,
                 stages=[ps.to_dict(s) for s in stages])
    title = f"\U0001f6e0 Pipeline #{pid} · {project_key}"
    try:
        chat_id, topic_id = await _P["open_topic"](pid, title)
        store.set_thread(pid, chat_id, topic_id)
    except Exception:
        logger.exception("pipeline #%s: topic creation failed", pid)
    await _dispatch_stage(pid)
    return pid


def _stage_body(stage: ps.PipelineStageDecl, task: str, pid: int) -> str:
    return (
        f"Pipeline #{pid} — stage '{stage.name}' (tipo {stage.type}).\n"
        f"Task: {task}\n\n"
        f"Esegui SOLO questo stage. Quando hai finito, rispondi in QUESTA "
        f"conversazione con la skill /hub-send includendo context tipato:\n"
        f'{{"pipeline_verdict": {{"id": {pid}, "stage": "{stage.name}", '
        f'"ok": true|false, "summary": "...", "sha": "<git sha se rilevante>"}}}}\n'
        f"ok=false se lo stage fallisce. Non aprire altre conversazioni."
    )


async def _dispatch_stage(pid: int) -> None:
    rec = store.get(pid)
    if rec is None:
        return
    stages = _stages(rec)
    idx = rec["stage_index"]
    if idx >= len(stages):
        return await _finish(pid)
    stage = stages[idx]
    if stage.type == "gate":
        return await _post_gate_stage(pid, rec, stage)   # Task 5
    tenant = rec["tenant"]
    ctx = ps.build_command(pid, stage, rec["task"])
    body = _stage_body(stage, rec["task"], pid)
    client = _P["client"](tenant, _P["config"].telegram_agent)
    conv = await client.create_conversation(
        to=[stage.agent], subject=f"Pipeline #{pid} · {stage.name}"[:120],
        body=body, context=ctx,
    )
    store.set_conv(pid, stage.name, int(conv["id"]))
    store.set_status(pid, "running")


async def on_verdict(conv_id: int, message_id: int) -> bool:
    """Route an incoming bus message to its pipeline stage. Returns True if it
    belonged to a pipeline (handled), False otherwise (let normal routing run)."""
    route = store.conv_to_pipeline(conv_id)
    if route is None:
        return False
    pid, stage_name = route
    rec = store.get(pid)
    if rec is None or rec["status"] in ("done", "aborted", "failed"):
        return True
    tenant = rec["tenant"]
    client = _P["client"](tenant, _P["config"].telegram_agent)
    try:
        conv = await client.get_conversation(conv_id)
    except Exception:
        logger.warning("pipeline #%s: verdict fetch failed for conv %s", pid, conv_id)
        return True                       # NOT marked -> redelivery retries the fetch
    # Dedup only once we hold the data to act on. Single-consumer _listen loop
    # (like the deployer) -> no race; never lose a verdict to a transient fetch error.
    if not store.mark_processed(pid, int(message_id)):
        return True
    msg = next((m for m in conv.get("messages", [])
                if int(m.get("id", 0)) == int(message_id)), None)
    verdict = ps.parse_verdict((msg or {}).get("context"), pid, stage_name)
    if verdict is None:
        verdict = {"ok": False, "blocked": False,
                   "summary": "nessun verdetto tipato", "sha": None}
    if verdict.get("blocked"):
        await _on_blocked(pid, rec, stage_name, verdict)
        return True
    store.set_stage_result(pid, stage_name, verdict)
    if verdict["ok"]:
        if stage_name_is_test(rec, stage_name) and verdict.get("sha"):
            store.set_sha_before(pid, verdict["sha"])
        await _advance(pid)
    else:
        await _handle_failure(pid, stage_name, verdict["summary"])   # Task 5
    return True


async def _on_blocked(pid: int, rec: dict, stage_name: str, verdict: dict) -> None:
    """A stage agent is blocked on a human decision: pause the pipeline as
    awaiting_human and surface the question in the pipeline topic. The
    blocked verdict is NOT recorded as a stage result (the pipeline does not
    advance) — the stage is paused, not failed."""
    store.set_blocked(pid, stage_name, verdict.get("summary", ""),
                      verdict.get("decision_needed", ""))
    # Ensure a bindable topic exists so the human's reply can be routed back.
    if rec.get("chat_id") is None or rec.get("topic_id") is None:
        title = f"\U0001f6e0 Pipeline #{pid} · {rec['project']}"
        try:
            chat_id, topic_id = await _P["open_topic"](pid, title)
            store.set_thread(pid, chat_id, topic_id)
        except Exception:
            logger.exception("pipeline #%s: topic creation failed (blocked)", pid)
        rec = store.get(pid) or rec
    text = (
        f"\U0001f514 Pipeline #{pid}: stage '{stage_name}' è BLOCCATO e serve "
        f"la tua decisione.\n{verdict.get('summary', '')}\n"
        f"👉 {verdict.get('decision_needed') or 'Procedi?'} "
        f"(rispondi 'procedi' per autorizzare o 'no' per annullare)")
    # Double topic-creation failure (start + here, both non-retryable): without
    # a bindable topic _notify no-ops silently and the pipeline strands as
    # awaiting_human, unseen and unanswerable (#13). Fall back to the
    # escalation conversation (its topic is created hub-side with flood retry)
    # so the block is at least surfaced to a human.
    if rec.get("chat_id") is None or rec.get("topic_id") is None:
        await _escalate(rec, text)
    else:
        await _notify(rec, text)


async def on_human_decision(chat_id, topic_id, text) -> bool:
    # Coercion guard: this seam is fed by raw Telegram topic-reply input
    # (hub.py). Malformed ids degrade gracefully (return False -> other
    # handlers still run) instead of raising into the hub dispatch loop.
    try:
        chat_key, topic_key = int(chat_id), int(topic_id)
    except (TypeError, ValueError):
        return False
    pid = store.topic_to_pipeline(chat_key, topic_key)
    if pid is None:
        return False
    rec = store.get(pid)
    if rec is None or rec.get("status") != "awaiting_human":
        return False            # nothing pending here -> let other handlers run
    blocked = store.get_blocked(pid) or {}
    stage_name = blocked.get("stage", "")
    decision = escalation.interpret_decision(text)
    if decision == "approve":
        store.clear_blocked(pid)                      # status -> running
        # Re-dispatch the SAME stage (stage_index is not advanced): the
        # blocked stage never produced a usable verdict, so it must be
        # re-run now that a human authorized it.
        await _dispatch_stage(pid)
        rec = store.get(pid) or rec   # notify against fresh state (status running)
        await _notify(rec, f"✅ Autorizzato: rilancio stage '{stage_name}'.")
        return True
    if decision == "deny":
        store.clear_blocked(pid)
        store.set_status(pid, "aborted")
        rec = store.get(pid) or rec   # notify against fresh state (status aborted)
        await _notify(rec, f"❌ Negato: pipeline #{pid} annullata.")
        return True
    # ambiguous — re-ask, do not guess
    await _notify(rec, "Non ho capito: rispondi 'procedi' o 'no'.")
    return True


async def _advance(pid: int) -> None:
    rec = store.get(pid)
    if rec is None:
        return
    store.set_stage_index(pid, rec["stage_index"] + 1)
    await _dispatch_stage(pid)


async def _finish(pid: int) -> None:
    store.set_status(pid, "done")
    rec = store.get(pid)
    summary = ", ".join(f"{k}:{'✅' if v.get('ok') else '❌'}"
                        for k, v in (rec.get("results") or {}).items())
    await _notify(rec, f"✅ Pipeline #{pid} completata. {summary}")


# --- gate, failure handling, rollback, resume (Task 5) ----------------------
async def _post_gate_stage(pid: int, rec: dict, stage) -> None:
    store.set_status(pid, "awaiting_gate")
    results = rec.get("results") or {}
    summary = ", ".join(f"{k}:{'✅' if v.get('ok') else '❌'}"
                        for k, v in results.items()) or "—"
    text = (f"\U0001f6c2 Pipeline #{pid} — GATE\n"
            f"Progetto: {rec['project']} · {summary}\n"
            f"Approvi il deploy in produzione?")
    chat_id, topic_id = rec.get("chat_id"), rec.get("topic_id")
    if chat_id is None or topic_id is None:
        logger.warning("pipeline #%s: gate has no Telegram thread", pid)
        return
    await _P["post_gate"](pid, chat_id, topic_id, text)


async def on_gate_decision(pid: int, decision: str) -> None:
    """Called by the bot gate callback. 'ok' advances past the gate; 'no' aborts.
    Idempotent: a second decision after the gate is resolved is a no-op."""
    rec = store.get(pid)
    if rec is None or rec["status"] != "awaiting_gate":
        return
    store.set_gate(pid, decision)
    if decision == "ok":
        store.set_status(pid, "running")
        await _advance(pid)
    else:
        store.set_status(pid, "aborted")
        await _notify(rec, f"❌ Pipeline #{pid} rifiutata al gate. Nessun deploy.")


async def _handle_failure(pid: int, stage_name: str, summary: str) -> None:
    rec = store.get(pid)
    if rec is None:
        return
    stype = next((s.type for s in _stages(rec) if s.name == stage_name), "")
    if stype in ps.ROLLBACK_STAGES:
        return await _rollback(pid, rec, stage_name, summary)
    store.set_status(pid, "aborted")
    await _notify(rec, f"❌ Pipeline #{pid} interrotta a '{stage_name}': {summary}")


async def _rollback(pid: int, rec: dict, stage_name: str, summary: str) -> None:
    sha = rec.get("sha_before")
    deploy_agent = next((s.agent for s in _stages(rec) if s.type == "deploy"), "")
    if not sha or not deploy_agent:
        store.set_status(pid, "failed")
        await _escalate(rec, f"Pipeline #{pid}: '{stage_name}' fallito ({summary}) "
                             f"e rollback impossibile (sha/agent ignoto). Serve un umano.")
        return
    tenant = rec["tenant"]
    rb_stage = ps.PipelineStageDecl("rollback", "deploy", deploy_agent,
                                    {"rollback": True, "to_sha": sha})
    ctx = ps.build_command(pid, rb_stage, rec["task"])
    body = _stage_body(rb_stage, rec["task"], pid)
    client = _P["client"](tenant, _P["config"].telegram_agent)
    conv = await client.create_conversation(
        to=[deploy_agent], subject=f"Pipeline #{pid} · rollback"[:120],
        body=body, context=ctx,
    )
    store.set_conv(pid, "rollback", int(conv["id"]))
    store.set_status(pid, "failed")
    await _notify(rec, f"⚠️ Pipeline #{pid}: '{stage_name}' fallito — rollback a {sha} "
                       f"dispatchato a {deploy_agent}.")


async def _escalate(rec: dict, text: str) -> None:
    """Post a needs_human message on a fresh bus conversation to the human face."""
    try:
        client = _P["client"](rec["tenant"], _P["config"].telegram_agent)
        conv = await client.create_conversation(
            to=[_P["config"].telegram_agent], subject="Pipeline — serve un umano"[:120],
            body=text,
        )
        await client.send_message(int(conv["id"]), text, needs_human=True)
    except Exception:
        logger.exception("pipeline escalate failed")
    await _notify(rec, "🔔 " + text)


async def resume_pending() -> None:
    """On boot, resume in-flight pipelines from the store.

    - running with a dispatched current stage but no recorded result -> re-dispatch
      (the typed command is idempotent: implement/test re-run, deploy re-pulls ff-only).
    - awaiting_gate -> re-post the gate buttons (the prior inline message is stale).
    - awaiting_human -> re-surface the blocked question (never re-dispatch the
      stage: it's paused on a human decision, not lost)."""
    for rec in store.list_pending():
        pid = int(rec["id"])
        try:
            if rec["status"] == "awaiting_gate":
                stage = _stages(rec)[rec["stage_index"]]
                await _post_gate_stage(pid, rec, stage)
            elif rec["status"] == "awaiting_human":
                blocked = store.get_blocked(pid)
                if blocked is None:
                    continue
                text = (
                    f"\U0001f514 Pipeline #{pid}: stage '{blocked.get('stage', '')}' "
                    f"è BLOCCATO e serve la tua decisione.\n{blocked.get('reason', '')}\n"
                    f"👉 {blocked.get('decision_needed') or 'Procedi?'} "
                    f"(rispondi 'procedi' per autorizzare o 'no' per annullare)")
                await _notify(rec, text)
            else:
                stages = _stages(rec)
                idx = rec["stage_index"]
                if idx < len(stages) and stages[idx].name not in rec.get("results", {}):
                    await _dispatch_stage(pid)
        except Exception:
            logger.exception("pipeline #%s: resume failed", pid)
