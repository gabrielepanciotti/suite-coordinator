# migrate.py
"""Cross-host migration coordinator: parallel fan-out per phase + all-or-nothing
barrier before commit. Telegram-free (I/O injected via `_M`, like pipeline.py).

Each phase dispatches a typed `migrate` command to every db-owner target and
awaits a typed `migrate_verdict` from each (routed here by hub.py via on_verdict).
A barrier holds the phase until every target answered:
  prepare/verify: any failure -> abort-all (nothing committed).
  commit:         any failure -> partial + needs_human (already-committed stay).
State persists in migrate_store; resume_pending re-dispatches the current phase
to targets without a recorded verdict.
"""
from __future__ import annotations

import logging

import escalation
import migrate_protocol as mp
import migrate_store as store

logger = logging.getLogger("coordinator.migrate")

_M: dict = {"client": None, "open_topic": None, "send_thread": None,
            "bot": None, "config": None}


def configure(*, client, open_topic, send_thread, bot, config) -> None:
    _M.update(client=client, open_topic=open_topic, send_thread=send_thread,
              bot=bot, config=config)


def _topic_title(mid: int, project: str, rev: str) -> str:
    return f"\U0001f5c3 Migrazione #{mid} · {project} ({rev})"


async def _notify(rec: dict, text: str) -> None:
    chat_id, topic_id = rec.get("chat_id"), rec.get("topic_id")
    if chat_id is None or topic_id is None:
        return
    try:
        await _M["send_thread"](_M["bot"], chat_id, topic_id, text)
    except Exception:
        logger.exception("migration #%s: notify failed", rec["id"])


def _phase_body(phase: str, rev: str, mid: int, agent: str, authorized: bool = False) -> str:
    prefix = "✅ the owner ha AUTORIZZATO questa fase: procedi pure.\n" if authorized else ""
    return (
        prefix +
        f"Migrazione #{mid} — fase '{phase}' (rev {rev}).\n"
        f"Esegui SOLO questa fase sul TUO database e rispondi in QUESTA "
        f"conversazione con la skill /hub-send includendo context tipato:\n"
        f'{{"migrate_verdict": {{"id": {mid}, "phase": "{phase}", '
        f'"agent": "{agent}", "ok": true|false, "summary": "..."}}}}\n'
        f"prepare=dry-run (alembic --sql/check); verify=revisione base attesa; "
        f"commit=alembic upgrade {rev}. ok=false se fallisce. Non aprire altre conversazioni."
    )


async def start_migration(tenant: str, project_key: str, rev: str, *, targets) -> int:
    mid = store.next_id()
    store.create(mid, tenant=tenant, project=project_key, rev=rev,
                 targets=[{"agent": t.agent, "host": t.host} for t in targets])
    title = _topic_title(mid, project_key, rev)
    try:
        chat_id, topic_id = await _M["open_topic"](mid, title)
        store.set_thread(mid, chat_id, topic_id)
    except Exception:
        logger.exception("migration #%s: topic creation failed", mid)
    await _dispatch_phase(mid, "prepare")
    return mid


async def _dispatch_phase(mid: int, phase: str, *, authorized: bool = False) -> None:
    rec = store.get(mid)
    if rec is None:
        return
    store.set_phase(mid, phase)
    store.set_status(mid, "running")
    done = store.phase_results(mid, phase)
    tenant = rec["tenant"]
    client = _M["client"](tenant, _M["config"].telegram_agent)
    dispatched = 0
    for t in rec["targets"]:
        agent = t["agent"]
        if agent in done:
            continue
        args = {"authorized": True} if authorized else None
        ctx = mp.build_command(mid, phase, rec["rev"], args)
        body = _phase_body(phase, rec["rev"], mid, agent, authorized)
        conv = await client.create_conversation(
            to=[agent], subject=f"Migrazione #{mid} · {phase}"[:120],
            body=body, context=ctx)
        # One live conv per agent: set_conv drops the agent's prior-phase entries
        # so conv_to_migration / parse_verdict resolve verdicts to THIS phase only.
        store.set_conv(mid, agent, phase, int(conv["id"]))
        dispatched += 1
    if dispatched:
        await _notify(rec, f"⚙️ Migrazione #{mid}: fase {phase} su "
                           f"{dispatched} target.")


async def on_verdict(conv_id: int, message_id: int) -> bool:
    route = store.conv_to_migration(conv_id)
    if route is None:
        return False
    mid, agent, phase = route
    rec = store.get(mid)
    if rec is None or rec["status"] in ("done", "aborted", "partial"):
        return True
    tenant = rec["tenant"]
    client = _M["client"](tenant, _M["config"].telegram_agent)
    try:
        conv = await client.get_conversation(conv_id)
    except Exception:
        logger.warning("migration #%s: verdict fetch failed for conv %s", mid, conv_id)
        return True
    if not store.mark_processed(mid, int(message_id)):
        return True
    msg = next((m for m in conv.get("messages", [])
                if int(m.get("id", 0)) == int(message_id)), None)
    verdict = mp.parse_verdict((msg or {}).get("context"), mid, phase, agent)
    if verdict is None:
        verdict = {"ok": False, "blocked": False,
                   "summary": "nessun verdetto tipato", "agent": agent}
    if verdict.get("blocked"):
        await _on_blocked(mid, rec, phase, agent, verdict)
        return True
    store.record_verdict(mid, phase, agent, verdict)
    results = store.phase_results(mid, phase)
    if len(results) < len(rec["targets"]):
        return True
    await _evaluate_barrier(mid, phase, results)
    return True


async def _on_blocked(mid: int, rec: dict, phase: str, agent: str, verdict: dict) -> None:
    """A target is blocked on a human decision: pause the migration as
    awaiting_human and surface the question in the migration topic. The
    blocked verdict is NOT recorded as a phase result (the barrier does not
    advance) — the phase is paused, not failed."""
    store.set_blocked(mid, phase, agent, verdict.get("summary", ""),
                      verdict.get("decision_needed", ""))
    # Ensure a bindable topic exists so the human's reply can be routed back.
    if rec.get("chat_id") is None or rec.get("topic_id") is None:
        title = _topic_title(mid, rec["project"], rec["rev"])
        try:
            chat_id, topic_id = await _M["open_topic"](mid, title)
            store.set_thread(mid, chat_id, topic_id)
        except Exception:
            logger.exception("migration #%s: topic creation failed (blocked)", mid)
        rec = store.get(mid) or rec
    text = (
        f"🔔 Migrazione #{mid}: {agent} è BLOCCATO sulla fase '{phase}' e serve "
        f"la tua decisione.\n{verdict.get('summary', '')}\n"
        f"👉 {verdict.get('decision_needed') or 'Procedi?'} "
        f"(rispondi 'procedi' per autorizzare o 'no' per annullare)")
    # Double topic-creation failure (start_migration + here, both non-retryable):
    # without a bindable topic _notify no-ops silently and the migration strands
    # as awaiting_human, unseen and unanswerable (#13). Fall back to the
    # escalation conversation (its topic is created hub-side with flood retry) so
    # the block is at least surfaced to a human. Auto-binding the 'procedi'/'no'
    # reply still needs the migration topic; a human reached this way acts manually.
    if rec.get("chat_id") is None or rec.get("topic_id") is None:
        await _escalate(rec, text)
    else:
        await _notify(rec, text)


async def on_human_decision(chat_id, topic_id, text) -> bool:
    # Coercion guard: this seam is fed by raw Telegram topic-reply input
    # (hub.py). Malformed ids degrade gracefully (return False -> other handlers
    # still run) instead of raising into the hub dispatch loop.
    try:
        chat_key, topic_key = int(chat_id), int(topic_id)
    except (TypeError, ValueError):
        return False
    mid = store.topic_to_migration(chat_key, topic_key)
    if mid is None:
        return False
    rec = store.get(mid)
    if rec is None or rec.get("status") != "awaiting_human":
        return False            # nothing pending here -> let other handlers run
    blocked = store.get_blocked(mid) or {}
    decision = escalation.interpret_decision(text)
    if decision == "approve":
        store.clear_blocked(mid)                      # status -> running
        # NOTE: authorization is broadcast to ALL still-pending targets of this
        # phase, not only the blocked agent. Safe because prepare/verify are
        # read-only and commit is idempotent (alembic upgrade no-ops if already
        # applied). Revisit (scope to the blocked agent) if a write-and-non-
        # idempotent phase is ever added.
        await _dispatch_phase(mid, blocked.get("phase", rec["phase"]), authorized=True)
        rec = store.get(mid) or rec   # notify against fresh state (status running)
        await _notify(rec, f"✅ Autorizzato: rilancio {blocked.get('agent', 'il target')} "
                           f"sulla fase {blocked.get('phase', rec['phase'])}.")
        return True
    if decision == "deny":
        store.clear_blocked(mid)
        store.set_status(mid, "aborted")
        rec = store.get(mid) or rec   # notify against fresh state (status aborted)
        await _notify(rec, f"❌ Negato: migrazione #{mid} annullata. "
                           f"Nessuna azione eseguita dal target bloccato.")
        return True
    # ambiguous — re-ask, do not guess
    await _notify(rec, "Non ho capito: rispondi 'procedi' per autorizzare "
                       "oppure 'no' per annullare.")
    return True


async def _evaluate_barrier(mid: int, phase: str, results: dict) -> None:
    rec = store.get(mid)
    failed = [a for a, v in results.items() if not v.get("ok")]
    if not failed:
        idx = mp.PHASES.index(phase)
        if idx + 1 < len(mp.PHASES):
            await _dispatch_phase(mid, mp.PHASES[idx + 1])
        else:
            store.set_status(mid, "done")
            await _notify(rec, f"✅ Migrazione #{mid} completata su tutti i target.")
        return
    if phase == "commit":
        await _escalate_partial(mid, rec, results, failed)
    else:
        store.set_status(mid, "aborted")
        await _notify(rec, f"❌ Migrazione #{mid}: fase {phase} fallita su "
                           f"{', '.join(failed)}. Nessun commit eseguito.")


async def _escalate_partial(mid: int, rec: dict, results: dict, failed: list) -> None:
    store.set_status(mid, "partial")
    ok = [a for a, v in results.items() if v.get("ok")]
    text = (f"Migrazione #{mid}: COMMIT PARZIALE. Committati: {', '.join(ok) or '—'}. "
            f"Falliti: {', '.join(failed)}. Serve riconciliazione umana (no rollback automatico).")
    await _escalate(rec, text)


async def _escalate(rec: dict, text: str) -> None:
    try:
        client = _M["client"](rec["tenant"], _M["config"].telegram_agent)
        conv = await client.create_conversation(
            to=[_M["config"].telegram_agent],
            subject="Migrazione — serve un umano"[:120], body=text)
        await client.send_message(int(conv["id"]), text, needs_human=True)
    except Exception:
        logger.exception("migration escalate failed")
    await _notify(rec, "\U0001f514 " + text)


async def resume_pending() -> None:
    """Re-dispatch the current phase to targets lacking a verdict (idempotent:
    prepare/verify read-only, commit re-runs alembic upgrade which no-ops if applied)."""
    for rec in store.list_resumable():
        mid = int(rec["id"])
        try:
            await _dispatch_phase(mid, rec["phase"])
        except Exception:
            logger.exception("migration #%s: resume failed", mid)
