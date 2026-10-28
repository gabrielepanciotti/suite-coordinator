# restructure.py
"""Per-node agents-restructuring coordinator (Wave B, 2nd network test).

Single-phase clone of migrate.py: fan out one `agents_restructure` command to
every target node and await a typed `restructure_verdict` from each. Each node is
independent (no cross-host barrier) — the woken persona re-runs its own suite
conform, reconciles domain agents, registers the device-aware persona on the bus,
and replies. A verdict is accepted only if ok AND its reported roster is coherent
(verify_manifest). Any failure -> partial + needs_human escalation (nothing is
rolled back; restructuring is idempotent — re-running conform converges).
Telegram-free (I/O injected via `_M`, like migrate.py / pipeline.py).
"""
from __future__ import annotations

import logging

import escalation
import restructure_protocol as rp
import restructure_store as store

logger = logging.getLogger("coordinator.restructure")

_M: dict = {"client": None, "open_topic": None, "send_thread": None,
            "bot": None, "config": None}


def configure(*, client, open_topic, send_thread, bot, config) -> None:
    _M.update(client=client, open_topic=open_topic, send_thread=send_thread,
              bot=bot, config=config)


def _topic_title(rid: int) -> str:
    return f"\U0001f9ec Restructure #{rid} · agents canonici"


async def _notify(rec: dict, text: str) -> None:
    chat_id, topic_id = rec.get("chat_id"), rec.get("topic_id")
    if chat_id is None or topic_id is None:
        return
    try:
        await _M["send_thread"](_M["bot"], chat_id, topic_id, text)
    except Exception:
        logger.exception("restructure #%s: notify failed", rec["id"])


def _body(rid: int, agent: str, persona_archetype: str, authorized: bool = False) -> str:
    prefix = "✅ the owner ha AUTORIZZATO: procedi pure.\n" if authorized else ""
    return (
        prefix +
        f"Restructure #{rid} — porta il TUO .claude/agents/ al layout canonico.\n"
        f"1) Ri-esegui il conform della suite (rigenera gli archetipi e "
        f"materializza la persona device-aware dall'archetipo '{persona_archetype}').\n"
        f"2) Riconcilia gli agenti di dominio dichiarati; NON cancellare nulla: "
        f"riporta i file ad-hoc/legacy in 'removed_candidates'.\n"
        f"3) Registra la persona sul bus (roster).\n"
        f"4) Rispondi in QUESTA conversazione con la skill /hub-send includendo "
        f"context tipato:\n"
        f'{{"restructure_verdict": {{"id": {rid}, "agent": "{agent}", "ok": true, '
        f'"persona_name": "<nome-persona>", "agents": ["..."], '
        f'"removed_candidates": ["..."], "summary": "..."}}}}\n'
        f"ok=false se fallisce. Se serve una DECISIONE umana (es. cancellare "
        f"legacy), rispondi con blocked:true e decision_needed. "
        f"Non aprire altre conversazioni."
    )


async def start_restructure(tenant: str, *, targets: list[dict],
                            persona_archetype: str = "node-persona") -> int:
    rid = store.next_id()
    store.create(rid, tenant=tenant,
                 targets=[{"agent": t["agent"], "host": t.get("host", t["agent"])}
                          for t in targets],
                 persona_archetype=persona_archetype)
    try:
        chat_id, topic_id = await _M["open_topic"](rid, _topic_title(rid))
        store.set_thread(rid, chat_id, topic_id)
    except Exception:
        logger.exception("restructure #%s: topic creation failed", rid)
    await _dispatch(rid)
    return rid


async def _dispatch(rid: int, *, authorized: bool = False) -> None:
    rec = store.get(rid)
    if rec is None:
        return
    store.set_status(rid, "running")
    done = store.results(rid)
    tenant = rec["tenant"]
    persona_archetype = rec.get("persona_archetype", "node-persona")
    client = _M["client"](tenant, _M["config"].telegram_agent)
    dispatched = 0
    for t in rec["targets"]:
        agent = t["agent"]
        if agent in done:
            continue
        ctx = rp.build_command(rid, persona_archetype)
        body = _body(rid, agent, persona_archetype, authorized)
        conv = await client.create_conversation(
            to=[agent], subject=f"Restructure #{rid} · {agent}"[:120],
            body=body, context=ctx)
        store.set_conv(rid, agent, int(conv["id"]))
        dispatched += 1
    if dispatched:
        await _notify(rec, f"⚙️ Restructure #{rid}: comando su {dispatched} nodo/i.")


async def on_verdict(conv_id: int, message_id: int) -> bool:
    route = store.conv_to_restructure(conv_id)
    if route is None:
        return False
    rid, agent = route
    rec = store.get(rid)
    if rec is None or rec["status"] in ("done", "aborted", "partial"):
        return True
    tenant = rec["tenant"]
    client = _M["client"](tenant, _M["config"].telegram_agent)
    try:
        conv = await client.get_conversation(conv_id)
    except Exception:
        logger.warning("restructure #%s: verdict fetch failed for conv %s", rid, conv_id)
        return True
    if not store.mark_processed(rid, int(message_id)):
        return True
    msg = next((m for m in conv.get("messages", [])
                if int(m.get("id", 0)) == int(message_id)), None)
    verdict = rp.parse_verdict((msg or {}).get("context"), rid, agent)
    if verdict is None:
        verdict = {"ok": False, "blocked": False, "persona_name": "", "agents": [],
                   "removed_candidates": [], "summary": "nessun verdetto tipato",
                   "agent": agent}
    if verdict.get("blocked"):
        await _on_blocked(rid, rec, agent, verdict)
        return True
    # Inline manifest verification (design §7): an ok verdict with an incoherent
    # roster is downgraded to a failure so the barrier escalates it.
    if verdict.get("ok") and not rp.verify_manifest(verdict):
        verdict = {**verdict, "ok": False,
                   "summary": "manifest incoerente: persona non nel roster"}
    store.record_verdict(rid, agent, verdict)
    results = store.results(rid)
    if len(results) < len(rec["targets"]):
        return True
    await _evaluate(rid, results)
    return True


async def _evaluate(rid: int, results: dict) -> None:
    rec = store.get(rid)
    failed = [a for a, v in results.items() if not v.get("ok")]
    if not failed:
        store.set_status(rid, "done")
        await _notify(rec, f"✅ Restructure #{rid} completato su tutti i nodi.")
        return
    store.set_status(rid, "partial")
    ok = [a for a, v in results.items() if v.get("ok")]
    text = (f"Restructure #{rid}: PARZIALE. Ok: {', '.join(ok) or '—'}. "
            f"Falliti: {', '.join(failed)}. Serve verifica umana (nessun rollback: "
            f"ri-eseguire il conform converge).")
    await _escalate(rec, text)


async def _on_blocked(rid: int, rec: dict, agent: str, verdict: dict) -> None:
    """A node needs a human decision (e.g. legacy-agent classification). Pause as
    awaiting_human and surface the question. The blocked verdict is NOT recorded
    as a result — the barrier does not advance."""
    store.set_blocked(rid, agent, verdict.get("summary", ""),
                      verdict.get("decision_needed", ""))
    if rec.get("chat_id") is None or rec.get("topic_id") is None:
        try:
            chat_id, topic_id = await _M["open_topic"](rid, _topic_title(rid))
            store.set_thread(rid, chat_id, topic_id)
        except Exception:
            logger.exception("restructure #%s: topic creation failed (blocked)", rid)
        rec = store.get(rid) or rec
    text = (
        f"🔔 Restructure #{rid}: {agent} è BLOCCATO e serve la tua decisione.\n"
        f"{verdict.get('summary', '')}\n"
        f"👉 {verdict.get('decision_needed') or 'Procedi?'} "
        f"(rispondi 'procedi' per autorizzare o 'no' per annullare)")
    if rec.get("chat_id") is None or rec.get("topic_id") is None:
        await _escalate(rec, text)
    else:
        await _notify(rec, text)


async def on_human_decision(chat_id, topic_id, text) -> bool:
    try:
        chat_key, topic_key = int(chat_id), int(topic_id)
    except (TypeError, ValueError):
        return False
    rid = store.topic_to_restructure(chat_key, topic_key)
    if rid is None:
        return False
    rec = store.get(rid)
    if rec is None or rec.get("status") != "awaiting_human":
        return False
    blocked = store.get_blocked(rid) or {}
    decision = escalation.interpret_decision(text)
    if decision == "approve":
        store.clear_blocked(rid)
        await _dispatch(rid, authorized=True)
        rec = store.get(rid) or rec
        await _notify(rec, f"✅ Autorizzato: rilancio {blocked.get('agent', 'il nodo')}.")
        return True
    if decision == "deny":
        store.clear_blocked(rid)
        store.set_status(rid, "aborted")
        rec = store.get(rid) or rec
        await _notify(rec, f"❌ Negato: restructure #{rid} annullato.")
        return True
    await _notify(rec, "Non ho capito: rispondi 'procedi' per autorizzare "
                       "oppure 'no' per annullare.")
    return True


async def _escalate(rec: dict, text: str) -> None:
    try:
        client = _M["client"](rec["tenant"], _M["config"].telegram_agent)
        conv = await client.create_conversation(
            to=[_M["config"].telegram_agent],
            subject="Restructure — serve un umano"[:120], body=text)
        await client.send_message(int(conv["id"]), text, needs_human=True)
    except Exception:
        logger.exception("restructure escalate failed")
    await _notify(rec, "\U0001f514 " + text)


async def resume_pending() -> None:
    """Re-dispatch to targets lacking a verdict (idempotent: re-running a node's
    conform converges)."""
    for rec in store.list_resumable():
        rid = int(rec["id"])
        try:
            await _dispatch(rid)
        except Exception:
            logger.exception("restructure #%s: resume failed", rid)
