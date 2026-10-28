# scheduler.py
"""In-process notification scheduler (APScheduler AsyncIOScheduler).

One job per notification, ``id == notification.key``. bot.py injects its
``run_claude`` and an async ``send_target(chat_id, thread_id, text)`` via ``start()``; this
module never imports bot.py (acyclic graph). max_instances=1 = no overlap.

Downtime resilience: APScheduler's MemoryJobStore is not persistent, so a
trigger that fires while the bot is down would be lost forever. Each fire is
recorded (notif_store.record_run); on startup ``_schedule_catchup`` recomputes,
from the crontab, whether a trigger was missed during downtime and still falls
inside ``CATCHUP_WINDOW_SEC`` — if so it is re-fired once. ``coalesce`` collapses
bursts and the per-job ``misfire_grace_time`` covers in-process event-loop stalls.
"""
from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from apscheduler.util import astimezone

import butler_config
import butler_prep
import butler_reorg
import dm_replies
import notif_store
import notifications
import overview_store
import profiles
import providers
import taskgen
from taskgen_config import Watcher

logger = logging.getLogger("coordinator.scheduler")

# How far back a missed trigger is still worth recovering on startup (seconds).
# A 08:00 daily notification still arrives if the bot is back up by ~10:00.
CATCHUP_WINDOW_SEC = int(os.environ.get("NOTIF_CATCHUP_WINDOW_SEC", str(2 * 60 * 60)))

# Injected dependencies + the live scheduler, set in start().
_S: dict = {"sched": None, "run_claude": None, "send_to": None,
            "send_target": None, "tz": "Europe/Rome"}


def _tzobj():
    """The scheduler timezone as a concrete tzinfo (matches CronTrigger output)."""
    return astimezone(_S["tz"])


def next_run(key: str) -> str | None:
    """ISO next fire time of a job from the live scheduler, or None."""
    sched = _S.get("sched")
    if sched is None:
        return None
    job = sched.get_job(key)
    return job.next_run_time.isoformat() if job and job.next_run_time else None


def get_run_claude():
    """The injected run_claude callable (or None before start())."""
    return _S.get("run_claude")


def start(*, run_claude, send_to=None, send_target=None, tz: str = "Europe/Rome",
          topic_thread=None) -> None:
    """Create the scheduler, seed the store if empty, register enabled jobs, run.

    ``topic_thread(chat_id, name) -> thread_id | None`` resolves an
    ``@argomento:`` directive to a forum topic of that chat.
    """
    _S["run_claude"] = run_claude
    _S["send_to"] = send_to        # kept for API back-compat; unused by scheduler itself
    _S["send_target"] = send_target
    _S["topic_thread"] = topic_thread
    _S["tz"] = tz
    sched = AsyncIOScheduler(timezone=tz)
    _S["sched"] = sched
    notif_store.seed_if_empty()
    try:
        # Register the subsystem's own watchdog before reading the store, so it
        # is scheduled in this same pass (it is an ordinary notification).
        import notif_health

        notif_health.ensure_watchdog()
    except Exception:
        logger.exception("could not ensure the notification watchdog")
    try:
        # DeepSeek's balance, before a failed wake becomes the first evidence (#95).
        import llm_balance

        llm_balance.ensure_watchdog()
    except Exception:
        logger.exception("could not ensure the DeepSeek balance watchdog")
    notifs = notif_store.load()
    for notif in notifs:
        # Banned notifications (personal project, not allowlisted) keep their store
        # entry but never get a job — no inert daily wakeup, and /notifiche still
        # lists them. _fire is the runtime backstop. Allowlisted personal keys
        # (see notifications.NOTIFICATION_ALLOWLIST) are scheduled normally.
        if notif.enabled and not notifications.is_banned(notif):
            _register(notif)
    sched.start()
    _schedule_catchup(notifs)
    logger.info("scheduler started with %d jobs", len(sched.get_jobs()))


def _register(notif) -> None:
    _S["sched"].add_job(
        _fire,
        notifications.cron_trigger(notif.schedule, timezone=_S["tz"]),
        id=notif.key,
        args=[notif.key],
        max_instances=1,
        coalesce=True,
        misfire_grace_time=CATCHUP_WINDOW_SEC,
        replace_existing=True,
    )


def _last_fire_in_window(schedule: str, now: datetime, window_sec: int) -> datetime | None:
    """Most recent crontab trigger time within ``[now - window_sec, now]``, or None."""
    trig = notifications.cron_trigger(schedule, timezone=now.tzinfo)
    cursor = now - timedelta(seconds=window_sec)
    last: datetime | None = None
    for _ in range(10000):  # safety bound; window is small relative to cadence
        nxt = trig.get_next_fire_time(None, cursor)
        if nxt is None or nxt > now:
            break
        last = nxt
        cursor = nxt + timedelta(seconds=1)
    return last


def _schedule_catchup(notifs) -> None:
    """Re-fire (once) any enabled notification whose trigger was missed while down.

    A trigger counts as missed when its last in-window occurrence is newer than
    the recorded last run. First-seen notifications get a baseline instead of a
    surprise send, so this never fires spuriously right after a fresh deploy.
    """
    sched = _S["sched"]
    if sched is None:
        return
    runs = notif_store.load_runs()
    now = datetime.now(_tzobj())
    for notif in notifs:
        if not notif.enabled or notifications.is_banned(notif):
            continue
        baseline = runs.get(notif.key)
        if baseline is None:
            notif_store.record_run(notif.key, now.isoformat())  # establish baseline
            continue
        last_missed = _last_fire_in_window(notif.schedule, now, CATCHUP_WINDOW_SEC)
        if last_missed is None:
            continue
        try:
            last_run = datetime.fromisoformat(baseline)
        except ValueError:
            continue
        if last_missed > last_run:
            sched.add_job(
                _fire,
                "date",
                run_date=now + timedelta(seconds=5),
                args=[notif.key],
                id=f"catchup:{notif.key}",
                misfire_grace_time=None,
                replace_existing=True,
            )
            logger.info("catch-up scheduled for %s (missed trigger at %s)",
                        notif.key, last_missed.isoformat())


def apply(notif) -> None:
    """Sync the scheduler to a stored notification (add/reschedule/remove)."""
    sched = _S["sched"]
    if sched is None:
        return
    if notif.enabled:
        _register(notif)
    elif sched.get_job(notif.key):
        sched.remove_job(notif.key)


def unschedule(key: str) -> None:
    sched = _S["sched"]
    if sched and sched.get_job(key):
        sched.remove_job(key)


async def _fire(key: str) -> str:
    """Run one notification and PERSIST its outcome, returning that outcome.

    ``_fire_once`` below decides what happened; this wrapper writes it down.
    Returning the outcome was step one (it reached ``POST /{key}/test``), but a
    string that only ever lands in a ``logger.info`` is not measurement: nobody
    reads it, nothing aggregates it, and a notification can stay mute for weeks
    while looking perfectly healthy. The outcome ledger (notif_store
    .record_outcome) is what ``notif_health`` later aggregates.
    """
    when = datetime.now(_tzobj()).isoformat()
    outcome = await _fire_once(key)
    notif_store.record_outcome(key, outcome, when)
    return outcome


def _topic_thread(chat_id: int, name: str):
    """Thread of the named topic, or None (the General topic) when unknown."""
    resolver = _S.get("topic_thread")
    try:
        thread = resolver(chat_id, name) if resolver else None
    except Exception:
        logger.exception("topic lookup failed for %s/%r", chat_id, name)
        thread = None
    if thread is None:
        logger.warning("notification topic %r not found in chat %s: sent to General", name, chat_id)
    return thread


async def _fire_once(key: str) -> str:
    """Run one notification, returning WHY nothing was sent (or "sent").

    The outcome used to be invisible: every skip below returned None, so
    `POST /{key}/test` answered {"ok": true} while the policy gate dropped the
    notification without sending anything. A test that cannot fail is not a
    test — and that is how 14 dead notifications stayed unnoticed.
    """
    notif = notif_store.get(key)
    if notif is None:
        return notifications.OUTCOME_UNKNOWN_KEY
    if not notif.enabled:
        return notifications.OUTCOME_DISABLED
    # Record the trigger as fired (powers startup catch-up) before any skip below:
    # a paused/empty run still means this occurrence was handled, not missed.
    notif_store.record_run(key, datetime.now(_tzobj()).isoformat())
    # Policy gate (2026-06-15): the Coordinator does not produce or send
    # notifications for personal projects, EXCEPT keys in NOTIFICATION_ALLOWLIST.
    # Return BEFORE invoking the provider so neither a claude session runs
    # (produce) nor a Telegram message is sent. This is the authoritative backstop
    # — covers any banned notification regardless of how it entered the
    # store/scheduler (seed, manifest sync, /notifiche).
    if notifications.is_banned(notif):
        logger.info("notification %s skipped: banned by policy (personal/work)", key)
        return notifications.OUTCOME_BANNED
    # Pause gate: skip delivery if the project is paused via Network overview.
    from overview_snapshot import _project_of  # local import: overview_snapshot → notif_store (no cycle)
    project_key = _project_of(notif)
    if overview_store.is_paused(project_key):
        logger.info("notification %s skipped: project %s is paused", key, project_key)
        return f"progetto {project_key} in pausa"
    # Deterministic gate: skip (without ever invoking the provider/LLM) when the
    # notification's gate command exits non-zero.
    from gate import evaluate_gate  # local import: gate → projects only (no cycle)
    verdict = await evaluate_gate(notif)
    if not verdict.passed:
        # The reason travels with the outcome: a gate that says "not today" and a
        # gate whose script no longer exists both stop the notification, and only
        # the detail tells them apart (see gate.GateVerdict).
        logger.info("notification %s skipped by gate: %s", key, verdict.detail)
        return f"fermata dal gate: {verdict.detail}" if verdict.detail else "fermata dal gate"
    try:
        text = await providers.run_provider(notif, run_claude=_S["run_claude"])
    except providers.ProviderError as exc:
        logger.warning("notification %s failed: %s", key, exc)
        return f"{notifications.OUTCOME_ERROR_PREFIX}: {str(exc)[:500]}"
    except Exception:
        logger.exception("provider failed for %s", key)
        text = None
    topic = None
    if text:
        topic, text = notifications.split_topic_directive(text)
    if not text:
        logger.info("notification %s produced no text; skipping", key)
        return notifications.OUTCOME_NO_CONTENT
    targets = notifications.resolve_targets(notif)
    if not targets:
        logger.info("notification %s resolved to no targets", key)
        return notifications.OUTCOME_NO_TARGETS
    if topic:
        targets = [(chat_id, _topic_thread(chat_id, topic) if thread_id is None else thread_id)
                   for chat_id, thread_id in targets]
    if _S["send_target"] is None:
        logger.error("notification %s: no send_target configured; skipping", key)
        return notifications.OUTCOME_NO_CHANNEL
    is_dm = (notif.destination or "dm").strip().lower() == "dm"
    failed = 0
    for chat_id, thread_id in targets:
        try:
            await _S["send_target"](chat_id, thread_id, text)
        except Exception:
            logger.exception("send failed to %s/%s for %s", chat_id, thread_id, key)
            failed += 1
            continue
        if is_dm:
            # #55: the person may answer in that private chat; remember which
            # node asked so the reply can travel back instead of dying here.
            try:
                p = profiles.profile_for(chat_id)
                dm_replies.record_sent(
                    notif, chat_id, text,
                    is_admin=bool(p and p.is_admin), now=datetime.now(_tzobj()),
                )
            except Exception:
                logger.exception("could not record DM question %s -> %s", key, chat_id)
    if failed:
        return (f"{notifications.OUTCOME_SEND_FAILED_PREFIX} verso {failed} "
                f"destinatari su {len(targets)}")
    return notifications.OUTCOME_SENT


# --- taskgen (M5 Slice B) ------------------------------------------------
# One APScheduler job per enabled watcher, in-process, same pattern as the
# notification jobs above. taskgen.run_watcher and its config loaders
# (taskgen_config.load_watchers/load_trust_policy) do the actual work; this
# module only owns cadence-string -> trigger translation and job wiring, so
# it can stay import-cycle-free (never imports bot.py).

TASKGEN_CONFIG_PATH = os.environ.get("TASKGEN_CONFIG_PATH", "taskgen.config.json")


def _load_taskgen_config(path: str = TASKGEN_CONFIG_PATH) -> dict:
    """Read the taskgen config JSON file. Missing/unreadable -> {} (no watchers)."""
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        logger.info("taskgen config not found at %s; no watchers registered", path)
        return {}
    except Exception:
        logger.exception("could not load taskgen config from %s", path)
        return {}


def _taskgen_trigger(cadence: str):
    """Translate a Watcher.cadence string into an APScheduler trigger, or None.

    ``cron:<crontab>`` -> notifications.cron_trigger (standard 5-field crontab, scheduler
    timezone). ``interval:<n>s`` -> IntervalTrigger(seconds=n). Any other shape
    (or a malformed interval) returns None so the caller can skip + warn
    instead of raising out of job registration.
    """
    if cadence.startswith("cron:"):
        return notifications.cron_trigger(cadence[len("cron:"):], timezone=_S["tz"])
    if cadence.startswith("interval:") and cadence.endswith("s"):
        try:
            seconds = int(cadence[len("interval:"):-1])
        except ValueError:
            return None
        if seconds <= 0:
            return None
        return IntervalTrigger(seconds=seconds)
    return None


def register_taskgen_jobs(
    scheduler,
    *,
    run_claude,
    hw_client,
    send_notice,
    resolve_workspace,
    dedup,
    chat_id_of,
    save_state=None,
    config: dict | None = None,
) -> list:
    """Register one APScheduler job per enabled taskgen watcher.

    Mirrors ``_register`` above: stable job id per watcher key
    (``taskgen:<key>``), ``coalesce=True``, ``max_instances=1`` (no
    overlapping ticks of the same watcher). Disabled watchers, and watchers
    with an unrecognized ``cadence``, are skipped (no job added) — never
    raises out of registration.

    ``run_claude``, ``hw_client``, ``send_notice``, ``resolve_workspace`` and
    ``dedup`` are injected exactly like ``start()``'s deps — this module
    never imports ``bot.py``. ``chat_id_of(profile_key) -> int | None``
    resolves a watcher's ``recipient_profile`` to the Telegram chat id
    ``run_watcher`` should notify; a tick is skipped (logged) if it resolves
    to ``None``. ``save_state``, if given, is called with no arguments after
    every tick (success or failure) so a mutated ``dedup`` survives a bot
    restart — mirrors bot.py's ``_save_state`` pattern for
    ``_TASK_DUE_MSG``/``_TASK_DUE_CACHE``. ``config`` overrides the on-disk
    ``taskgen.config.json`` (tests pass a dict directly).

    Returns the list of registered ``Watcher`` objects (enabled ones with a
    valid cadence).
    """
    cfg = config if config is not None else _load_taskgen_config()
    from taskgen_config import load_trust_policy, load_watchers

    watchers = load_watchers(cfg)
    policy = load_trust_policy(cfg)
    registered: list[Watcher] = []

    for watcher in watchers:
        if not watcher.enabled:
            continue
        trigger = _taskgen_trigger(watcher.cadence)
        if trigger is None:
            logger.warning(
                "taskgen watcher %s: unrecognized cadence %r, skipped",
                watcher.key, watcher.cadence,
            )
            continue

        async def _tick(w: Watcher = watcher) -> None:
            chat_id = chat_id_of(w.recipient_profile)
            if chat_id is None:
                logger.warning(
                    "taskgen watcher %s: no chat id for profile %s, skipped",
                    w.key, w.recipient_profile,
                )
                return
            try:
                from taskgen_gate import passes_pre_gate  # local import, no cycle

                if not await passes_pre_gate(w, workspace=resolve_workspace(w.node_key)):
                    logger.info("taskgen watcher %s: pre-gate closed, node not woken", w.key)
                    return
                await taskgen.run_watcher(
                    w,
                    run_claude=run_claude,
                    hw_client=hw_client,
                    policy=policy,
                    dedup=dedup,
                    resolve_workspace=resolve_workspace,
                    send_notice=send_notice,
                    telegram_chat_id=chat_id,
                    now=time.time(),
                    default_category_id=policy.default_category_id,
                )
            except Exception:
                logger.exception("taskgen watcher %s tick failed", w.key)
            finally:
                if save_state is not None:
                    try:
                        save_state()
                    except Exception:
                        logger.exception("taskgen watcher %s: save_state failed", w.key)

        scheduler.add_job(
            _tick,
            trigger,
            id=f"taskgen:{watcher.key}",
            max_instances=1,
            coalesce=True,
            replace_existing=True,
        )
        registered.append(watcher)

    logger.info("taskgen: registered %d watcher job(s)", len(registered))
    return registered


# --- butler (M5 Slice A reorg + Slice C prep) -----------------------------
# One APScheduler job per enabled butler watcher, GEMELLO of
# register_taskgen_jobs above. Routes by watcher.kind to butler_reorg.run_reorg_watcher
# or butler_prep.run_prep_watcher; this module only owns cadence-string ->
# trigger translation and job wiring, import-cycle-free (never imports bot.py).

BUTLER_CONFIG_PATH = os.environ.get("BUTLER_CONFIG_PATH", "butler.config.json")


def _load_butler_config(path: str = BUTLER_CONFIG_PATH) -> dict:
    """Read the butler config JSON file. Missing/unreadable -> {} (no watchers)."""
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        logger.info("butler config not found at %s; no watchers registered", path)
        return {}
    except Exception:
        logger.exception("could not load butler config from %s", path)
        return {}


def register_butler_jobs(
    scheduler,
    *,
    run_claude,
    hw_client,
    resolve_workspace,
    resolve_specialist,
    chat_id_of,
    dedup,
    save_state=None,
    send_reorg_card,
    store_artifact,
    apply_reorg=None,
    propose_action=None,
    config: dict | None = None,
) -> list:
    """Register one APScheduler job per enabled butler watcher.

    Mirrors ``register_taskgen_jobs``: stable job id per watcher key
    (``butler:<key>``), ``coalesce=True``, ``max_instances=1``. Disabled
    watchers, and watchers with an unrecognized ``cadence``, are skipped
    (no job added) — never raises out of registration. A watcher whose
    resolved ``trigger_mode`` is ``"on_demand"`` (#21) is skipped too: it stays
    reachable only through the manual Telegram commands. The default
    ``"autonomous"`` keeps the historical behaviour.

    Each tick resolves ``chat_id = chat_id_of(watcher.recipient_profile)``
    (skips + logs if ``None``) and routes by ``watcher.kind``: ``"reorg"`` ->
    ``butler_reorg.run_reorg_watcher``, ``"prep"`` -> ``butler_prep.run_prep_watcher``;
    any other kind is skipped + logged. ``save_state``, if given, is called
    with no arguments after every tick (success or failure).

    ``apply_reorg`` (optional, injected by bot.py) is what lets the
    ``auto_apply_trusted`` reorg mode (#19) act on the schedule; without it —
    or without a ``trust`` section in the config — reorg ticks stay proposals.

    ``propose_action`` (optional, injected by bot.py) is what lets the
    ``side_effects`` prep mode (#20) ask the person to confirm a preparation
    action; without it — or without an ``action_trust`` section in the config —
    prep ticks stay readonly. It never executes anything: it sends a card.

    Returns the list of registered ``ButlerWatcher`` objects (enabled ones
    with a valid cadence).
    """
    cfg = config if config is not None else _load_butler_config()

    watchers = butler_config.load_butler_watchers(cfg)
    policy = butler_config.load_butler_policy(cfg)
    trust = butler_config.load_butler_trust(cfg)
    action_trust = butler_config.load_butler_action_trust(cfg)
    registered: list = []

    for watcher in watchers:
        if not watcher.enabled:
            continue
        if policy.resolve("trigger_mode", watcher.node_key) == "on_demand":
            logger.info(
                "butler watcher %s: trigger_mode=on_demand, no job registered "
                "(reachable only via the /riorganizza|/prepara commands)",
                watcher.key,
            )
            continue
        trigger = _taskgen_trigger(watcher.cadence)
        if trigger is None:
            logger.warning(
                "butler watcher %s: unrecognized cadence %r, skipped",
                watcher.key, watcher.cadence,
            )
            continue

        async def _tick(w=watcher) -> None:
            chat_id = chat_id_of(w.recipient_profile)
            if chat_id is None:
                logger.warning(
                    "butler watcher %s: no chat id for profile %s, skipped",
                    w.key, w.recipient_profile,
                )
                return
            try:
                if w.kind == "reorg":
                    await butler_reorg.run_reorg_watcher(
                        w,
                        run_claude=run_claude,
                        hw_client=hw_client,
                        dedup=dedup,
                        resolve_workspace=resolve_workspace,
                        send_reorg_card=send_reorg_card,
                        telegram_chat_id=chat_id,
                        now=time.time(),
                        policy=policy,
                        trust=trust,
                        apply_reorg=apply_reorg,
                    )
                elif w.kind == "prep":
                    await butler_prep.run_prep_watcher(
                        w,
                        run_claude=run_claude,
                        hw_client=hw_client,
                        dedup=dedup,
                        resolve_workspace=resolve_workspace,
                        resolve_specialist=resolve_specialist,
                        store_artifact=store_artifact,
                        telegram_chat_id=chat_id,
                        now=time.time(),
                        policy=policy,
                        action_trust=action_trust,
                        propose_action=propose_action,
                    )
                else:
                    logger.warning(
                        "butler watcher %s: unrecognized kind %r, skipped",
                        w.key, w.kind,
                    )
            except Exception:
                logger.exception("butler watcher %s tick failed", w.key)
            finally:
                if save_state is not None:
                    try:
                        save_state()
                    except Exception:
                        logger.exception("butler watcher %s: save_state failed", w.key)

        scheduler.add_job(
            _tick,
            trigger,
            id=f"butler:{watcher.key}",
            max_instances=1,
            coalesce=True,
            replace_existing=True,
        )
        registered.append(watcher)

    logger.info("butler: registered %d watcher job(s)", len(registered))
    return registered

