"""butler_reorg — Slice A watcher: proactive schedule reorganization (pure + injection).

No scheduler, no network, no bot import (grafo aciclico: butler_reorg never
imports bot.py — run_claude/hw_client/send_reorg_card are all injected,
mirroring taskgen.py's run_watcher).
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import llm_usage
import wake_routing
from taskgen import Dedup, _find_balanced_braces

logger = logging.getLogger("coordinator.butler_reorg")

# Declared in <istanza>/config/llm-routing.json: model and fallback are the queue's.
SERVICE = "coordinator.riorganizza"


def build_reorg_seed(schedule: list[dict]) -> str:
    """Prompt asking the node to propose a reorganization of its schedule.

    Passes the schedule (name/category/orario) and asks for a JSON block of
    proposals (``accorpa``/``sposta``) — see spec §3.1.
    """
    lines = []
    for item in schedule or []:
        if not isinstance(item, dict):
            continue
        name = item.get("name", "?")
        category = item.get("category", "?")
        start_at = item.get("start_at", "?")
        st_id = item.get("id", "?")
        lines.append(f"- id={st_id} \"{name}\" categoria={category} orario={start_at}")
    schedule_text = "\n".join(lines) if lines else "(nessun task pianificato nella finestra)"

    return (
        "Sei il maggiordomo. Ecco lo schedule pianificato nella finestra corrente:\n\n"
        f"{schedule_text}\n\n"
        "Individua eventuali riorganizzazioni utili (accorpare task simili/vicini, "
        "spostare un task in un orario migliore). Rispondi SOLO con un blocco JSON, "
        "nessun altro testo, in questo schema:\n"
        '{"proposals": [{"kind": "accorpa" | "sposta", "st_ids": [1, 2], '
        '"new_start_iso": "2026-07-10T19:00:00+02:00", "rationale": "..."}]}\n\n'
        'Se non c\'è nulla da riorganizzare, rispondi con {"proposals": []}.'
    )


def _proposals_from_obj(obj: dict) -> list[dict] | None:
    if not isinstance(obj, dict) or "proposals" not in obj:
        return None
    items = obj.get("proposals")
    if not isinstance(items, list):
        return None
    out: list[dict] = []
    for it in items:
        if isinstance(it, dict):
            out.append(it)
    return out


def parse_reorg_proposals(text: str) -> list[dict]:
    """Extract the FIRST valid JSON object with key "proposals" from text.

    Never raises; returns [] on any failure or absence of a usable block
    (reuses the balanced-brace scan from taskgen._find_balanced_braces).
    """
    try:
        if not text:
            return []

        candidates = _find_balanced_braces(text)

        for cand in candidates:
            try:
                obj = json.loads(cand)
            except (json.JSONDecodeError, TypeError, ValueError):
                continue
            proposals = _proposals_from_obj(obj)
            if proposals is not None:
                return proposals

        return []
    except Exception:
        return []


def _dedup_probe(kind: str, st_ids: list, new_start_iso: str):
    """Build a SimpleNamespace shaped like a taskgen.Suggestion so we can reuse
    taskgen.Dedup.seen/record as-is: Dedup.key() hashes f"{title}|{when_iso}|{category}",
    so title=kind, when_iso=st_ids-joined, category=new_start_iso reproduces the
    exact ``sha1(kind|st_ids|new_start_iso)`` ordering required by the spec.
    """
    ids_part = ",".join(str(x) for x in sorted(st_ids))
    return SimpleNamespace(title=kind, when_iso=ids_part, category=new_start_iso)


async def run_reorg_watcher(
    watcher,
    *,
    run_claude,
    hw_client,
    dedup: "Dedup",
    resolve_workspace,
    send_reorg_card,
    telegram_chat_id: int,
    now: float,
    policy,
    trust=None,
    apply_reorg=None,
) -> dict:
    """Run one Slice A reorg watcher tick end-to-end.

    1. Fetch the schedule in [now, now+horizon) — tolerant (any exception → []);
       an empty schedule short-circuits: no LLM wake, counters stay at zero.
    2. Wake the node with `build_reorg_seed` via injected `run_claude`.
    3. Parse proposals (`parse_reorg_proposals`, never raises).
    4. Per proposal: dedup (skip if seen); then `policy.resolve("reorg_apply_mode", ...)`:
       - ``"propose"`` (default) sends the ``[Applica]/[Ignora]`` card;
       - ``"auto_apply_trusted"`` (#19) applies the reorganization itself via the
         injected ``apply_reorg`` — but ONLY when ``trust.decision(kind)`` is
         "trusted" AND the ``trust.daily_cap`` still has room; the card is then
         a posteriori (``proposal["applied"] = True`` → ``[Annulla]``);
       - any other mode degrades to propose.
       Each proposal is isolated in its own try/except (`failed++` on error, no
       dedup record so it retries next tick).

    ``apply_reorg(st_ids, new_start_iso, telegram_chat_id)`` is an injected async
    callable returning the list of st_ids that FAILED to reschedule (empty on
    full success). When it is ``None`` — or ``trust`` is ``None`` — the tick
    degrades to propose: no action on the person's schedule can happen without
    both an explicit trust policy and an applier.

    Returns counters: ``{"proposed": n, "applied": n, "skipped": n, "failed": n}``.
    """
    counters = {"proposed": 0, "applied": 0, "skipped": 0, "failed": 0}

    frm_dt = datetime.fromtimestamp(now, tz=timezone.utc)
    to_dt = frm_dt + timedelta(hours=watcher.horizon_hours)
    frm_iso = frm_dt.isoformat()
    to_iso = to_dt.isoformat()

    try:
        schedule = await hw_client.list_scheduled_tasks(
            telegram_chat_id=telegram_chat_id, frm=frm_iso, to=to_iso
        )
    except Exception as exc:
        logger.warning("butler_reorg watcher=%s list_scheduled_tasks failed: %s", watcher.key, exc)
        schedule = []

    if not schedule:
        return counters

    # Original start times, keyed by str(st_id): the undo of an auto-applied
    # reorganization needs them, and they must survive the JSON round-trip of
    # the bot state (JSON object keys are always strings).
    prev_starts: dict[str, str] = {}
    for item in schedule:
        if not isinstance(item, dict):
            continue
        st_id = item.get("id")
        start_at = item.get("start_at")
        if st_id is not None and start_at:
            prev_starts[str(st_id)] = start_at

    node_key = watcher.node_key or watcher.key
    seed = build_reorg_seed(schedule)
    run = await wake_routing.run_node_service(SERVICE, node_key, seed)
    if run is None:
        run = await run_claude(
            seed,
            workspace=resolve_workspace(node_key),
            persona=None,
            resume_id=None,
        )
    text, _sid = run
    llm_usage.record(run, node=node_key, caller="butler_reorg")
    proposals = parse_reorg_proposals(text)
    applied_today = 0

    for raw in proposals:
        try:
            kind = raw["kind"]
            st_ids = raw["st_ids"]
            new_start_iso = raw["new_start_iso"]
            rationale = raw.get("rationale")

            probe = _dedup_probe(kind, st_ids, new_start_iso)
            if dedup.seen(probe, now=now):
                counters["skipped"] += 1
                continue

            mode = policy.resolve("reorg_apply_mode", watcher.node_key)
            if mode not in ("propose", "auto_apply_trusted"):
                logger.info(
                    "butler_reorg watcher=%s mode=%r sconosciuta, degrado a propose",
                    watcher.key, mode,
                )
                mode = "propose"

            auto_apply = (
                mode == "auto_apply_trusted"
                and trust is not None
                and apply_reorg is not None
                and trust.decision(kind) == "trusted"
                and applied_today < trust.daily_cap
            )

            proposal = {
                "kind": kind,
                "st_ids": st_ids,
                "new_start_iso": new_start_iso,
                "rationale": rationale,
                "telegram_chat_id": telegram_chat_id,
                "prev_starts": dict(prev_starts),
            }

            if not auto_apply:
                await send_reorg_card(proposal, telegram_chat_id)
                dedup.record(probe, now=now)
                counters["proposed"] += 1
                continue

            # act-then-notify: apply first, tell the person afterwards with an undo.
            failed_ids = await apply_reorg(st_ids, new_start_iso, telegram_chat_id)
            failed_ids = list(failed_ids or [])
            if st_ids and len(failed_ids) >= len(st_ids):
                # Nothing landed: no dedup record, so the next tick retries.
                logger.warning(
                    "butler_reorg watcher=%s auto-apply failed for every st_id %r",
                    watcher.key, st_ids,
                )
                counters["failed"] += 1
                continue

            proposal["applied"] = True
            if failed_ids:
                proposal["failed_ids"] = failed_ids
            await send_reorg_card(proposal, telegram_chat_id)
            dedup.record(probe, now=now)
            applied_today += 1
            counters["applied"] += 1
        except Exception as exc:
            logger.warning("butler_reorg watcher=%s proposal=%r failed: %s", watcher.key, raw, exc)
            counters["failed"] += 1
            continue

    logger.info(
        "butler_reorg watcher=%s proposed=%d applied=%d skipped=%d failed=%d",
        watcher.key, counters["proposed"], counters["applied"], counters["skipped"],
        counters["failed"],
    )
    return counters
