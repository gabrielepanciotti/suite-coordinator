"""butler_prep — Slice C watcher: prepares a readonly artifact ahead of a task.

Pure orchestration + injected I/O, no bot import (graph aciclico: butler_*
never imports bot.py). Gemello di taskgen.run_watcher.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import llm_usage
import wake_routing
from taskgen import _find_balanced_braces

logger = logging.getLogger("coordinator.butler_prep")

# Declared in <istanza>/config/llm-routing.json: model and fallback are the queue's.
SERVICE = "coordinator.preparazione"

# The only action kinds this module knows how to describe. Anything else the
# specialist invents is dropped: an action we cannot render on the confirmation
# card is an action the person cannot judge, so it must never be proposed.
SUPPORTED_ACTION_KINDS = ("email_draft",)


@dataclass(frozen=True)
class _PrepDedupKey:
    """Adapter so `taskgen.Dedup` (built around `Suggestion`) can dedup on
    `st_id|start_at` — `Dedup.key()` reads `.title`/`.when_iso`/`.category`."""

    title: str
    when_iso: str
    category: str = "prep"


def build_prep_seed(st: dict) -> str:
    """Prompt asking a specialist to produce a ready deliverable ahead of time.

    Asks for plain text only (no JSON contract) — a recipe/checklist/shopping
    list/draft, whatever fits the task's category, ready to be shown to the
    person on the `task.due` card before the task actually starts.
    """
    name = st.get("name")
    category = st.get("category")
    start_at = st.get("start_at")
    return (
        f"Prepara in anticipo il deliverable per «{name}» (categoria {category}, "
        f"ore {start_at}). Produci l'artefatto pronto (ricetta/checklist/lista "
        "spesa/bozza, a seconda del task) — solo testo, nessun JSON, nessun "
        "commento aggiuntivo: solo il contenuto pronto da mostrare."
    )


def build_actions_seed(st: dict, artifact: str) -> str:
    """Second prompt of the ``side_effects`` mode (#20): which PREPARATION
    actions, if any, would help this task — given the artifact just produced.

    Asks for a JSON block only (twin of ``butler_reorg.build_reorg_seed``), and
    is explicit that nothing is executed here: whatever comes back is shown to
    the person for an explicit confirmation first.
    """
    name = st.get("name")
    category = st.get("category")
    start_at = st.get("start_at")
    return (
        "Sei il maggiordomo. Hai appena preparato questo artefatto per il task "
        f"«{name}» (categoria {category}, ore {start_at}):\n\n"
        f"{artifact}\n\n"
        "Individua le eventuali azioni di preparazione verso l'esterno che "
        "converrebbe compiere prima del task (nell'MVP solo bozze e-mail). "
        "Rispondi SOLO con un blocco JSON, nessun altro testo, in questo schema:\n"
        '{"actions": [{"kind": "email_draft", "to": "...", "subject": "...", '
        '"body": "...", "rationale": "..."}]}\n\n'
        'Se non c\'è nulla da preparare, rispondi con {"actions": []}. '
        "Nessuna azione verrà eseguita senza una conferma esplicita della persona."
    )


def _actions_from_obj(obj: dict) -> list[dict] | None:
    if not isinstance(obj, dict) or "actions" not in obj:
        return None
    items = obj.get("actions")
    if not isinstance(items, list):
        return None
    return [it for it in items if isinstance(it, dict)]


def parse_prep_actions(text: str) -> list[dict]:
    """Extract the FIRST valid JSON object with key ``actions`` from text.

    Never raises; returns ``[]`` on any failure or absence of a usable block
    (same balanced-brace scan as ``butler_reorg.parse_reorg_proposals``).
    """
    try:
        if not text:
            return []
        for cand in _find_balanced_braces(text):
            try:
                obj = json.loads(cand)
            except (json.JSONDecodeError, TypeError, ValueError):
                continue
            actions = _actions_from_obj(obj)
            if actions is not None:
                return actions
        return []
    except Exception:
        return []


async def _propose_actions(
    *,
    watcher,
    st: dict,
    artifact: str,
    run_claude,
    resolve_workspace,
    project: str,
    agent,
    action_trust,
    propose_action,
    telegram_chat_id: int,
    counters: dict,
) -> None:
    """``side_effects`` half of a prep tick: ask for actions, propose the trusted ones.

    SAFETY INVARIANT — this module NEVER executes an action, it only proposes it.
    ``action_trust`` decides whether an action is worth disturbing the person
    with; the action itself happens only after the person taps confirm, in the
    bot's callback handler. There is deliberately no "auto-execute if trusted"
    branch here, and there must never be one.
    """
    seed = build_actions_seed(st, artifact)
    run = await wake_routing.run_node_service(SERVICE, project, seed, persona=agent)
    if run is None:
        run = await run_claude(
            seed,
            workspace=resolve_workspace(project),
            persona=agent,
            resume_id=None,
        )
    text, _sid = run
    llm_usage.record(run, node=project, caller="butler_prep:actions")
    actions = parse_prep_actions(text)
    proposed_today = 0

    for action in actions:
        try:
            kind = action.get("kind")
            if kind not in SUPPORTED_ACTION_KINDS:
                logger.info(
                    "butler_prep watcher=%s: azione kind=%r non supportata, scartata",
                    watcher.key, kind,
                )
                continue
            if action_trust.decision(kind) != "trusted":
                logger.info(
                    "butler_prep watcher=%s: azione kind=%r non fidata, scartata "
                    "(non vale la pena disturbare la persona)",
                    watcher.key, kind,
                )
                continue
            if proposed_today >= action_trust.daily_cap:
                logger.info(
                    "butler_prep watcher=%s: cap giornaliero azioni (%d) raggiunto",
                    watcher.key, action_trust.daily_cap,
                )
                break
            await propose_action(action, st, telegram_chat_id)
            proposed_today += 1
            counters["actions_proposed"] += 1
        except Exception as exc:
            logger.warning(
                "butler_prep watcher=%s action=%r failed: %s", watcher.key, action, exc
            )
            counters["failed"] += 1
            continue


async def run_prep_watcher(
    watcher,
    *,
    run_claude,
    hw_client,
    dedup,
    resolve_workspace,
    resolve_specialist,
    store_artifact,
    telegram_chat_id: int,
    now: float,
    policy,
    action_trust=None,
    propose_action=None,
) -> dict:
    """Run one prep-watcher tick: fetch schedule → pre-execute eligible ST → store artifact.

    Window: `frm=now, to=now+watcher.horizon_hours` (epoch → ISO, tolerant fetch:
    any `hw_client.list_scheduled_tasks` failure degrades to `[]`, never raises).

    Eligibility: only scheduled tasks with `notification_state == "pending"` AND
    whose category resolves to a REAL specialist via
    `resolve_specialist(st["category"])`. `resolve_specialist` is INJECTED (prod:
    `task_routing.resolve_specialist`; tests: a lambda) to keep this module's
    import graph acyclic (never imports task_routing/bot directly). When it
    returns the generic fallback `("scheduler", "generale")` the ST is SKIPPED
    (counted in `skipped`) — pre-executing the generic specialist for
    unmapped categories would waste a run_claude call on output nobody asked
    for, so we only ever prepare artifacts for a real domain specialist.

    Per eligible ST: dedup identity is `st_id|start_at` (via a `taskgen.Dedup`
    adapter key so the same ST/start_at combo is never re-prepared); if `dedup.seen`
    the ST is skipped. Otherwise `policy.resolve("prep_output_mode", watcher.node_key
    or project)` decides the mode:
    - `"readonly_artifact"` (default): resolves `(project, agent)` via
      `resolve_specialist`, runs `run_claude(build_prep_seed(st),
      workspace=resolve_workspace(project), persona=agent, resume_id=None)`.
      An empty/None artifact is NOT stored (nothing worth caching) and does
      NOT count as prepared. A non-empty artifact is persisted via
      `store_artifact(st_id, artifact)` and `dedup.record` is called;
      `prepared` is incremented.
    - `"side_effects"` (#20): ADDITIVE — the readonly artifact is still produced
      and stored exactly as above, and on top of it a SECOND `run_claude` (with
      `build_actions_seed`, same workspace/persona) asks for preparation actions
      with effects on the outside world. Each parsed action is dropped unless its
      `kind` is supported AND `action_trust.decision(kind) == "trusted"` AND the
      `action_trust.daily_cap` still has room; the survivors are handed to the
      injected `propose_action(action, st, telegram_chat_id)`, which sends a
      confirmation card. Requires BOTH `action_trust` and `propose_action`:
      either one missing degrades to `readonly_artifact`.
    - anything else: logs the unknown mode and degrades to `readonly_artifact`
      behavior for this ST (never crashes, never performs side effects).

    SAFETY INVARIANT: this module NEVER executes an action, it only ever
    proposes one. The trust policy decides whether an action is worth showing to
    the person; execution happens exclusively after an explicit tap, in the
    bot's callback handler. No "auto-execute if trusted" branch exists here.

    Each ST's processing is isolated in a try/except: any exception counts
    toward `failed` and does not abort the tick or affect other STs.

    Returns counters: ``{"prepared": n, "skipped": n, "failed": n,
    "actions_proposed": n}``.
    """
    counters = {"prepared": 0, "skipped": 0, "failed": 0, "actions_proposed": 0}

    frm_dt = datetime.fromtimestamp(now, tz=timezone.utc)
    to_dt = frm_dt + timedelta(hours=watcher.horizon_hours)
    frm = frm_dt.isoformat()
    to = to_dt.isoformat()

    try:
        schedule = await hw_client.list_scheduled_tasks(
            telegram_chat_id=telegram_chat_id, frm=frm, to=to
        )
    except Exception:
        schedule = []

    for st in schedule or []:
        try:
            if not isinstance(st, dict):
                counters["failed"] += 1
                continue
            if st.get("notification_state") != "pending":
                continue

            category = st.get("category")
            project, agent = resolve_specialist(category)
            if (project, agent) == ("scheduler", "generale"):
                counters["skipped"] += 1
                continue

            st_id = st.get("id")
            start_at = st.get("start_at")
            key_sugg = _PrepDedupKey(title=str(st_id), when_iso=str(start_at))
            if dedup.seen(key_sugg, now=now):
                counters["skipped"] += 1
                continue

            mode = policy.resolve("prep_output_mode", watcher.node_key or project)
            if mode not in ("readonly_artifact", "side_effects"):
                logger.info(
                    "butler_prep watcher=%s st_id=%s mode=%r sconosciuta, "
                    "degrado a readonly_artifact",
                    watcher.key, st_id, mode,
                )
                mode = "readonly_artifact"
            side_effects = (
                mode == "side_effects"
                and action_trust is not None
                and propose_action is not None
            )
            if mode == "side_effects" and not side_effects:
                logger.info(
                    "butler_prep watcher=%s st_id=%s: side_effects senza "
                    "action_trust/propose_action, degrado a readonly_artifact",
                    watcher.key, st_id,
                )

            seed = build_prep_seed(st)
            run = await wake_routing.run_node_service(SERVICE, project, seed, persona=agent)
            if run is None:
                run = await run_claude(
                    seed,
                    workspace=resolve_workspace(project),
                    persona=agent,
                    resume_id=None,
                )
            artifact, _sid = run
            llm_usage.record(run, node=project, caller="butler_prep")

            if not artifact:
                continue

            await store_artifact(st_id, artifact)
            dedup.record(key_sugg, now=now)
            counters["prepared"] += 1

            if side_effects:
                await _propose_actions(
                    watcher=watcher,
                    st=st,
                    artifact=artifact,
                    run_claude=run_claude,
                    resolve_workspace=resolve_workspace,
                    project=project,
                    agent=agent,
                    action_trust=action_trust,
                    propose_action=propose_action,
                    telegram_chat_id=telegram_chat_id,
                    counters=counters,
                )
        except Exception as exc:
            logger.warning(
                "butler_prep watcher=%s st=%r failed: %s", watcher.key, st, exc
            )
            counters["failed"] += 1
            continue

    logger.info(
        "butler_prep watcher=%s prepared=%d skipped=%d failed=%d actions_proposed=%d",
        watcher.key, counters["prepared"], counters["skipped"], counters["failed"],
        counters["actions_proposed"],
    )
    return counters
