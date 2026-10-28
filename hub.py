# hub.py
"""Network integration layer for Coordinator (Fase 1: tenant personale).

Opens SSE listeners (one per local agent + one as telegram-owner), routes
events to forum-topics or local run_claude wakes, sends human replies back to
the bus, and serves /hub. Dependency-injection like scheduler.py: never imports
bot.py (acyclic). See docs/specs/2026-06-04-coordinator-agenthub-integration-design.md.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as _dt
import html
import logging
import os
import re
import time
from pathlib import Path

import a2a_client
import agent_nodes
import claude_runner
import escalation
import hub_client
import hub_config
import hub_origin
import hub_store
import remote_turn
import llm_usage
import migrate
import overview_snapshot
import pipeline
import projects
import restructure
import status_report
import wake_pool
import wake_routing

logger = logging.getLogger("coordinator.hub")

# Wake retry budget for TRANSPORT faults (bus unreachable, CLI exit, stream cut
# short): these never reached a model, so they are retried with exponential
# backoff and do NOT consume one of the two answer attempts (#29). Bounded on
# purpose — the wake is awaited inside the SSE listener, so the worst case here
# (2+4+8s) is how long a single event can hold that listener.
WAKE_TRANSPORT_RETRIES = int(os.environ.get("HUB_WAKE_TRANSPORT_RETRIES", "3"))
WAKE_TRANSPORT_BACKOFF_S = float(os.environ.get("HUB_WAKE_TRANSPORT_BACKOFF_S", "2"))
# A single wake may ask for a longer wall-clock budget than CLAUDE_MAX_TIMEOUT
# via ``context.max_minutes`` on the waking message (#70), up to this ceiling.
WAKE_MAX_MINUTES_CAP = int(os.environ.get("HUB_WAKE_MAX_MINUTES_CAP", "180"))
# How often a running wake re-asserts ``running`` on the bus (#70). The bus
# expires the state after 6h, so anything well under that keeps it alive.
WAKE_RUNNING_REFRESH_S = float(os.environ.get("HUB_WAKE_RUNNING_REFRESH_S", "1800"))
# A live interactive session on this host keeps the headless twin out (#51,
# #54) -- but "live" only means its window is open. If it has not written its
# transcript since the message arrived, nobody is behind it: after this long
# the worker is woken anyway, on its own derived session (#74). 0 disables.
IDLE_VOICE_RECHECK_S = float(os.environ.get("HUB_IDLE_VOICE_RECHECK_S", "600"))
# While the session keeps working (transcript written) it gets another window,
# up to this many; then it is left alone, it is clearly busy with something.
IDLE_VOICE_MAX_RECHECKS = int(os.environ.get("HUB_IDLE_VOICE_MAX_RECHECKS", "6"))
# Marker on the event data of the forced wake: survives the persisted queue.
IDLE_VOICE_OVERRIDE = "_idle_voice_override"

# Every notice the Coordinator posts on the bus BY ITSELF, as an agent ("did not
# answer", "turn cap reached", "CLI not authenticated", "BLOCKED") starts with
# this tag. Writer and filter share it so they cannot diverge: such a notice is
# addressed to the human, and waking the other agent on it is how two silent
# agents ended up escalating at each other every 40 seconds (#59).
AUTO_NOTICE_TAG = "[avviso automatico Coordinator]"
# The same notices as worded before the tag existed: a Coordinator not yet
# redeployed on another box keeps posting them during a rollout.
_LEGACY_AUTO_NOTICE = re.compile(
    r"^(?:L'agente \S+ non ha risposto alla conversazione #\d+"
    r"|Raggiunto il tetto di \d+ turni automatici"
    r"|\U0001f512 La CLI di questo Coordinator non e' autenticata"
    r"|\U0001f513 L'agente \S+ è BLOCCATO)"
)


def is_auto_notice(body: str | None) -> bool:
    """True if `body` is one of the Coordinator's own automatic notices."""
    text = (body or "").lstrip()
    return text.startswith(AUTO_NOTICE_TAG) or bool(_LEGACY_AUTO_NOTICE.match(text))


def _auto_notice(text: str) -> str:
    return f"{AUTO_NOTICE_TAG} {text}"

# Un 401/403 sullo stream SSE non e' un guasto di trasporto: la credenziale e'
# sbagliata, o l'identita' e' stata ritirata dal roster del bus. Nessun retry lo
# rimettera' a posto. Fino al 2026-09 il listener lo trattava come un errore di
# rete qualunque (WARNING + riconnessione a 30s), quindi il processo restava su,
# sano all'apparenza, e non ricevendo nulla non aveva modo di accorgersene: due
# identita' ritirate (itmap/itvault) sono state respinte ~2 volte al minuto per
# venti ore senza che nulla cambiasse colore (#32). Dopo N rifiuti CONSECUTIVI
# sulla stessa identita' il listener smette di ritentare, lo dice a voce alta e
# resta registrato fra i morti: un'assenza dichiarata e' diagnosticabile, un
# retry infinito no.
AUTH_REJECTS_BEFORE_GIVING_UP = int(os.environ.get("HUB_AUTH_REJECTS_MAX", "3"))
_AUTH_REJECT_STATUSES = hub_client.AUTH_REJECT_STATUSES

# Origin-session resume (#51): the host's session registry + transcript store,
# read through the container mounts (~/.claude -> /home/app/.claude). Overridable
# for tests. The transcript store mirrors projects.PROJECTS_ROOT, but kept here
# as its own knob so hub_origin stays decoupled from the registry.
_HUB_SESSIONS_DIR = Path(
    os.environ.get("HUB_SESSIONS_DIR") or Path.home() / ".claude" / "state" / "sessions"
)
_HUB_PROJECTS_DIR = Path(
    os.environ.get("HUB_PROJECTS_DIR") or Path.home() / ".claude" / "projects"
)

# task.due dispatch (M5): injected like run_claude/send_to, never a bot.py
# import. bot.py registers the real handler at startup via
# set_task_due_handler(); the dedup set guards against the bus re-delivering
# the same scheduled task (at-least-once push semantics). The dedup key is
# the pair (scheduled_task_id, phase): Scheduler pushes two distinct events
# per task ("pre" ~10min before, "start" at the scheduled time) sharing the
# same scheduled_task_id, and both must render as separate cards — only a
# redelivery of the SAME (id, phase) is a true duplicate.
_task_due_handler = None
_seen_task_due: set[tuple[int, object]] = set()


def set_task_due_handler(cb) -> None:
    """Inject the async callback invoked for inbound task.due events.

    ``cb`` is ``Callable[[dict], Awaitable[None]]``. Dependency-injection like
    the scheduler does with ``run_claude`` — hub.py must never import bot.py.
    """
    global _task_due_handler
    _task_due_handler = cb


_S: dict = {
    "bot": None,
    "run_claude": None,
    "send_to": None,
    "config": None,
    "clients": {},
    "a2a_clients": {},
    # SSE listener tasks split by responsibility (see the module docstring):
    #  - local_tasks: per-local-agent listeners — run on EVERY node, always.
    #  - owner_tasks: the telegram-owner listener — Telegram owner only.
    # "tasks" is kept as a live view (local + owner) for back-compat with code
    # and tests that ask "how many SSE listeners are up?".
    "local_tasks": [],  # SSE listener tasks for local agents (always-on)
    "owner_tasks": [],  # SSE listener task(s) gated on Telegram ownership
    "snap_task": None,  # overview snapshot loop task (owner-only, separate ref)
    "status_task": None,  # fleet status-report loop (local/always-on, every node)
    "resume_task": None,  # pipeline boot-resume task (owner-only)
    "migrate_resume_task": None,  # migration boot-resume task (owner-only)
    "restructure_resume_task": None,  # restructure boot-resume task (owner-only)
    "send_thread": None,
    # Listener che si sono spenti da soli perche' il bus li rifiuta (401/403):
    # (tenant, agent) -> dettaglio. Sopravvive allo stop dei task, cosi' la
    # diagnosi resta leggibile dopo, non solo nel log del momento.
    "dead_listeners": {},
    # Anti-gemello headless-vs-headless (#38): (tenant, agent) -> conv_id del
    # wake attualmente in corso. La stessa notifica puo' arrivare due volte
    # (SSE + webhook A2A) e generare due _wake_local_agent concorrenti sullo
    # stesso workspace, nessuno dei due al corrente dell'altro -- una finisce
    # in fretta ed escala una conversazione che l'altra sta ancora lavorando
    # per davvero. is_watched() copre solo il caso interattivo/headless;
    # questo e' l'equivalente fra due spawn headless.
    "inflight_wakes": {},
    # message_id of the event the in-flight wake is handling: a second event on
    # the SAME conversation is a twin only if it is not newer than this (#67).
    "inflight_msgs": {},
    # (tenant, agent) -> task draining hub_store's wake queue (#67): wakes that
    # found the slot busy are parked there, and started one by one afterwards.
    "wake_drainers": {},
    "idle_voice_rechecks": {},
}


def _all_listener_tasks() -> list:
    """Combined live view of every SSE listener task (local + owner)."""
    return [*_S["local_tasks"], *_S["owner_tasks"]]


# Bus-epoch guard: run the DB-rebuild check once per tenant per process. The set
# is only added-to on a confirmed epoch read, so a deferred (bus-unreachable)
# check is retried on the next reconnect. The lock serialises the concurrent
# _listen tasks that share a tenant so the reset runs at most once.
_epoch_checked: set[str] = set()
_epoch_lock = asyncio.Lock()


async def _ensure_bus_epoch(tenant: str) -> None:
    """Detect a Network DB rebuild and drop stale local state before consuming.

    hub_store keys conversations by conv_id and cursors by event_id — both reset
    to low numbers when Network's DB is recreated (``down -v`` / new volume). A
    leftover ``wake_escalated`` then silently suppresses every wake and a high
    cursor skips new events (issue #25). We fingerprint the bus DB instance
    (GET /api/system/epoch) and, on change, clear this tenant's conv records +
    cursors so wakes resume from a clean slate. Idempotent and fail-safe: a bus
    that can't be reached defers the check (no state touched)."""
    if tenant in _epoch_checked:
        return
    async with _epoch_lock:
        if tenant in _epoch_checked:
            return
        cfg = _S["config"]
        agent_id = next(iter(cfg.agents_for(tenant)), cfg.telegram_agent)
        try:
            epoch = await _client(tenant, agent_id).get_bus_epoch()
        except Exception as exc:
            logger.warning("hub epoch check deferred for %s (bus unreachable): %s", tenant, exc)
            return  # not marked checked -> retried on next reconnect
        if not epoch:
            _epoch_checked.add(tenant)  # older Network without the endpoint
            return
        seen = hub_store.get_epoch(tenant)
        if seen and seen != epoch:
            n = hub_store.reset_bus_state(tenant)
            logger.warning(
                "hub: bus DB rebuild detected for tenant %s (epoch %s -> %s); "
                "cleared %d stale conv record(s) + cursors",
                tenant,
                seen,
                epoch,
                n,
            )
        hub_store.set_epoch(tenant, epoch)
        _epoch_checked.add(tenant)


class OutcomeUnknown(Exception):
    """The bus could not be read to confirm a wake's outcome. Raised so _listen
    reconnects from the old cursor (redelivery) instead of advancing past an
    unverified message — fail-safe toward retry, never toward silent loss."""


def _client(tenant: str, agent_id: str) -> hub_client.HubClient:
    cfg = _S["config"]
    tenant = cfg.canonical_tenant(tenant)
    key = (tenant, agent_id)
    if key not in _S["clients"]:
        _S["clients"][key] = hub_client.HubClient(
            cfg.hub_url, cfg.api_keys[tenant], agent_id, cfg.agent_key(agent_id, tenant)
        )
    return _S["clients"][key]


def _a2a_client(tenant: str, agent_id: str) -> a2a_client.A2AClient:
    """A2A JSON-RPC client cached per (tenant, acting agent). Same credentials
    as _client; used for outbound sends only when cfg.use_a2a is set."""
    cfg = _S["config"]
    tenant = cfg.canonical_tenant(tenant)
    key = (tenant, agent_id)
    if key not in _S["a2a_clients"]:
        _S["a2a_clients"][key] = a2a_client.A2AClient(
            cfg.hub_url, cfg.api_keys[tenant], agent_id, cfg.agent_key(agent_id, tenant)
        )
    return _S["a2a_clients"][key]


def start(
    *,
    bot,
    run_claude,
    send_to,
    config: hub_config.HubConfig,
    spawn_listeners: bool = True,
) -> None:
    """Wire deps and (optionally) spawn the SSE listener tasks.

    Listeners are split into two groups (see the module docstring):
      - local listeners (start_local_listeners) run on EVERY node, always —
        they wake the agents this box hosts (`*-local` / `*-minipc`).
      - owner listeners (start_owner_listeners) — the telegram-owner bus
        listener + snapshot/resume loops — follow the Telegram ownership.

    With dynamic ownership (ownership.py) bot.py wires deps here with
    spawn_listeners=False and starts the local listeners itself at on_startup;
    the ownership supervisor toggles only the owner listeners on acquire/
    release. Single-node (ownership disabled) keeps the legacy behaviour:
    spawn_listeners=True brings up both groups here.
    """
    _S["bot"] = bot
    _S["run_claude"] = run_claude
    _S["send_to"] = send_to
    _S["config"] = config
    if not config.enabled:
        logger.info("hub integration disabled (config not complete)")
        return
    if spawn_listeners:
        start_local_listeners()
        start_owner_listeners()


def start_local_listeners() -> int:
    """Spawn the SSE listener tasks for the LOCAL agents only (idempotent).

    These wake the agents this node hosts and must run on every node regardless
    of Telegram ownership. No telegram_agent listener, no snapshot/resume loops.
    For role 'worker' this is the only group (workers have no human face).
    Returns the number of live local SSE listener tasks.
    """
    config = _S["config"]
    if not config or not config.enabled:
        return 0
    live = [t for t in _S["local_tasks"] if not t.done()]
    if live:
        return len(live)  # already listening
    _S["local_tasks"].clear()  # drop stale refs before rebuilding
    wake_pool.configure(config.claude_max_concurrency)
    # Sessions written before the per-agent keying are shared between the agents
    # of a conversation: each may carry another node's project and history, and
    # a poisoned one blocks for the whole idle timeout on every resume (#30).
    # Dropping them costs one cold session per conversation.
    purged = hub_store.purge_legacy_sessions()
    # No run of this process is alive yet: a `running` from before a restart
    # would show a session as busy forever (TicketManager#10).
    hub_store.clear_running_sessions()
    if purged:
        logger.warning(
            "hub: dropped %d legacy per-conversation session(s) — they were "
            "shared between the agents of a conversation and could resume in "
            "the wrong workspace (#30). Those conversations start a fresh "
            "session on their next wake.",
            purged,
        )
    if config.a2a_webhook_enabled and config.role != "worker":
        # Webhook mode: Network pushes events to us; no SSE listeners (would
        # double-deliver). Recovery below still runs (it reads the REST inbox).
        logger.info("hub local SSE listeners skipped (A2A webhook mode)")
    else:
        if config.a2a_webhook_enabled:
            # HUB_A2A_WEBHOOK_URL is a per-DEVICE var read by every service on
            # the box, but only the bot can act on it: the receiver lives on
            # api.app (bot-only) and register_push_webhooks is called from
            # bot.on_startup. A worker that honoured it would drop its SSE
            # listeners, register nothing, serve nothing — and say so with a
            # cheerful INFO line. It keeps SSE instead: the bot's push configs
            # cover a disjoint agent set (BOT_LOCAL_AGENTS vs HUB_LOCAL_AGENTS),
            # so there is no double delivery to fear.
            logger.warning(
                "A2A webhook mode ignored for role=worker: this process serves "
                "no /a2a/webhook endpoint and registers no push config. Keeping "
                "the SSE listeners — without them the worker would receive "
                "nothing at all."
            )
        for tenant in config.api_keys:
            for agent_id in config.agents_for(tenant):
                t = asyncio.create_task(_listen(tenant, agent_id))
                _S["local_tasks"].append(t)
    # Worker boot-recovery: re-wake conversations left open while this node was
    # down. Owner nodes recover via the owner listeners' resume loops instead.
    if config.role == "worker":
        for tenant in config.api_keys:
            for agent_id in config.agents_for(tenant):
                asyncio.create_task(_recover_backlog(tenant, agent_id))
    # Wakes killed mid-run by the last shutdown (#73): once per process, before
    # anything here can have started a run, they rejoin the head of the queue.
    if not _S.get("interrupted_requeued"):
        _S["interrupted_requeued"] = True
        for tenant, agent_id, conv_id in hub_store.requeue_interrupted_wakes():
            logger.warning(
                "hub wake: #%s for %s was interrupted by a restart, requeued", conv_id, agent_id
            )
    # Wakes parked behind a busy slot before a restart (#67): resume them.
    for tenant, agent_id in hub_store.wake_queue_agents():
        if agent_id in config.local_agents and tenant in config.api_keys:
            schedule_wake_drain(tenant, agent_id)
    # Fleet status-report loop: always-on, one per node (not gated on ownership),
    # so every device reports the workspaces it hosts to the Network overview.
    status_t = _S.get("status_task")
    if status_t is None or status_t.done():
        _S["status_task"] = asyncio.create_task(_status_report_loop())
    # One-shot roster reconciliation: an agent configured here but unknown to the
    # bus fails silently-but-forever otherwise (see _check_local_roster).
    roster_t = _S.get("roster_task")
    if roster_t is None or roster_t.done():
        _S["roster_task"] = asyncio.create_task(_check_local_roster())
    _report_orphan_tenant_agents(config)
    logger.info(
        "hub local listeners started: %d + status loop (agenti da: %s)",
        len(_S["local_tasks"]),
        getattr(config, "local_agents_source", "") or "?",
    )
    return len(_S["local_tasks"])


def _report_orphan_tenant_agents(config) -> None:
    """Say it when a configured local agent belongs to no listened tenant.

    `agents_for()` filters them out silently, so a stale tenant prefix in
    BOT_LOCAL_AGENTS/HUB_LOCAL_AGENTS (`acme/` after the switch to `life-work`)
    meant "hub local listeners started: 0" and nothing else (conv #658). ERROR
    in the log every boot; the Telegram alarm once per process.
    """
    orphans = config.orphan_tenant_agents()
    if not orphans:
        return
    stale = sorted({t for t, _ in orphans})
    logger.error(
        "hub local agents IGNORED: %d agent(s) in BOT_LOCAL_AGENTS/HUB_LOCAL_AGENTS "
        "are qualified with tenant(s) %s, which are not in HUB_TENANTS (%s) — "
        "nobody listens for them: %s",
        len(orphans), ", ".join(stale), ", ".join(config.api_keys) or "-",
        ", ".join(f"{t}/{a}" for t, a in orphans),
    )
    if _S.get("orphan_tenants_announced"):
        return
    _S["orphan_tenants_announced"] = True
    try:
        asyncio.get_running_loop().create_task(_announce_orphan_tenants(config, orphans))
    except RuntimeError:
        pass  # no loop (sync caller/tests): the log line above is the signal


async def _announce_orphan_tenants(config, orphans: list[tuple[str, str]]) -> None:
    send_to = _S.get("send_to")
    if send_to is None:
        return
    stale = sorted({t for t, _ in orphans})
    shown = ", ".join(f"{t}/{a}" for t, a in orphans[:8])
    more = f" (+{len(orphans) - 8})" if len(orphans) > 8 else ""
    text = (
        f"\u26d4 Agenti locali ignorati: {len(orphans)} voci di "
        f"<code>BOT_LOCAL_AGENTS</code>/<code>HUB_LOCAL_AGENTS</code> hanno il tenant "
        f"<code>{html.escape(', '.join(stale))}</code>, che non è in "
        f"<code>HUB_TENANTS</code> (<code>{html.escape(', '.join(config.api_keys) or '-')}</code>).\n"
        f"Nessuno ascolta per loro: <code>{html.escape(shown)}</code>{more}\n"
        f"Da fare: correggere il prefisso nel <code>.env</code> e ricreare il bot."
    )
    try:
        import profiles

        for uid in profiles.admin_user_ids():
            await send_to(uid, text)
    except Exception:
        logger.exception("orphan-tenant alert failed")


async def _check_local_roster() -> None:
    """Compare HUB_LOCAL_AGENTS against the bus roster, once at boot.

    A local agent the bus does not know answers 403 to *every* call, for ever:
    the SSE listener reconnects, the recovery loop retries, the status report
    retries — and from the outside the node looks healthy while serving nobody.
    That is exactly what happened on this PC in 2026-08: the bus renamed the
    dev-device agents away from the `-prod` suffix (`cosmos-prod` -> `cosmos`)
    and the .env stayed behind, so six listeners spun on 403 for weeks.

    Diagnosis only — nothing here changes behaviour. It turns an endless
    per-request 403 into one actionable line that names the identity which
    *would* have worked. Fail-safe: any error is logged, never raised.
    """
    cfg = _S["config"]
    if not cfg:
        return
    for tenant in cfg.api_keys:
        configured = list(cfg.agents_for(tenant))
        if not configured:
            continue
        try:
            roster = await _client(tenant, configured[0]).list_agents(tenant)
        except Exception as exc:
            # Reaching here means even the roster call was refused: the node has
            # no accepted identity at all on this tenant, which is a superset of
            # the per-agent problem and just as invisible without this line.
            logger.error(
                "bus roster check FAILED for tenant '%s' (%s): this node cannot "
                "list the agents of its own tenant, so every listener, recovery "
                "and status-report call will keep failing. Check HUB_URL, the "
                "tenant key and HUB_AGENT_KEYS.",
                tenant, exc,
            )
            continue
        known = {a.get("name") for a in roster if a.get("is_active", True)}
        _warn_unheard_device_agents(cfg, tenant, known, configured)
        missing = [a for a in configured if a not in known]
        if not missing:
            logger.info(
                "bus roster OK for '%s': all %d local agents are registered",
                tenant, len(configured),
            )
            continue
        for agent_id in missing:
            project = (cfg.local_agents.get(agent_id) or "").lower()
            same_project = sorted(
                str(a.get("name"))
                for a in roster
                if str(a.get("project", "")).lower() == project
                and a.get("is_active", True)
            )
            hint = (
                f" Registered under the same project: {', '.join(same_project)}."
                if same_project
                else " No agent is registered for that project either."
            )
            logger.error(
                "bus identity MISSING: '%s' (project '%s') is in HUB_LOCAL_AGENTS "
                "but not registered on tenant '%s' — every call for it answers 403 "
                "for ever, so this node listens for nobody.%s",
                agent_id, project, tenant, hint,
            )


def _warn_unheard_device_agents(cfg, tenant, known, configured) -> None:
    """The reverse direction of the roster check: bus -> HUB_LOCAL_AGENTS.

    An agent registered as `<key>-<dev_device>` belongs to this machine by the
    suffix rule, yet if no listener here serves it every conversation addressed
    to it waits for ever — and the forward check still says "roster OK".
    On 2026-09-28 five `*-dev-linux` agents sat like that (issue #58): the
    ticket-manager conversation #272 waited six hours with nobody woken.
    Diagnosis only; without a dev_device there is no suffix to own.
    """
    if not cfg.dev_device:
        return
    suffix = f"-{cfg.dev_device}"
    unheard = sorted(
        name for name in known
        if name and name.endswith(suffix)
        and name not in configured and name != cfg.telegram_agent
    )
    if unheard:
        logger.warning(
            "bus agents UNHEARD on tenant '%s': %s registered with this device's "
            "suffix '%s' but not in HUB_LOCAL_AGENTS — conversations sent to them "
            "wake nobody. Add them (with their key in HUB_AGENT_KEYS) or retire them.",
            tenant, ", ".join(unheard), suffix,
        )


def owner_listen_tenants(config) -> list[str]:
    """Tenants to open the telegram-owner stream on, one per bus identity.

    The bus authenticates a stream by the agent's OWN key, not by the tenant
    key, and HUB_AGENT_KEYS is keyed by agent name only. With two tenants that
    both have an agent called like the telegram agent, every tenant resolved
    the same key: two streams on one identity (the bus showed it held twice by
    the same machine), each event consumed twice under two cursors, and the
    second tenant's identity heard by nobody. The first tenant keeps the key;
    the others are skipped and said, since listening on them was never real.
    """
    seen: dict[str, str] = {}
    out: list[str] = []
    for tenant in config.api_keys:
        key = config.agent_key(config.telegram_agent, tenant)
        if key and key in seen:
            logger.warning(
                "hub owner listener %s/%s skipped: its agent key is the one of "
                "%s/%s, so it would hold the same bus identity twice",
                tenant, config.telegram_agent, seen[key], config.telegram_agent,
            )
            continue
        if key:
            seen[key] = tenant
        out.append(tenant)
    return out


def start_owner_listeners() -> int:
    """Spawn the owner-only tasks (idempotent): the telegram-owner bus
    listener + overview snapshot loop + pipeline/migrate boot-resume.

    Gated on Telegram ownership: only the single owner posts into user topics,
    runs the snapshot loop and re-arms pending pipelines/migrations (leader
    only). For role 'worker' there is no human face, so this is a no-op.
    Returns the number of live owner SSE listener tasks (snapshot/resume loops
    are not counted).
    """
    config = _S["config"]
    if not config or not config.enabled:
        return 0
    if config.role == "worker":
        return 0  # workers have no telegram face / leader loops
    live = [t for t in _S["owner_tasks"] if not t.done()]
    if live:
        return len(live)  # already listening
    _S["owner_tasks"].clear()  # drop stale refs before rebuilding
    if config.a2a_webhook_enabled:
        # Webhook mode: the telegram agent receives via push, not SSE. The
        # snapshot/resume loops below still run (they are not event listeners).
        logger.info("hub owner SSE listener skipped (A2A webhook mode)")
    else:
        for tenant in owner_listen_tenants(config):
            t = asyncio.create_task(_listen(tenant, config.telegram_agent))
            _S["owner_tasks"].append(t)
    # Overview snapshot task: separate ref so it doesn't affect the SSE count.
    snap = _S.get("snap_task")
    if snap is None or snap.done():
        _S["snap_task"] = asyncio.create_task(_snapshot_loop())
    # Boot resume: re-arm pending pipelines/migrations once per ownership
    # acquire. The owner is the single leader, so this fires on exactly one node.
    _S["resume_task"] = asyncio.create_task(pipeline.resume_pending())
    _S["migrate_resume_task"] = asyncio.create_task(migrate.resume_pending())
    _S["restructure_resume_task"] = asyncio.create_task(restructure.resume_pending())
    logger.info("hub owner listeners started: %d + snapshot loop", len(_S["owner_tasks"]))
    return len(_S["owner_tasks"])


def start_listeners() -> int:
    """Spawn BOTH listener groups (local + owner) and return the total SSE
    listener count. Convenience entry point for single-node startup; the
    snapshot loop is not counted (matches the historical contract)."""
    return start_local_listeners() + start_owner_listeners()


async def _cancel_and_await(tasks: list) -> None:
    """Cancel the given tasks and await their cleanup so httpx stream context
    managers close before a potential immediate restart."""
    for t in tasks:
        t.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


async def stop_owner_listeners() -> None:
    """Cancel ONLY the owner tasks — the telegram-owner listener, the
    snapshot loop and the boot-resume tasks — and wait for cleanup (idempotent).

    Leaves the local listeners alive: the agents this node hosts keep being
    woken even after we yield Telegram ownership. Cursors persist per event in
    _listen, so a later start_owner_listeners() resumes where we left off.
    """
    tasks = list(_S["owner_tasks"])
    _S["owner_tasks"].clear()
    snap = _S.get("snap_task")
    if snap and not snap.done():
        tasks.append(snap)
    _S["snap_task"] = None
    for key in ("resume_task", "migrate_resume_task", "restructure_resume_task"):
        t = _S.get(key)
        if t and not t.done():
            tasks.append(t)
        _S[key] = None
    await _cancel_and_await(tasks)
    logger.info("hub owner listeners stopped")


async def stop_listeners() -> None:
    """Cancel ALL SSE listener tasks (local + owner) + the owner loops and wait
    for cleanup (idempotent). Used for full process shutdown.

    Cursors are persisted per event in _listen, so a later start_*_listeners()
    resumes where we left off. Awaiting the cancelled tasks lets the httpx
    stream context managers close before a potential immediate restart.
    """
    tasks = _all_listener_tasks()
    _S["local_tasks"].clear()
    _S["owner_tasks"].clear()
    snap = _S.get("snap_task")
    if snap and not snap.done():
        tasks.append(snap)
    _S["snap_task"] = None
    status_t = _S.get("status_task")
    if status_t and not status_t.done():
        tasks.append(status_t)
    _S["status_task"] = None
    for key in ("resume_task", "migrate_resume_task", "restructure_resume_task"):
        t = _S.get(key)
        if t and not t.done():
            tasks.append(t)
        _S[key] = None
    await _cancel_and_await(tasks)
    logger.info("hub SSE listeners stopped")


async def _snapshot_loop() -> None:
    """Periodically push a schedule snapshot to Network and apply pause replies.

    Uses the first configured tenant + its telegram_agent as auth identity.
    Fail-safe: any push error is logged as a warning; the loop always continues.
    Interval: overview_snapshot.SNAPSHOT_INTERVAL_S (15 min default).
    """
    import scheduler as _sched_mod  # local import: avoids circular; scheduler may be None

    cfg = _S["config"]
    if not cfg or not cfg.api_keys:
        return
    # Pick the first tenant for the snapshot push auth
    tenant = next(iter(cfg.api_keys))
    api_key = cfg.api_keys[tenant]
    agent_id = cfg.telegram_agent

    while True:
        sched = _sched_mod._S.get("sched")
        # Its OWN key, like every other bus call. The snapshot endpoint used to
        # be the single exception (tenant-scoped, agent keys 422'd); the bus now
        # resolves the tenant from an agent key too, so the exception is gone.
        # The tenant key is still passed as the fallback auth_headers() uses
        # when this deployment has no per-agent key configured.
        await overview_snapshot.push_snapshot(
            cfg.hub_url, api_key, agent_id, sched, cfg.agent_key(agent_id, tenant)
        )
        await asyncio.sleep(overview_snapshot.SNAPSHOT_INTERVAL_S)


async def _status_report_loop() -> None:
    """Push this node's fleet status (git + services) to Network, on the LOCAL
    cadence — every device reports the workspaces IT hosts, independent of
    Telegram ownership (unlike _snapshot_loop, which is a single-leader concern).
    Network dedups per STATUS_DEVICE and the overview merges across devices.

    Uses the first tenant's creds + its local agent as the auth identity; the
    reported node keys are the Coordinator project keys behind the local agents.
    Fail-safe: push_status_report never raises, so the loop runs forever."""
    cfg = _S["config"]
    if not cfg or not cfg.api_keys:
        return
    tenant = next(iter(cfg.api_keys))
    api_key = cfg.api_keys[tenant]
    agent_id = next(iter(cfg.agents_for(tenant)), cfg.telegram_agent)
    # Report the nodes behind the local agents PLUS the Coordinator's own node:
    # the bus host runs here with a real git repo + containers, so the fleet view
    # should show its status too. "coordinator" is the canonical bus slug across
    # the network; harmlessly skipped on devices whose registry lacks that project.
    node_keys = sorted(set(cfg.local_agents.values()) | {"coordinator"})
    while True:
        await status_report.push_status_report(
            cfg.hub_url, api_key, agent_id, node_keys,
            agent_key=cfg.agent_key(agent_id, tenant),
        )
        await asyncio.sleep(status_report.STATUS_REPORT_INTERVAL_S)


async def _run_event(tenant, agent_id, ev_type, data, ev_id, tracker) -> None:
    """Concurrent single-event handler: serialized per conversation, with
    low-water-mark bookkeeping on the tracker. The global bound is NOT here but
    around the claude run (``wake_pool.slot``): held here, three long runs kept
    every other agent's events from even being looked at (#74)."""
    conv_id = data.get("conversation_id")
    async with wake_pool.guard((tenant, conv_id)):
        try:
            await _dispatch(tenant, agent_id, ev_type, data)
        except OutcomeUnknown:
            tracker.on_unknown(ev_id)
            return
        except Exception:
            # Generic failure is log-and-ADVANCE (on_done below): only OutcomeUnknown
            # (bus unreachable) is retry-safe and holds the cursor. Matches the serial
            # path's historical behaviour — a logged dispatch bug is not redelivered.
            logger.exception("hub dispatch failed (%s/%s)", tenant, agent_id)
        tracker.on_done(ev_id)


# Vive in hub_client, accanto al trasporto che produce l'errore, perche' anche
# hub_watch.py deve porsi la stessa domanda e non puo' importare questo modulo.
_auth_reject_status = hub_client.auth_reject_status


def dead_listeners() -> list[dict]:
    """Listener rinunciatari (identita' rifiutata dal bus), per la diagnosi."""
    return sorted(_S["dead_listeners"].values(), key=lambda d: (d["tenant"], d["agent"]))


async def _announce_dead_listener(tenant: str, agent_id: str, status: int) -> None:
    """Dillo alla persona, non solo al log: un listener morto e' invisibile."""
    send_to = _S.get("send_to")
    if send_to is None:
        return
    project = _S["config"].local_agents.get(agent_id, "?") if _S["config"] else "?"
    text = (
        f"\u26d4 Listener bus spento: <code>{html.escape(tenant)}/{html.escape(agent_id)}</code> "
        f"(progetto <b>{html.escape(str(project))}</b>)\n"
        f"Il bus lo rifiuta con {status} da {AUTH_REJECTS_BEFORE_GIVING_UP} tentativi "
        f"consecutivi: la credenziale e' sbagliata o l'identita' e' stata ritirata dal "
        f"roster. Ho smesso di ritentare.\n"
        f"Da fare: togliere l'agente da <code>BOT_LOCAL_AGENTS</code>/"
        f"<code>HUB_AGENT_KEYS</code>, oppure allineare la chiave e riavviare."
    )
    try:
        import profiles

        for uid in profiles.admin_user_ids():
            await send_to(uid, text)
    except Exception:
        logger.exception("dead-listener alert failed for %s/%s", tenant, agent_id)


async def _consume(tenant, agent_id, is_telegram, events, start_cursor) -> int:
    """Process one SSE connection's events. The telegram listener stays serial
    (await each dispatch, advance the cursor per event) so #7/#8 verdict barriers
    keep their single-consumer guarantee. Local-agent listeners run wakes
    concurrently (per-conv serialized, bounded) and advance a low-water-mark
    cursor. Returns the number of events seen. In-flight tasks are drained before
    return so the cursor settles. Stream errors are caught and logged.
    On OutcomeUnknown a wake sets tracker.reconnect_needed; the loop checks it
    after each event, so on a fully-idle stream the reconnect waits for the next
    event or the SSE read-timeout. No loss (the held low-water-mark cursor
    guarantees redelivery) — latency only."""
    got = 0
    if is_telegram:
        try:
            async for ev_type, data, ev_id in events:
                got += 1
                try:
                    await _dispatch(tenant, agent_id, ev_type, data)
                except OutcomeUnknown:
                    logger.warning(
                        "hub %s/%s: outcome unknown, retry from cursor", tenant, agent_id
                    )
                    return got
                except Exception:
                    logger.exception("hub dispatch failed (%s/%s)", tenant, agent_id)
                hub_store.set_cursor(tenant, agent_id, ev_id)
        except Exception as exc:
            if _auth_reject_status(exc):
                raise  # non e' un guasto di stream: lo decide _listen
            logger.warning("hub SSE %s/%s stream error: %s", tenant, agent_id, exc)
        return got

    tracker = wake_pool.Tracker(start_cursor, lambda c: hub_store.set_cursor(tenant, agent_id, c))
    inflight: set = set()
    try:
        async for ev_type, data, ev_id in events:
            got += 1
            tracker.on_received(ev_id)
            t = asyncio.create_task(_run_event(tenant, agent_id, ev_type, data, ev_id, tracker))
            inflight.add(t)
            t.add_done_callback(inflight.discard)
            if tracker.reconnect_needed:
                break
    except Exception as exc:
        if _auth_reject_status(exc):
            if inflight:
                await asyncio.gather(*inflight, return_exceptions=True)
            raise  # non e' un guasto di stream: lo decide _listen
        logger.warning("hub SSE %s/%s stream error: %s", tenant, agent_id, exc)
    finally:
        if inflight:
            await asyncio.gather(*inflight, return_exceptions=True)
    return got


async def _listen(tenant: str, agent_id: str) -> None:
    """Reconnecting SSE loop for one (tenant, agent). Delegates one connection's
    events to _consume; cursors persist per the low-water-mark (local) or per
    event (telegram), so a reconnect resumes without loss."""
    cfg = _S["config"]
    is_telegram = agent_id == cfg.telegram_agent
    # Ripartire cancella il certificato di morte: se la config e' stata corretta
    # e il listener riparte, il vecchio verdetto non deve restare appeso.
    _S["dead_listeners"].pop((tenant, agent_id), None)
    backoff = 1
    auth_rejects = 0
    dichiarato = False
    while True:
        try:
            # Drop stale state if the bus DB was rebuilt (runs once per tenant,
            # retried here on reconnect if the bus was unreachable). MUST precede
            # get_cursor so a cleared cursor forces full redelivery.
            await _ensure_bus_epoch(tenant)
            cursor = hub_store.get_cursor(tenant, agent_id)
            if not dichiarato:
                # Dire al bus chi ascolta per questo agente, una volta. Finche'
                # nessuno lo diceva, le identita' tenute da questo processo
                # risultavano `unowned` — tenute da qualcuno di cui nessuno
                # risponde — e una sovrapposizione con un altro dispatcher
                # (due risposte alla stessa domanda) non era visibile a nessuno
                # dei due (issue #37). Best-effort: un bus che non risponde non
                # deve impedire l'ascolto.
                dichiarato = await hub_client.dichiara_proprietario(
                    cfg.hub_url,
                    cfg.api_keys[tenant],
                    agent_id,
                    cfg.agent_key(agent_id, tenant),
                )
            events = hub_client.stream_events(
                cfg.hub_url,
                cfg.api_keys[tenant],
                agent_id,
                last_event_id=cursor,
                agent_key=cfg.agent_key(agent_id, tenant),
            )
            got = await _consume(tenant, agent_id, is_telegram, events, cursor)
            # Lo stream si e' aperto: qualunque rifiuto precedente e' passato.
            auth_rejects = 0
            if got:
                backoff = 1
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            status = _auth_reject_status(exc)
            if status is None:
                auth_rejects = 0
                logger.warning("hub SSE %s/%s lost: %s", tenant, agent_id, exc)
            else:
                auth_rejects += 1
                logger.warning(
                    "hub SSE %s/%s rifiutato (%s), tentativo %d/%d",
                    tenant, agent_id, status, auth_rejects,
                    AUTH_REJECTS_BEFORE_GIVING_UP,
                )
                if auth_rejects >= AUTH_REJECTS_BEFORE_GIVING_UP:
                    _S["dead_listeners"][(tenant, agent_id)] = {
                        "tenant": tenant,
                        "agent": agent_id,
                        "project": cfg.local_agents.get(agent_id, ""),
                        "status": status,
                        "rejects": auth_rejects,
                        "since": _dt.datetime.now(_dt.timezone.utc).isoformat(),
                    }
                    logger.error(
                        "hub SSE %s/%s ABBANDONATO: %d rifiuti %s consecutivi. La "
                        "credenziale non e' valida o l'identita' non e' piu' nel "
                        "roster del bus — nessun retry puo' risolverlo. Togli "
                        "l'agente da BOT_LOCAL_AGENTS/HUB_AGENT_KEYS oppure "
                        "allinea la chiave e riavvia.",
                        tenant, agent_id, auth_rejects, status,
                    )
                    await _announce_dead_listener(tenant, agent_id, status)
                    return
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, 30)


# Una notizia sola, detta una volta. La chiave e' il processo, non la
# conversazione: e' il processo a non essere autenticato.
_CLI_NON_AUTENTICATA: dict[str, object] = {}


def cli_non_autenticata() -> dict | None:
    """Il verdetto "questa CLI non e' autenticata", se e' stato emesso."""
    return _CLI_NON_AUTENTICATA.get("verdetto")  # type: ignore[return-value]


async def _post_auto_notice(tenant: str, agent_id: str, conv_id: int, text: str) -> bool:
    """Post a needs_human notice as `agent_id`, at most ONE per conversation
    until a real message arrives (#59). Returns True if it was posted.

    The cap is the second fence behind the wake filter: even if a notice were
    to wake someone, the conversation cannot fill up with them.
    """
    if hub_store.auto_notice_posted(conv_id):
        logger.info(
            "hub: #%s already carries an automatic notice with nothing real after "
            "it -- not posting another as %s",
            conv_id,
            agent_id,
        )
        return False
    await _client(tenant, agent_id).send_message(
        conv_id, _auto_notice(text), needs_human=True
    )
    hub_store.mark_auto_notice(conv_id)
    return True


async def _segnala_cli_non_autenticata(
    tenant: str, agent_id: str, conv_id: int, text: str
) -> None:
    """Dillo forte, dillo una volta, e dillo a chi puo' ripararlo.

    Il difetto che ha reso invisibile #34 per tre mesi non e' che mancasse un
    avviso: e' che l'unico avviso era un `needs_human` scritto dentro la
    conversazione stallata, cioe' nel posto dove va a finire chi gia' non
    riceve risposta. Qui il verdetto e' di processo, si registra una volta, e
    prova a uscire dal bus: verso Telegram se questo Coordinator ce l'ha (il
    bot), altrimenti con un solo `needs_human`, che e' il canale che un worker
    headless possiede.
    """
    gia_detto = bool(_CLI_NON_AUTENTICATA.get("verdetto"))
    _CLI_NON_AUTENTICATA["verdetto"] = {
        "tenant": tenant,
        "agent": agent_id,
        "conversation_id": conv_id,
        "since": _dt.datetime.now(_dt.timezone.utc).isoformat(),
        "detail": text.replace("\n", " ")[:300],
    }
    if gia_detto:
        logger.warning(
            "hub wake: #%s non eseguito, la CLI non e' autenticata (gia' segnalato)",
            conv_id,
        )
        return
    logger.error(
        "LA CLI DI QUESTO PROCESSO NON E' AUTENTICATA: nessun agente qui puo' "
        "rispondere, e nessun tentativo lo cambiera'. Primo caso sulla "
        "conversazione #%s (%s/%s). Dettaglio: %s. Rimedio: `claude /login` "
        "dentro questo container.",
        conv_id, tenant, agent_id, _CLI_NON_AUTENTICATA["verdetto"]["detail"],
    )
    testo = (
        f"\U0001f512 La CLI di questo Coordinator non e' autenticata: nessuno "
        f"degli agenti che ospita puo' rispondere sul bus, e non e' un guasto "
        f"che passa da solo.\nPrimo caso: #{conv_id} ({tenant}/{agent_id}).\n"
        f"Serve un `claude /login` interattivo su questo host."
    )
    send_to = _S.get("send_to")
    if send_to is not None:
        try:
            import profiles

            for uid in profiles.admin_user_ids():
                await send_to(uid, testo)
            return
        except Exception:
            logger.exception("avviso CLI non autenticata: invio Telegram fallito")
    try:
        await _post_auto_notice(tenant, agent_id, conv_id, testo)
    except Exception:
        logger.exception("avviso CLI non autenticata: needs_human fallito")


async def _dispatch(tenant: str, agent_id: str, ev_type: str, data: dict) -> None:
    """Route an SSE event. Filled in by later tasks."""
    cfg = _S["config"]
    if agent_id == cfg.telegram_agent:
        await _handle_human_event(tenant, ev_type, data)
    elif agent_id in cfg.local_agents:
        await _wake_local_agent(tenant, agent_id, ev_type, data)


# ---------------------------------------------------------------------------
# A2A push webhook (inbound) — alternative to the SSE listener
# ---------------------------------------------------------------------------
def _webhook_to_bus(evt: dict) -> tuple[str | None, dict]:
    """Map an inbound A2A status-update (Network push) back to a bus event.

    Network enriches the A2A event `metadata` with the original bus fields
    (busEvent, from, needsHuman, subject, priority, messageId), so the same
    _dispatch path the SSE listener uses can consume it unchanged.
    """
    meta = evt.get("metadata") or {}
    task_id = evt.get("taskId") or evt.get("contextId")
    try:
        conv_id = int(task_id)
    except (TypeError, ValueError):
        return None, {}
    status = evt.get("status") or {}
    msg = status.get("message") or {}
    body = "".join(p.get("text", "") for p in msg.get("parts", []) if p.get("kind") == "text")
    ev_type = meta.get("busEvent") or ("status_changed" if evt.get("final") else "new_message")
    data = {
        "conversation_id": conv_id,
        "from_agent": meta.get("from") or (msg.get("metadata") or {}).get("from"),
        "body": body,
        "needs_human": bool(meta.get("needsHuman", False)),
        "subject": meta.get("subject"),
        "priority": meta.get("priority"),
        "message_id": meta.get("messageId"),
        "new_status": meta.get("newStatus") or status.get("state"),
        "changed_by": meta.get("changedBy"),
    }
    return ev_type, data


async def dispatch_webhook_event(tenant: str, agent_id: str, evt: dict) -> None:
    """Entry point for the A2A webhook receiver: map + route like an SSE event."""
    if evt.get("type") == "task.due":
        scheduled_task_id = evt.get("scheduled_task_id")
        phase = evt.get("phase")
        dedup_key = (scheduled_task_id, phase)
        # Dedup check happens up-front (unchanged): a (scheduled_task_id, phase)
        # pair already marked as delivered is skipped outright, handler not
        # invoked. The None-guard stays on scheduled_task_id only — a missing
        # phase is still a valid part of the key.
        if scheduled_task_id is not None and dedup_key in _seen_task_due:
            logger.debug(
                "task.due dedup: skipping scheduled_task_id=%s phase=%s",
                scheduled_task_id,
                phase,
            )
            return
        if _task_due_handler is None:
            logger.warning("task.due received but no handler registered: %s", evt)
            return
        # Mark AFTER the handler returns successfully, not before: if rendering/
        # dispatch raises, a redelivery of the same event must still get a
        # chance to run (see task-6 brief). A None id is never inserted — it
        # would otherwise make every malformed event dedup against every other.
        await _task_due_handler(evt)
        if scheduled_task_id is not None:
            _seen_task_due.add(dedup_key)
        return
    ev_type, data = _webhook_to_bus(evt)
    if ev_type is None:
        logger.warning("a2a webhook: undecodable event for %s/%s", tenant, agent_id)
        return
    await _dispatch(tenant, agent_id, ev_type, data)


def _push_targets(cfg) -> list[tuple[str, str]]:
    """(tenant, agent) pairs this process would register a push config for."""
    out = []
    for tenant in cfg.api_keys:
        agents = list(cfg.agents_for(tenant))
        agents.append(cfg.telegram_agent)
        out.extend((tenant, agent) for agent in agents)
    return out


async def register_push_webhooks() -> int:
    """Register an agent-level push config per listened agent (idempotent by
    config_id).

    OWNER-ONLY, wired to ownership acquire (see bot.py). A push config does not
    replace the SSE delivery, it is delivered *in addition* to it (Network
    emit(): the SSE queues are served first, the webhook callbacks after). So a
    STANDBY device holding a push config for a shared identity — `telegram-owner`
    is the same agent on both peers — would receive every escalation a second
    time and post a duplicate card next to the owner's. Registering only while
    polling keeps exactly one device answering, which is the same rule that
    governs Telegram itself.
    """
    cfg = _S["config"]
    if not cfg or not cfg.a2a_webhook_enabled:
        return 0
    if cfg.role == "worker":
        # The receiver is mounted on api.app, which only bot.py serves. A worker
        # registering push configs would tell Network to deliver its events to a
        # URL nothing answers on — worse than SSE, because Network would then
        # consider those agents reachable and stop warning about undelivered
        # messages. Workers stay on SSE (see start_local_listeners).
        logger.warning(
            "push webhook registration refused for role=worker: no receiver in "
            "this process. The worker listens over SSE instead."
        )
        return 0
    base = cfg.a2a_webhook_url.rstrip("/")
    token = cfg.a2a_webhook_token or None
    n = 0
    for tenant, agent in _push_targets(cfg):
        url = f"{base}/a2a/webhook/{tenant}/{agent}"
        try:
            await _a2a_client(tenant, agent).set_push_config(
                agent, url, token=token, config_id=f"coordinator-{agent}"
            )
            n += 1
        except Exception:
            logger.exception("push webhook registration failed: %s/%s", tenant, agent)
    logger.info("a2a push webhooks registered: %d", n)
    return n


async def unregister_push_webhooks() -> int:
    """Drop this device's push configs (ownership release, see bot.py).

    Leaving them behind is not inert: Network keeps posting every event to a
    device that is no longer the Telegram face, so the escalation is handled
    twice — and, because a push config counts as a delivery path, the bus also
    stops warning that nobody heard a message. Failures are logged and skipped:
    a peer that cannot reach the bus while standing down must still stand down.
    """
    cfg = _S["config"]
    if not cfg or not cfg.a2a_webhook_enabled:
        return 0
    n = 0
    for tenant, agent in _push_targets(cfg):
        try:
            await _a2a_client(tenant, agent).delete_push_config(
                agent, f"coordinator-{agent}"
            )
            n += 1
        except a2a_client.A2AError as exc:
            # "not found" is the ordinary case, not a fault: this runs at every
            # boot and every release, and most of the time there is nothing to
            # drop. Only a real bus failure deserves a stack trace.
            if "not found" in (exc.message or "").lower():
                logger.debug("push webhook already absent: %s/%s", tenant, agent)
            else:
                logger.exception("push webhook removal failed: %s/%s", tenant, agent)
        except Exception:
            logger.exception("push webhook removal failed: %s/%s", tenant, agent)
    logger.info("a2a push webhooks removed: %d", n)
    return n


async def _create_forum_topic_retrying(chat_id: int, title: str, *, attempts: int = 6):
    """create_forum_topic with RetryAfter (flood-control) backoff.

    Telegram heavily rate-limits forum-topic creation. Without this, a RetryAfter
    propagates up to _listen, which has already advanced the SSE cursor -> the
    escalation is logged-and-dropped forever (silent loss). We honour the
    server-mandated delay and retry; only the last attempt is allowed to raise.
    """
    from telegram.error import RetryAfter

    for attempt in range(1, attempts + 1):
        try:
            return await _S["bot"].create_forum_topic(chat_id=chat_id, name=title)
        except RetryAfter as exc:
            if attempt == attempts:
                raise
            wait = float(getattr(exc, "retry_after", 5)) + 1.0
            logger.warning(
                "create_forum_topic flood-controlled, retry in %.0fs (attempt %d/%d)",
                wait,
                attempt,
                attempts,
            )
            await asyncio.sleep(wait)


async def _ensure_topic(
    tenant: str, conv_id: int, subject: str, counterpart: str | None = None
) -> tuple[int, int]:
    """Return (chat_id, topic_id) for a conversation, creating the topic once.

    `counterpart` (the escalating agent) is persisted so a later A2A reply can
    address it; it is also backfilled onto an existing record if missing.
    """
    rec = hub_store.get_conv(conv_id)
    if rec and rec.get("chat_id") and rec.get("topic_id"):
        if counterpart and not rec.get("counterpart"):
            hub_store.upsert_conv(
                conv_id,
                tenant=tenant,
                chat_id=rec["chat_id"],
                topic_id=rec["topic_id"],
                counterpart=counterpart,
            )
        return rec["chat_id"], rec["topic_id"]
    cfg = _S["config"]
    chat_id = cfg.telegram_chat_id
    title = f"\U0001f6f0 #{conv_id} · {subject}"[:128]
    topic = await _create_forum_topic_retrying(chat_id, title)
    topic_id = topic.message_thread_id
    hub_store.upsert_conv(
        conv_id, tenant=tenant, chat_id=chat_id, topic_id=topic_id, counterpart=counterpart
    )
    return chat_id, topic_id


async def _open_pipeline_topic(pid: int, title: str) -> tuple[int, int]:
    """Create a Telegram forum topic for a pipeline (flood-control aware)."""
    cfg = _S["config"]
    chat_id = cfg.telegram_chat_id
    topic = await _create_forum_topic_retrying(chat_id, title[:128])
    return chat_id, topic.message_thread_id


# A forwarded agent reply is a heads-up, not the transcript: the link opens the rest.
FORWARD_BODY_MAX = int(os.environ.get("HUB_FORWARD_BODY_MAX", "600"))


def _conv_link(conv_id: int) -> str:
    """Trailing link to the conversation on the Network UI, or "" if unset (#66).
    The URL is also the anchor text, so the plain-text fallback keeps it."""
    base = getattr(_S.get("config"), "network_ui_url", "")
    if not base:
        return ""
    url = html.escape(f"{base}/conversations/{conv_id}")
    return f'\n\U0001f517 <a href="{url}">{url}</a>'


def _truncate(text: str, limit: int = FORWARD_BODY_MAX) -> str:
    return text if len(text) <= limit else text[:limit].rstrip() + "\u2026"


# One Telegram message per conversation (Coordinator: "valanga di messaggi"):
# the card in the topic is EDITED for agent replies and for the closing, and
# re-posted (old one deleted) only for a new ask -- the one event that must
# notify. The card state lives in the conv record: ``card`` + ``card_msg_id``.
CARD_REPLIES_KEPT = 3
CARD_TEXT_MAX = 3800


def _render_card(conv_id: int, card: dict) -> str:
    lines = [
        f"\U0001f514 <b>{html.escape(card.get('ask_from') or 'agente')}</b> ha bisogno di te",
        html.escape(_truncate(card.get("ask") or "", 2000)),
    ]
    for r in card.get("replies") or []:
        lines.append(
            f"\U0001f4ac <b>{html.escape(r['from'])}</b>: {html.escape(_truncate(r['body']))}"
        )
    if card.get("closed_by"):
        lines.append(f"\u2705 Conversazione chiusa da <b>{html.escape(card['closed_by'])}</b>")
    return "\n".join(lines)[:CARD_TEXT_MAX] + _conv_link(conv_id)


async def _show_card(conv_id: int, card: dict, *, notify: bool) -> None:
    """Edit the conversation's card in place, or (notify / no card yet / edit
    failed) post it anew and delete the previous one. Never raises."""
    rec = hub_store.get_conv(conv_id) or {}
    chat_id, topic_id = rec.get("chat_id"), rec.get("topic_id")
    if chat_id is None or topic_id is None:
        return
    bot = _S["bot"]
    text = _render_card(conv_id, card)
    old = rec.get("card_msg_id")
    msg_id = None
    if old and not notify:
        try:
            await bot.edit_message_text(
                text, chat_id=chat_id, message_id=old,
                parse_mode="HTML", disable_web_page_preview=True,
            )
            msg_id = old
        except Exception as exc:
            if "not modified" in str(exc).lower():
                msg_id = old
            else:
                logger.warning("card edit failed for #%s (%s), reposting", conv_id, exc)
    if msg_id is None:
        try:
            sent = await bot.send_message(
                chat_id, text, message_thread_id=topic_id,
                parse_mode="HTML", disable_web_page_preview=True,
            )
            if sent is None:
                logger.warning("card for #%s not sent (dropped by message guard)", conv_id)
                hub_store.set_conv_fields(conv_id, card=card)
                return
            msg_id = sent.message_id
        except Exception:
            logger.exception("card post failed for #%s", conv_id)
            hub_store.set_conv_fields(conv_id, card=card)
            return
        if old and old != msg_id:
            try:
                await bot.delete_message(chat_id=chat_id, message_id=old)
            except Exception as exc:
                logger.debug("old card %s for #%s not deleted: %s", old, conv_id, exc)
    hub_store.set_conv_fields(conv_id, card=card, card_msg_id=msg_id)


async def _forward_agent_reply(conv_id: int, data: dict) -> None:
    """After the human answered from Telegram, the agents' replies on that
    conversation reach the topic too, even without needs_human (#66): otherwise
    an answer given from Telegram gets no visible acknowledgement at all. They
    go into the conversation's card (edited, silent), not as new messages."""
    cfg = _S["config"]
    frm = data.get("from_agent") or ""
    if not frm or frm == cfg.telegram_agent:
        return  # our own echo
    raw_body = (data.get("body") or "").strip()
    if not raw_body or is_auto_notice(raw_body):
        return
    if not hub_store.human_replied(conv_id):
        return
    rec = hub_store.get_conv(conv_id) or {}
    if rec.get("chat_id") is None or rec.get("topic_id") is None:
        return
    card = dict(rec.get("card") or {})
    replies = list(card.get("replies") or []) + [{"from": frm, "body": raw_body}]
    card["replies"] = replies[-CARD_REPLIES_KEPT:]
    await _show_card(conv_id, card, notify=False)


async def _handle_human_event(tenant: str, ev_type: str, data: dict) -> None:
    if ev_type == "new_message":
        conv_id = data.get("conversation_id")
        msg_id = data.get("message_id")
        if conv_id is not None and msg_id is not None:
            try:
                if await pipeline.on_verdict(int(conv_id), int(msg_id)):
                    return  # belonged to a pipeline
                if await migrate.on_verdict(int(conv_id), int(msg_id)):
                    return  # belonged to a migration
                if await restructure.on_verdict(int(conv_id), int(msg_id)):
                    return  # belonged to a restructure
            except Exception:
                logger.exception("verdict routing failed for #%s", conv_id)
    if ev_type == "new_message" and data.get("conversation_id") is not None:
        try:
            if await _deliver_remote_turn_reply(int(data["conversation_id"]), data):
                return  # a remote turn (#83, F3): its answer went back to the chat
        except Exception:
            logger.exception("remote turn routing failed")
    if ev_type == "status_changed":
        return await _handle_status(tenant, data)  # Task 10
    if ev_type not in ("new_message", "conversation_created"):
        return
    if not data.get("needs_human"):
        # Default-deny, with one exception: once the human answered on this
        # conversation, the agents' follow-ups are forwarded (#66).
        if ev_type == "new_message" and data.get("conversation_id") is not None:
            await _forward_agent_reply(int(data["conversation_id"]), data)
        return
    conv_id = int(data["conversation_id"])
    raw_body = (data.get("body") or "").strip()
    # Open a forum topic ONLY for a genuine ask: a needs_human event carrying a
    # real message. A bare/empty-body escalation (an informational ping or a
    # status echo that happens to set needs_human) would spawn a topic with
    # nothing for the human to reply to -> pure clutter. Same for an empty
    # follow-up in an existing topic: the card stays as it is.
    if not raw_body:
        return
    subject = data.get("subject") or "(senza oggetto)"
    await _ensure_topic(tenant, conv_id, subject, counterpart=data.get("from_agent"))
    # A new ask replaces the card: posted anew so it notifies, the old one deleted.
    card = {"ask_from": data.get("from_agent") or "agente", "ask": raw_body, "replies": []}
    await _show_card(conv_id, card, notify=True)


async def _handle_status(tenant: str, data: dict) -> None:
    if data.get("new_status") != "resolved":
        return
    conv_id = int(data["conversation_id"])
    rec = hub_store.get_conv(conv_id)
    # rec may exist from a wake (session_id/turns) yet have NO telegram topic:
    # chat_id/topic_id are added only by _ensure_topic. Without them there is no
    # thread to post into -> nothing to close out (avoid KeyError on resolve).
    chat_id = (rec or {}).get("chat_id")
    topic_id = (rec or {}).get("topic_id")
    if chat_id is None or topic_id is None:
        return
    # Resolved = the human no longer needs to act here: say who closed it, with
    # the link (#66 — a silent archive left the owner wondering whether his answer
    # had been taken), then archive the topic. Fail-safe: a send/close error is
    # logged, never raised (cursor still advances).
    # The closing goes into the card (edited, silent), not a new message.
    card = dict(rec.get("card") or {})
    card["closed_by"] = data.get("changed_by") or "un agente"
    try:
        await _show_card(conv_id, card, notify=False)
    except Exception:
        logger.exception("resolve notice failed for #%s", conv_id)
    try:
        await _S["bot"].close_forum_topic(chat_id=chat_id, message_thread_id=topic_id)
    except Exception:
        logger.exception("close_forum_topic on resolve failed for #%s", conv_id)


async def handle_topic_reply(chat_id: int, topic_id: int, text: str) -> bool:
    """If (chat,topic) maps to a hub conversation, post the reply to the bus as
    the telegram agent and return True. Otherwise return False (not ours)."""
    cfg = _S["config"]
    if not cfg or chat_id != cfg.telegram_chat_id:
        return False
    # A blocked migration's topic isn't a hub conversation — its human decision
    # (procedi/no) is routed to the migration coordinator, which resumes or aborts.
    try:
        if await migrate.on_human_decision(chat_id, topic_id, text):
            return True
    except Exception:
        logger.exception(
            "migration human-decision routing failed for topic %s/%s", chat_id, topic_id
        )
    # A blocked pipeline's topic isn't a hub conversation either — its human
    # decision (procedi/no) is routed to the pipeline orchestrator, which
    # resumes the blocked stage or aborts (mirror of the migrate branch above).
    try:
        if await pipeline.on_human_decision(chat_id, topic_id, text):
            return True
    except Exception:
        logger.exception(
            "pipeline human-decision routing failed for topic %s/%s", chat_id, topic_id
        )
    try:
        if await restructure.on_human_decision(chat_id, topic_id, text):
            return True
    except Exception:
        logger.exception(
            "restructure human-decision routing failed for topic %s/%s", chat_id, topic_id
        )
    conv_id = hub_store.conv_id_for_topic(chat_id, topic_id)
    if conv_id is None:
        return False
    rec = hub_store.get_conv(conv_id)
    tenant = rec["tenant"]
    counterpart = rec.get("counterpart")
    # A2A append needs a valid participant to address (POST /a2a/agents/{name});
    # fall back to the custom REST send when A2A is off or the counterpart is
    # unknown (legacy records predating counterpart persistence).
    if cfg.use_a2a and counterpart:
        await _a2a_client(tenant, cfg.telegram_agent).send_message(
            counterpart, text, task_id=conv_id
        )
    else:
        await _client(tenant, cfg.telegram_agent).send_message(conv_id, text, needs_human=False)
    hub_store.mark_human_replied(conv_id)
    return True


def wake_host_cwd(project) -> str | None:
    """The host path a bus wake should run in, or ``None`` to stay in-container.

    Only when host execution is configured (``HOST_EXEC_SSH``) AND the project
    has a host path on this device — a node not cloned here has nothing to run
    in on the host, and silently falling back is better than a wake that dies
    on a missing directory.
    """
    if not claude_runner.host_exec_enabled():
        return None
    return (getattr(project, "host_path", "") or "").strip() or None


_WAKE_CAUTION = """
⚠️ ATTENZIONE — sessione fantasma sospetta: risulta una sessione interattiva presente
su questo agente, ma NESSUN watcher del bus è realmente in ascolto (il claim non è
stato rinfrescato da hub_watch.py). Potresti non avere il contesto reale per cui
questa conversazione è stata aperta. NON risolvere la conversazione con sicurezza o
cortesia se non sei certo di aver capito la richiesta originale: preferisci
needs_human:true chiedendo conferma del contesto mancante, oppure lascia la
conversazione aperta, piuttosto che chiuderla con un messaggio generico.
"""

# Where the woken session runs decides what it can take for granted, so the
# prompt says it instead of letting the model guess from the global CLAUDE.md.
_WAKE_ENV_CONTAINER = """Ambiente di esecuzione (IMPORTANTE — non dedurlo dal CLAUDE.md globale):
- Giri come worker headless DENTRO un container del Coordinator, sull'host che ospita i repo: sei GIÀ su quel server. Non ti serve SSH per raggiungerlo né per fare un deploy "sul server" — il server è questo.
- La tua cwd /workspaces/<proj> è un bind-mount RW del repo reale dell'host (es. /workspaces/network == il repo Network dell'host). git, edit e build agiscono direttamente sul repo di produzione.
- NON hai docker qui dentro (niente docker.sock, #89), né chiavi SSH né cache Microsoft: build, deploy e ricreazioni si fanno dall'host. Se il lavoro li richiede, fai la parte di codice e chiedi il deploy con needs_human.
- Fa eccezione ogni `.venv`: qui dentro è una cartella SOLO del container, separata da quella dell'host (issue #47). Se è vuota o rotta ricreala pure; non cercare di riparare o ricreare il venv dell'host da qui."""

# Host runs (#46): the session IS a terminal session on this device, so the
# only thing worth saying is that — and that it holds for credentials too.
_WAKE_ENV_HOST = """Ambiente di esecuzione:
- Giri sull'host, come l'utente del device, nella cwd reale del repo ({cwd}): è la stessa sessione che si aprirebbe da terminale. `gh`, `git push`, `docker`, i path di `~/.claude/paths.sh` e la memoria di progetto sono quelli del terminale."""

# Line the woken worker prints when it decides the message needs no reply (#61).
# Without it a silent run is indistinguishable from a failed one.
NO_REPLY_MARKER = "[[NESSUNA_RISPOSTA]]"


def chose_not_to_reply(text: str | None) -> bool:
    """True iff the run's output declares a deliberate no-reply."""
    return bool(text) and NO_REPLY_MARKER in text


# Second attempt after a timeout (#70): the SAME session is resumed, so it
# already holds the original request -- it only needs to know it was cut off.
_WAKE_RESUME_PROMPT = """Sei stato interrotto per tempo dopo {minutes} minuti mentre lavoravi alla conversazione #{conv_id} su Network.
Riprendi da dove eri: guarda `git status` e `git diff` per ritrovare il lavoro già fatto, non ricominciare da capo.
Quando hai finito, rispondi nella conversazione #{conv_id} con la skill /hub-send come da istruzioni iniziali (oppure termina con la riga {no_reply} se non serve una risposta)."""


_WAKE_PROMPT = """Hai ricevuto un messaggio su Network (conversazione #{conv_id}: "{subject}").
Da: {frm}
Messaggio: "{body}"

{environment}
- Prima di dichiarare "non posso / non ho accesso", PROVA il comando. Escala a needs_human SOLO per una vera decisione o autorizzazione umana (azione irreversibile, GDPR, identità non verificabile) — MAI per un limite d'accesso che non hai verificato.
{caution}
Istruzioni:
1. Se hai abbastanza contesto, esegui ciò che serve e rispondi nel bus con la skill /hub-send.
2. Se NON ti basta, manda un messaggio con needs_human:true spiegando cosa ti serve.
3. Se NON puoi procedere perché serve una DECISIONE o AUTORIZZAZIONE umana (es. azione
   irreversibile, dati personali/GDPR, non puoi verificare l'identità di chi chiede), NON
   rifiutare con un messaggio normale: rispondi con un context tipato
   {{"escalation": {{"blocked": true, "reason": "...", "decision_needed": "..."}}}} e
   needs_human:true. Rifiutare in prosa senza segnale tipato è un errore di protocollo: la
   tua richiesta non raggiungerebbe the owner in modo agganciabile.
4. Se il messaggio che ti ha svegliato porta un comando TIPATO nel context — {{"pipeline": {{...}}}}
   (uno stage di pipeline), {{"migrate": {{...}}}} (una fase di migrazione) o
   {{"agents_restructure": {{...}}}} (ristrutturazione agenti) — dopo aver eseguito il
   lavoro DEVI rispondere con il verdetto tipato corrispondente ({{"pipeline_verdict": {{...}}}},
   {{"migrate_verdict": {{...}}}} oppure {{"restructure_verdict": {{...}}}}), riecheggiando l'`id`
   e lo `stage`/`phase` del comando. Un verdetto
   mancante o malformato è trattato come FALLIMENTO dall'orchestratore (mai un pass) → la pipeline/
   migrazione si interrompe. Il formato esatto è nella skill /hub-send.
5. Risolvi la conversazione SOLO se l'hai aperta tu: dal bus il `resolved` è permesso
   soltanto al creatore (o a un admin). Se sei il destinatario, rispondi e basta — un
   tentativo di chiuderla da destinatario riceve 403 e non fa altro che sporcare la
   conversazione. È chi l'ha aperta che la segue col proprio `/loop` e la chiude.

Regole di disciplina del bus (vincolanti):
- Rispondi SOLO in questa conversazione (#{conv_id}). NON aprire nuove conversazioni di
  ack, analisi o commento su conversazioni/messaggi di altri agenti: ogni messaggio
  sveglia tutti i partecipanti e genera rumore a catena.
- Se il messaggio non richiede un'azione DA TE, rispondi brevemente che non sei
  coinvolto — oppure non rispondere affatto. Non inoltrare, non fare da eco.
- Se scegli di NON rispondere (anche perché il messaggio chiude il discorso: una risposta
  conclusiva, un «ricevuto», un ringraziamento), termina il tuo output con la riga
  {no_reply} — senza quella riga il Coordinator legge il silenzio come «non ha
  risposto», ti risveglia e poi scomoda the owner per niente.
- Se la TUA risposta chiude il discorso (non chiede niente a nessuno), mandala con
  `hub_send.py reply <id> --body "..." --no-reply-needed`: il destinatario non viene
  nemmeno svegliato.
- needs_human è il canale verso the owner: non è un broadcast su cui intervenire e non
  va usato per girare la domanda ad altri agenti.
- Non parlare a nome di altri agenti e non rispondere a domande rivolte a loro."""


async def _fetch_conv_verify(tenant: str, agent_id: str, conv_id: int) -> dict:
    """get_conversation with one retry; raise OutcomeUnknown if both fail."""
    for attempt in (1, 2):
        try:
            return await _client(tenant, agent_id).get_conversation(conv_id)
        except Exception as exc:  # network/HTTP — outcome not confirmable
            if attempt == 2:
                logger.warning("verify fetch failed twice for #%s: %s", conv_id, exc)
                raise OutcomeUnknown(conv_id) from exc
            await asyncio.sleep(1)


async def _verify_outcome(tenant: str, agent_id: str, conv_id: int, since: int) -> bool:
    """Bus-authoritative: True iff the conv is resolved or the agent authored a
    message after `since` (the trigmemberng message id)."""
    conv = await _fetch_conv_verify(tenant, agent_id, conv_id)
    if conv.get("status") == "resolved":
        return True
    for m in conv.get("messages", []):
        # Our own automatic notices (the timeout notice of #70 above all) are
        # posted AS the agent but are not its answer.
        if (
            int(m.get("id", 0)) > since
            and m.get("from_agent") == agent_id
            and not is_auto_notice(m.get("body"))
        ):
            return True
    return False


async def _maybe_surface_escalation(tenant: str, agent_id: str, conv_id: int, since: int) -> None:
    """If the woken agent's reply carries a TYPED escalation but no needs_human was
    raised, surface it to the human once (idempotent). Decouples surfacing from the
    agent remembering the needs_human boolean. Best-effort: any fetch/post error is
    swallowed — it must never break an otherwise-successful wake."""
    try:
        conv = await _client(tenant, agent_id).get_conversation(conv_id)
    except Exception as exc:
        logger.debug("surface escalation: fetch failed for #%s: %s", conv_id, exc)
        return
    if conv.get("status") == "resolved":
        return
    msgs = conv.get("messages", []) or []
    # Idempotency: if any message after `since` already raised needs_human, stop
    # (covers our own repost on redelivery AND the agent setting the flag itself).
    if any(int(m.get("id", 0)) > since and m.get("needs_human") for m in msgs):
        return
    esc = None
    for m in msgs:
        if int(m.get("id", 0)) > since and m.get("from_agent") == agent_id:
            parsed = escalation.parse_escalation(m.get("context"))
            if parsed:
                esc = parsed  # take the latest such message
    if not esc:
        return
    try:
        await _client(tenant, agent_id).send_message(
            conv_id,
            _auto_notice(
                f"\U0001f513 L'agente {agent_id} è BLOCCATO e serve una tua decisione.\n"
                f"{esc['reason']}\n\U0001f449 {esc['decision_needed'] or 'Procedi?'}"
            ),
            needs_human=True,
        )
    except Exception as exc:
        logger.debug("surface escalation: post failed for #%s: %s", conv_id, exc)
        return


async def _wake_local_agent(tenant: str, agent_id: str, ev_type: str, data: dict) -> None:
    if ev_type not in ("new_message", "conversation_created"):
        return
    conv_id = int(data["conversation_id"])
    auto = is_auto_notice(data.get("body"))
    if not auto:
        # Real content (from anyone, ourselves included): the conversation may
        # carry one automatic notice again (#59).
        hub_store.clear_auto_notice(conv_id)
    # Fast-skip self-originated events: the agent's own escalation repost
    # (_maybe_surface_escalation posts needs_human AS this same agent) would
    # otherwise consume a turn_cap slot and re-run claude as a no-op before
    # idempotency stops the surfacing (#13). We never wake for our own message.
    if data.get("from_agent") == agent_id:
        return
    # Another agent's automatic notice is addressed to the human, not to us.
    # Dropped HERE, before the gag check: as a "newer message" it would lift our
    # gag and reset the turn counter, and our own notice would then do the same
    # to the other agent -- a ping-pong the turn cap never stops (#59).
    if auto:
        logger.info(
            "hub wake: #%s automatic notice from %s, not waking %s",
            conv_id,
            data.get("from_agent", "?"),
            agent_id,
        )
        return
    cfg = _S["config"]
    project_key = cfg.local_agents[agent_id]
    project = projects.resolve_project(project_key)
    if project is None:
        logger.warning("hub wake: unknown project %s for %s", project_key, agent_id)
        return

    # Single-flight: if a live WATCHER (hub_watch.py actually listening on the
    # bus) has claimed this agent, it handles the bus itself -- do NOT spawn a
    # headless twin, or two same-named agents talk over each other on the same
    # thread.
    if hub_store.is_watched(tenant, agent_id):
        logger.info("hub wake: %s claimed by a live watcher, skipping spawn", agent_id)
        return

    if await _no_reply_needed(tenant, agent_id, conv_id, data):
        logger.info(
            "hub wake: #%s message %s from %s declares no_reply_needed, not waking %s",
            conv_id,
            data.get("message_id"),
            data.get("from_agent", "?"),
            agent_id,
        )
        return

    inflight_key = (tenant, agent_id)
    msg_id = int(data.get("message_id") or 0)
    if inflight_key in _S["inflight_wakes"]:
        busy_conv = _S["inflight_wakes"][inflight_key]
        # Same event twice (SSE + A2A webhook, redelivery): the run in flight
        # is already handling it (#38).
        if busy_conv == conv_id and msg_id <= _S["inflight_msgs"].get(inflight_key, 0):
            logger.info(
                "hub wake: #%s message %s already being handled by %s, skipping twin",
                conv_id,
                msg_id,
                agent_id,
            )
            return
        # Anything else is real work: park it, never drop it (#67). The slot
        # owner starts the queue when it finishes, whatever the outcome.
        hub_store.enqueue_wake(tenant, agent_id, ev_type, data)
        queue = hub_store.queued_wakes(tenant, agent_id)
        logger.info(
            "hub wake: queued #%s behind #%s for %s (queue: %s)",
            conv_id,
            busy_conv,
            agent_id,
            queue,
        )
        await _publish_queue(tenant, agent_id, busy_conv, queue)
        return
    _S["inflight_wakes"][inflight_key] = conv_id
    _S["inflight_msgs"][inflight_key] = msg_id
    # Persisted, unlike inflight_wakes: if the process dies mid-run (container
    # recreate), boot puts this wake back at the head of the queue (#73).
    _note_wake_running(hub_store.mark_wake_running, tenant, agent_id, ev_type, data)
    cancelled = False
    try:
        await _run_local_wake(tenant, agent_id, ev_type, data, conv_id, project)
    except asyncio.CancelledError:
        # Shutdown (SIGTERM on recreate) cancels the run: the mark stays, so
        # the next boot requeues the wake instead of forgetting it.
        cancelled = True
        raise
    finally:
        if not cancelled:
            _note_wake_running(hub_store.clear_wake_running, tenant, agent_id)
        _S["inflight_wakes"].pop(inflight_key, None)
        _S["inflight_msgs"].pop(inflight_key, None)
        schedule_wake_drain(tenant, agent_id)


def _note_wake_running(fn, *args) -> None:
    """Best effort: an unwritable state file costs the restart requeue, never the wake."""
    try:
        fn(*args)
    except OSError as exc:
        logger.warning("hub wake: running mark not persisted: %s", exc)


def schedule_wake_drain(tenant: str, agent_id: str) -> None:
    """Start draining ``agent_id``'s parked wakes, unless a drainer is already
    running for it (#67). Safe to call from anywhere: no-op on an empty queue."""
    key = (tenant, agent_id)
    current = _S["wake_drainers"].get(key)
    if current is not None and not current.done():
        return
    if not hub_store.queued_wakes(tenant, agent_id):
        return
    _S["wake_drainers"][key] = asyncio.create_task(_drain_wake_queue(tenant, agent_id))


async def _drain_wake_queue(tenant: str, agent_id: str) -> None:
    """Run parked wakes one at a time while the agent's slot is free.

    Stops as soon as someone else holds the slot: that wake's own ``finally``
    schedules the next drain. Each parked wake goes back through
    ``_wake_local_agent``, so watcher claims, no_reply_needed, gags and the turn
    cap are re-checked against the state at the time it actually runs."""
    key = (tenant, agent_id)
    while key not in _S["inflight_wakes"]:
        item = hub_store.pop_wake(tenant, agent_id)
        if item is None:
            return
        conv_id = item["conv_id"]
        if not await _still_awaiting(tenant, agent_id, conv_id):
            logger.info("hub wake: queued #%s for %s already answered, dropped", conv_id, agent_id)
            await _set_delivery(tenant, agent_id, conv_id, "clear")
            continue
        logger.info("hub wake: starting queued #%s for %s", conv_id, agent_id)
        await _set_delivery(tenant, agent_id, conv_id, "running")
        # The ones still waiting moved up one place.
        await _publish_queue(tenant, agent_id, conv_id, hub_store.queued_wakes(tenant, agent_id))
        try:
            await _wake_local_agent(tenant, agent_id, item["ev_type"], item["data"])
        except OutcomeUnknown:
            logger.warning("hub wake: outcome unknown for queued #%s (%s)", conv_id, agent_id)
        except Exception:
            # One broken wake must not strand the rest of the queue.
            logger.exception("hub wake: queued #%s for %s failed", conv_id, agent_id)


async def _set_delivery(tenant: str, agent_id: str, conv_id: int, state: str, **kw) -> None:
    """Best effort (Network#35): the queue works the same if the bus does not
    hear about it -- the sender just loses the "in coda" badge."""
    try:
        await _client(tenant, agent_id).set_delivery(conv_id, state, **kw)
    except Exception as exc:
        logger.debug("hub wake: delivery %s for #%s not published: %s", state, conv_id, exc)


async def _publish_queue(tenant: str, agent_id: str, ahead: int, queue: list[int]) -> None:
    """Mark every parked conversation ``queued``, each behind the one before it."""
    for position, conv_id in enumerate(queue, start=1):
        await _set_delivery(
            tenant, agent_id, conv_id, "queued", behind=ahead, position=position
        )
        ahead = conv_id


async def _still_awaiting(tenant: str, agent_id: str, conv_id: int) -> bool:
    """A parked conversation still wants this agent: open, last message not its own.

    A bus read failure answers True -- a redundant wake is bounded by the turn
    cap, a dropped one is silent (#67)."""
    try:
        conv = await _client(tenant, agent_id).get_conversation(conv_id)
    except Exception as exc:
        logger.debug("hub wake: #%s state unreadable (%s), waking anyway", conv_id, exc)
        return True
    if conv.get("status", "open") != "open":
        return False
    msgs = conv.get("messages") or []
    return not (msgs and msgs[-1].get("from_agent") == agent_id)


async def _no_reply_needed(tenant: str, agent_id: str, conv_id: int, data: dict) -> bool:
    """True iff the trigmemberng message carries ``context.no_reply_needed`` (#61).

    The sender's own declaration that the exchange is over, so the recipient is
    not even woken. The SSE event does not carry ``context``: when the event
    lacks it, the message is read from the bus. Fails open -- any doubt (no
    message id, bus unreachable, message not found) wakes as usual, because a
    skipped wake that was needed is a silent loss, an extra one only costs a run.
    """
    ctx = await _message_context(tenant, agent_id, conv_id, data)
    return isinstance(ctx, dict) and ctx.get("no_reply_needed") is True


async def _message_context(tenant: str, agent_id: str, conv_id: int, data: dict) -> dict | None:
    """The waking message's ``context``: from the event, else read from the bus.

    The SSE event does not carry it. ``None`` on any doubt (no message id, bus
    unreachable, message not found, no context)."""
    ctx = data.get("context")
    if isinstance(ctx, dict):
        return ctx
    msg_id = int(data.get("message_id") or 0)
    if not msg_id:
        return None
    try:
        conv = await _client(tenant, agent_id).get_conversation(conv_id)
    except Exception as exc:
        logger.debug("message context: fetch failed for #%s: %s", conv_id, exc)
        return None
    msg = next(
        (m for m in conv.get("messages", []) or [] if int(m.get("id", 0)) == msg_id),
        None,
    )
    ctx = (msg or {}).get("context")
    return ctx if isinstance(ctx, dict) else None


def wake_max_seconds(ctx: dict | None) -> int | None:
    """Per-wake wall-clock budget from ``context.max_minutes`` (#70).

    Clamped to ``WAKE_MAX_MINUTES_CAP``; anything missing or not a positive
    number means "the default" (``None``), never an error: a sender typo must
    not stop the wake."""
    raw = (ctx or {}).get("max_minutes")
    if isinstance(raw, bool):
        return None
    try:
        minutes = float(raw)
    except (TypeError, ValueError):
        return None
    if minutes <= 0:
        return None
    return int(min(minutes, WAKE_MAX_MINUTES_CAP) * 60)


def _timed_out(run) -> bool:
    """The run was killed by the idle or the wall-clock watchdog."""
    usage = getattr(run, "usage", None)
    if usage is not None and getattr(usage, "outcome", None) == claude_runner.OUTCOME_TIMEOUT:
        return True
    return str(run[0] or "").lstrip().startswith("\u23f1")


async def _keep_running(tenant: str, agent_id: str, conv_id: int) -> None:
    """Keep telling the bus this wake is running until cancelled (#70); the
    caller says it the first time, before claude starts.

    Without it a direct wake was invisible: ``waiting_on_running`` stayed null
    for the whole run and the opener concluded nobody had taken it."""
    while True:
        await asyncio.sleep(WAKE_RUNNING_REFRESH_S)
        await _set_delivery(tenant, agent_id, conv_id, "running")


async def _post_wake_notice(tenant: str, agent_id: str, conv_id: int, text: str) -> None:
    """Informational notice on the thread (#70): auto-tagged, so it wakes nobody
    and is not counted as the agent's answer; no needs_human, nothing to decide;
    not capped, so it never blocks a later escalation notice. Best effort."""
    try:
        await _client(tenant, agent_id).send_message(conv_id, _auto_notice(text))
    except Exception as exc:
        logger.debug("hub wake: notice on #%s not posted: %s", conv_id, exc)


async def _origin_turn(tenant: str, agent_id: str, conv_id: int) -> hub_origin.Turn | None:
    """Decide whether this wake should resume the conversation's origin session.

    Returns ``None`` when the origin-session logic does not apply — host
    execution is off (a resume must run on the host, in the origin's real
    cwd), the conversation has neither an ``origin_session`` nor an interactive
    message from this agent (#54), or the bus can't be read —
    and the caller falls back to the derived session of today. Otherwise a
    :class:`hub_origin.Turn` to ``skip`` (another host's, or its /loop's, job)
    or ``resume_origin``.
    """
    if not claude_runner.host_exec_enabled():
        return None
    try:
        conv = await _client(tenant, agent_id).get_conversation(conv_id)
    except Exception as exc:
        logger.debug("origin: fetch failed for #%s: %s", conv_id, exc)
        return None
    origin = conv.get("origin_session")
    # The terminal session that last spoke as this agent, even on a
    # conversation it did not open: it must not get a headless twin (#54).
    voice = hub_origin.last_interactive_voice(conv.get("messages"), agent_id)
    if not origin and not voice:
        return None
    facts = dict(
        sessions_dir=_HUB_SESSIONS_DIR,
        projects_dir=_HUB_PROJECTS_DIR,
        now=time.time(),
    )
    host = hub_client.nome_host()
    turn = hub_origin.who_takes_turn(origin, agent_id, host, **facts)
    if voice and (not origin or voice.get("session_id") != origin.get("session_id")):
        turn = hub_origin.pick_turn(
            turn, hub_origin.who_takes_turn(voice, agent_id, host, **facts)
        )
    logger.info("hub wake: #%s origin turn: %s (%s)", conv_id, turn.action, turn.reason)
    return turn


def _schedule_idle_voice_recheck(
    tenant: str, agent_id: str, ev_type: str, data: dict, conv_id: int, session_id: str
) -> None:
    """Check back on a skip for a live interactive session (#74). One pending
    recheck per (agent, conversation): a newer event replaces the older one."""
    if IDLE_VOICE_RECHECK_S <= 0:
        return
    key = (tenant, agent_id, conv_id)
    old = _S["idle_voice_rechecks"].get(key)
    if old is not None and not old.done():
        old.cancel()
    _S["idle_voice_rechecks"][key] = asyncio.create_task(
        _idle_voice_recheck(tenant, agent_id, ev_type, data, conv_id, session_id)
    )


async def _idle_voice_recheck(
    tenant: str, agent_id: str, ev_type: str, data: dict, conv_id: int, session_id: str
) -> None:
    """Wake the worker if the interactive session that made us skip stays idle.

    Idle = its transcript not written since the message reached us: a session
    that is only open, waiting for its user to type, will not see the bus until
    then. Answered meanwhile (by anyone speaking as this agent) -> nothing to do.
    """
    key = (tenant, agent_id, conv_id)
    me = asyncio.current_task()
    seen_at = time.time()
    try:
        for _ in range(max(1, IDLE_VOICE_MAX_RECHECKS)):
            await asyncio.sleep(IDLE_VOICE_RECHECK_S)
            if not await _still_awaiting(tenant, agent_id, conv_id):
                logger.info("hub wake: #%s answered meanwhile, idle recheck done", conv_id)
                return
            last = hub_origin.last_write(_HUB_PROJECTS_DIR, session_id)
            if last is not None and last > seen_at:
                # It is working: give it another window.
                seen_at = time.time()
                continue
            await _wake_local_agent(tenant, agent_id, ev_type, {**data, IDLE_VOICE_OVERRIDE: True})
            return
        logger.warning(
            "hub wake: #%s still unanswered, session %s busy for %d rechecks: leaving it",
            conv_id,
            session_id,
            IDLE_VOICE_MAX_RECHECKS,
        )
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("hub wake: idle recheck on #%s for %s failed", conv_id, agent_id)
    finally:
        if _S["idle_voice_rechecks"].get(key) is me:
            _S["idle_voice_rechecks"].pop(key, None)


async def _run_local_wake(
    tenant: str, agent_id: str, ev_type: str, data: dict, conv_id: int, project
) -> None:
    """Body of the wake, run only while `_wake_local_agent` holds the
    per-agent in-flight slot (#38): a live claude session for `agent_id`
    is never spawned twice concurrently, whatever the source of the
    duplicate event (SSE redelivery, the A2A push webhook, ...).
    """
    cfg = _S["config"]

    # Origin-session resume (#51): if this conversation was opened by THIS agent
    # from an interactive session on THIS host, and that session has since
    # closed, resume it (in its own cwd) instead of a derived headless session.
    # Decided up front so a `skip` returns without consuming a turn or spawning.
    origin_turn = await _origin_turn(tenant, agent_id, conv_id)
    if origin_turn is not None and origin_turn.action == "skip":
        if data.get(IDLE_VOICE_OVERRIDE) and origin_turn.session_id:
            # The recheck found the session idle since the message: wake the
            # worker on its OWN derived session -- never the interactive
            # session's UUID, which is still open (#74).
            logger.warning(
                "hub wake: #%s interactive session %s idle since the message, "
                "waking %s headless",
                conv_id,
                origin_turn.session_id,
                agent_id,
            )
            origin_turn = None
        else:
            logger.info("hub wake: #%s not spawning — %s", conv_id, origin_turn.reason)
            if origin_turn.session_id:
                # Alive on THIS host: its own turn, if it takes it. Not final.
                _schedule_idle_voice_recheck(
                    tenant, agent_id, ev_type, data, conv_id, origin_turn.session_id
                )
            return

    # "Ghost" claim: an interactive session looks present (hub-auto-claim.sh
    # hook keeps the claim fresh across SessionStart/Stop) but nothing is
    # really listening on the bus for this agent -- the scenario from issue
    # #26. Don't skip the spawn here: that silently drops the event until the
    # ghost claim expires, and the eventual headless wake then runs with no
    # context on why the conversation was opened. Spawn as usual, but flag the
    # prompt so the woken worker is cautious instead of confidently closing a
    # conversation it may not understand.
    ghost = hub_store.is_claimed(tenant, agent_id)

    # Already escalated to a human after a failed wake -> the conversation is in
    # the human's court; re-wakes from its own escalation event are no-ops
    # (same self-terminating shape as the turn-cap guard; see incident #32).
    #
    # The gag is SCOPED to the message we gave up on: a newer message is fresh
    # content — someone is putting the ball back in play — and lifts it. An
    # unscoped flag never switched off (only a bus-DB epoch change cleared it),
    # so a 12-second outage that burned both attempts silenced conversation 33
    # for four hours with no log line anywhere (#29). Dropping an event is now
    # logged at WARNING: a silencer that doesn't announce itself is
    # indistinguishable from "all good".
    esc_at = hub_store.wake_escalated_at(conv_id)
    if esc_at is not None:
        msg_id = int(data.get("message_id") or 0)
        if msg_id <= esc_at:
            logger.warning(
                "hub wake: DROPPED %s on #%s for %s — conversation escalated at "
                "message %s, this event (%s) is not newer",
                ev_type,
                conv_id,
                agent_id,
                esc_at,
                msg_id,
            )
            return
        logger.info(
            "hub wake: #%s un-gagged by message %s from %s (was escalated at %s)",
            conv_id,
            msg_id,
            data.get("from_agent", "?"),
            esc_at,
        )
        # Clears the turn counter too (see clear_wake_escalated): a conversation
        # that came back from a human with new content starts over. Runs BEFORE
        # incr_turn below, so this event is turn 1 — otherwise a conversation
        # that had also hit the turn cap would swap one permanent silencer for
        # the other.
        hub_store.clear_wake_escalated(conv_id)

    # Loop guard: too many turns -> stop auto-running, escalate to the human.
    # Posted ONCE (turns == cap + 1): re-posting on every later event would itself
    # create a new_message -> a new event -> another escalation (incident #32).
    turns = hub_store.incr_turn(conv_id)
    if turns > cfg.turn_cap:
        if turns == cfg.turn_cap + 1:
            await _post_auto_notice(
                tenant,
                agent_id,
                conv_id,
                f"Raggiunto il tetto di {cfg.turn_cap} turni automatici: serve una decisione umana.",
            )
        return

    # An agent node (Life#157 §3.2): Life's common runtime answers, the reply goes
    # on the bus as this agent. A failed run is said on the thread, then the wake
    # goes on the usual way -- in the container, closed to the manifest's tools.
    agent_node = agent_nodes.node_of(project.workspace)
    if agent_node is not None and await _agent_node_reply(
        tenant, agent_id, conv_id, agent_node, data
    ):
        return

    # Issue #46: with host execution configured, the session runs on the host in
    # the project's REAL path — same user, credentials and project memory as a
    # terminal. Without a host path (project not on this device) it stays in the
    # container, and the prompt says which of the two it got.
    host_cwd = None if agent_node is not None else wake_host_cwd(project)
    run_cwd = host_cwd or project.workspace
    # Origin resume (#51): run in the origin session's own cwd on the host (host
    # execution is guaranteed on by _origin_turn). The derived session, keyed by
    # (conv, agent, cwd), is not touched — this is the opener's own session, not
    # the worker's.
    if origin_turn is not None and origin_turn.action == "resume_origin" and agent_node is None:
        host_cwd = origin_turn.cwd
        run_cwd = origin_turn.cwd
    prompt = _WAKE_PROMPT.format(
        conv_id=conv_id,
        subject=data.get("subject", ""),
        frm=data.get("from_agent", "agente"),
        body=data.get("body", ""),
        environment=(
            _WAKE_ENV_HOST.format(cwd=host_cwd) if host_cwd else _WAKE_ENV_CONTAINER
        ),
        caution=_WAKE_CAUTION if ghost else "",
        no_reply=NO_REPLY_MARKER,
    )
    # Closed loop: run, then confirm on the bus the agent actually posted/resolved.
    # One retry with a FRESH session (no resume) before escalating. Verify+retry
    # run inline within this one event handling, so a failed wake costs 1 turn.
    #
    # If the bus is unreachable during a verify, _verify_outcome raises
    # OutcomeUnknown, which propagates past this loop to _listen: the cursor is
    # held and the whole event is redelivered later. That deliberately skips the
    # second attempt — when we cannot even read the bus, re-running claude is
    # pointless; holding for full redelivery is the correct fail-safe.
    # `or 0` (not a get-default): the A2A push path can carry message_id=None for
    # the FIRST event of a conversation (the bus omits it; Network forwards the
    # null) -> int(None) would crash. since=0 is correct for a first wake (no
    # prior agent message to confuse _verify_outcome). The SSE path always sets it.
    since = int(data.get("message_id") or 0)
    # Sessions are keyed by the cwd they ran in: a container transcript is not
    # resumable on the host (different project dir) and vice versa.
    resume = hub_store.get_session(conv_id, agent_id, run_cwd)
    if origin_turn is not None and origin_turn.action == "resume_origin" and agent_node is None:
        resume = origin_turn.session_id
    elif resume and claude_runner.host_exec_enabled():
        # The Launcher can open this very session (`claude --resume`) from the
        # card link: while it is live, a headless resume would be a second
        # process on the same session UUID (TicketManager#10). It never speaks
        # on the bus, so the #54 voice rule cannot see it.
        held = hub_origin.interactive_holder(
            resume,
            agent_id,
            hub_client.nome_host(),
            sessions_dir=_HUB_SESSIONS_DIR,
            projects_dir=_HUB_PROJECTS_DIR,
            now=time.time(),
        )
        if held is not None:
            logger.warning("hub wake: #%s not spawning — %s", conv_id, held.reason)
            return
    # Inject the LIVE bus coordinates so /hub-send replies to THIS bus as THIS
    # agent, regardless of what the project's .env says (it can be stale: a
    # drifted HUB_URL/key silently posts to a dead/other bus and the reply never
    # lands -> spurious "agent did not answer" escalation).
    # HUB_API_KEY carries the agent's OWN key when we have one: the bus also
    # accepts an agent key in the X-Api-Key slot (dependencies.py path 3), so
    # every existing /hub-send-style caller inside the woken session keeps
    # authenticating unchanged once the shared tenant key is revoked.
    woken_agent_key = cfg.agent_key(agent_id, tenant)
    bus_env = {
        "HUB_URL": cfg.hub_url,
        "HUB_API_KEY": woken_agent_key or cfg.api_keys.get(tenant, ""),
        "HUB_TENANT_API_KEY": cfg.api_keys.get(tenant, ""),
        "HUB_AGENT_KEY": woken_agent_key,
        "HUB_AGENT_ID": agent_id,
    }
    # Per-wake budget (#70): ``context.max_minutes`` on the waking message.
    msg_ctx = await _message_context(tenant, agent_id, conv_id, data)
    max_s = wake_max_seconds(msg_ctx)
    # The instance's LLM queue (#91); None keeps the historic claude-only path.
    plan = _wake_plan(tenant, project, msg_ctx, host_cwd) if agent_node is None else None
    # From here on a claude run is coming: the bus hears it is running, and
    # keeps hearing it until we are done, whatever the outcome (#70).
    await _set_delivery(tenant, agent_id, conv_id, "running")
    heartbeat = asyncio.create_task(_keep_running(tenant, agent_id, conv_id))
    tools_token = (
        claude_runner.SESSION_TOOLS.set(",".join(agent_node.tools))
        if agent_node is not None
        else None
    )
    try:
        await _wake_attempts(
            tenant, agent_id, conv_id, project, prompt, resume, since,
            bus_env=bus_env, host_cwd=host_cwd, run_cwd=run_cwd, max_s=max_s,
            plan=plan,
        )
    finally:
        if tools_token is not None:
            claude_runner.SESSION_TOOLS.reset(tools_token)
        heartbeat.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await heartbeat
        await _set_delivery(tenant, agent_id, conv_id, "clear")
        hub_store.mark_session_stopped(conv_id, agent_id)


async def _agent_node_reply(
    tenant: str, agent_id: str, conv_id: int, node: "agent_nodes.AgentNode", data: dict
) -> bool:
    """Dry answer of an agent node on the bus; True when it was posted.

    False means the caller falls back to the usual wake: the failure has already
    been said on the thread (never silence, same rule as node_text)."""
    ans = await agent_nodes.answer(node, str(data.get("body") or ""))
    if ans.ok:
        try:
            await _client(tenant, agent_id).send_message(
                conv_id, ans.risposta, context={"agent_node": node.slug}
            )
            return True
        except Exception as exc:
            logger.warning("hub wake: #%s agent node %s reply not posted: %s",
                           conv_id, node.slug, exc)
            return False
    await _post_wake_notice(tenant, agent_id, conv_id, agent_nodes.failure_text(node, ans))
    return False


async def _wake_attempts(
    tenant: str,
    agent_id: str,
    conv_id: int,
    project,
    prompt: str,
    resume: str | None,
    since: int,
    *,
    bus_env: dict,
    host_cwd: str | None,
    run_cwd: str,
    max_s: int | None,
    plan: "wake_routing.Plan | None" = None,
) -> None:
    """Run claude up to two answer attempts, then escalate (closed loop).

    With a ``plan`` (#91) the claude run is the queue's first entry, on its
    profile and model; if it fails for any reason the rest of the queue runs on
    the host through Life's engine, and a human hears of it only if the whole
    queue failed."""
    attempt = 0
    transport_failures = 0
    timeouts = 0
    backoff = WAKE_TRANSPORT_BACKOFF_S
    budget_min = round((max_s or claude_runner.CLAUDE_MAX_TIMEOUT) / 60)
    first_prompt = prompt
    auth_text: str | None = None
    quota_text: str | None = None
    run_primary = plan is None or plan.primary is not None
    can_fall_back = plan is not None and bool(plan.fallback)
    primary = plan.primary if plan is not None else None
    profile_token = (
        claude_runner.WAKE_PROFILE.set({"instance": plan.instance, "name": primary.profilo})
        if primary is not None and primary.profilo
        else None
    )
    run_kw = {"model": primary.modello} if primary is not None and primary.modello else {}
    try:
        while run_primary and attempt < 2:
            t_run = time.monotonic()

            def _on_session(sid: str) -> None:
                # Registered at the run's first event, not at its end: the Launcher
                # opens this same session while it is still working (TicketManager#10).
                hub_store.set_session(
                    conv_id, sid, agent_id=agent_id, workspace=run_cwd, running=True
                )

            def _on_wait() -> None:
                logger.info(
                    "hub wake: #%s %s waits for a free claude slot (all %s busy)",
                    conv_id,
                    agent_id,
                    _S["config"].claude_max_concurrency if _S["config"] else "?",
                )

            # The process-wide bound sits HERE, around the run only (#74).
            async with wake_pool.slot(_on_wait):
                run = await _S["run_claude"](
                    prompt,
                    workspace=project.workspace,
                    persona=None,
                    resume_id=resume,
                    extra_env=bus_env,
                    on_session=_on_session,
                    **({"host_cwd": host_cwd} if host_cwd else {}),
                    **({"max_timeout": max_s} if max_s else {}),
                    **run_kw,
                )
            text, session_id = run
            # Bus wakes are the one kind of spend nobody is watching in real time:
            # they fire from another agent's message, not from a person. Attributed
            # to the woken agent so a chatty conversation shows up as one caller.
            llm_usage.record(run, node=project.key, caller=agent_id)
            if session_id:
                hub_store.set_session(
                    conv_id, session_id, agent_id=agent_id, workspace=run_cwd, running=False
                )
            # An infrastructure fault (bus down, CLI exit, stream cut short) is NOT
            # "the agent didn't answer": the model was never reached. Retry it with
            # backoff instead of spending one of the two answer attempts on it —
            # otherwise a 12-second outage exhausts the budget and escalates (#29).
            if plan is not None and wake_routing.is_quota_error(text):
                # Credits/limit of this subscription: retrying it cannot help,
                # the next entry of the queue can (#91).
                quota_text = text
                logger.warning(
                    "hub wake: #%s %s %s out of credits/limit: %s",
                    conv_id, agent_id,
                    wake_routing.primary_label(primary) if primary else "claude",
                    text.replace("\n", " ")[:200],
                )
                break
            if claude_runner.is_auth_error(text):
                if can_fall_back:
                    auth_text = text
                    break
                # Non e' un guasto di questa conversazione: e' la CLI di QUESTO
                # processo che non e' autenticata, quindi riguarda ogni agente che
                # ci gira e nessun tentativo puo' cambiarlo. Ritentare sarebbe
                # inutile; scriverlo in ogni conversazione sarebbe peggio che
                # inutile, perche' la notizia e' una sola e finirebbe ripetuta
                # nell'unico posto che nessuno rilegge (issue #34).
                await _segnala_cli_non_autenticata(tenant, agent_id, conv_id, text)
                return
            if claude_runner.is_transport_error(text):
                transport_failures += 1
                # Counted against attempts, not retries: the first run plus
                # WAKE_TRANSPORT_RETRIES retries. "failure 4/3" read as a bug.
                logger.warning(
                    "hub wake: transport failure on attempt %d/%d on #%s (%s): %s",
                    transport_failures,
                    WAKE_TRANSPORT_RETRIES + 1,
                    conv_id,
                    agent_id,
                    text.replace("\n", " ")[:200],
                )
                if transport_failures <= WAKE_TRANSPORT_RETRIES:
                    await asyncio.sleep(backoff)
                    backoff *= 2
                    resume = None  # a dead session is a common cause; start clean
                    continue
                break  # transport is durably broken -> hand it to a human
            attempt += 1
            if await _verify_outcome(tenant, agent_id, conv_id, since):
                await _maybe_surface_escalation(tenant, agent_id, conv_id, since)
                return
            if chose_not_to_reply(text):
                # Deliberate silence is an outcome, not a failure (#61): the prompt
                # allows "non rispondere affatto", so counting it as "did not answer"
                # re-woke the agent and paged a human with nothing to decide.
                logger.info(
                    "hub wake: #%s %s chose not to reply (%s) -- handled, no escalation",
                    conv_id,
                    agent_id,
                    NO_REPLY_MARKER,
                )
                return
            if _timed_out(run) and session_id:
                # Cut off by the watchdog, not finished: the work in progress (dirty
                # files, its own reasoning) lives in THAT session. Resume it instead
                # of a fresh one that would not know what the first was doing (#70).
                timeouts += 1
                minutes = max(1, round((time.monotonic() - t_run) / 60))
                logger.warning(
                    "hub wake: #%s %s timed out after %s' (attempt %d) -- resuming %s",
                    conv_id, agent_id, minutes, attempt, session_id,
                )
                if attempt < 2:
                    await _post_wake_notice(
                        tenant, agent_id, conv_id,
                        f"{agent_id}: interrotto per tempo dopo {minutes}' "
                        f"(tetto {budget_min}'), riprendo la stessa sessione.",
                    )
                    # The notice is a message from this agent: on the bus it clears
                    # our "running". Say it again -- we are still on it.
                    await _set_delivery(tenant, agent_id, conv_id, "running")
                resume = session_id
                prompt = _WAKE_RESUME_PROMPT.format(
                    minutes=minutes, conv_id=conv_id, no_reply=NO_REPLY_MARKER
                )
                continue
            resume = None  # attempt 2: clean restart

    finally:
        if profile_token is not None:
            claude_runner.WAKE_PROFILE.reset(profile_token)

    # No bus outcome -> escalate once, then gag THIS message (a newer one lifts it).
    if quota_text:
        reason = "crediti/limite esauriti"
    elif auth_text:
        reason = "CLI non autenticata"
    elif transport_failures > WAKE_TRANSPORT_RETRIES:
        reason = f"il trasporto ha continuato a fallire ({transport_failures} tentativi)"
    elif timeouts:
        reason = f"dopo 2 tentativi automatici (interrotto per tempo, tetto {budget_min}')"
    else:
        reason = "dopo 2 tentativi automatici"
    fallback = None
    if can_fall_back:
        fallback = await _wake_fallback(
            tenant, agent_id, conv_id, plan, first_prompt, since,
            bus_env=bus_env, cwd=host_cwd or run_cwd,
        )
        if fallback.ok:
            return
    if auth_text and (fallback is None or not fallback.attempts):
        # Nothing else could even try: the #34 notice, once, out of the thread.
        await _segnala_cli_non_autenticata(tenant, agent_id, conv_id, auth_text)
        return
    detail = ""
    if plan is not None:
        tried = wake_routing.attempts_text(reason if run_primary else None, plan, fallback)
        if tried:
            detail = f"\nCoda LLM ({plan.source}), tutti i tentativi falliti:\n{tried}"
        reason = "dopo aver provato tutta la coda LLM" if plan.fallback else reason
    await _post_auto_notice(
        tenant,
        agent_id,
        conv_id,
        f"L'agente {agent_id} non ha risposto alla conversazione #{conv_id} "
        f"{reason}: serve un umano.{detail}",
    )
    hub_store.set_wake_escalated(conv_id, at_message_id=since)


def _wake_plan(tenant: str, project, msg_ctx: dict | None, host_cwd: str | None):
    """The wake's LLM queue (#91), or None for the historic claude-only path.

    The fallback runs on the host, so without host execution (or a host path
    for this project) there is nothing it could run in. A forced entry comes
    from the waking message (``context.forza``) or from /modello on the
    project's group, in the engine's syntax ``mezzo:profilo/modello``."""
    if not host_cwd or not claude_runner.host_exec_enabled():
        return None
    key = getattr(project, "key", "") or ""
    force = (msg_ctx or {}).get("forza") if isinstance(msg_ctx, dict) else None
    force = force if isinstance(force, str) and force.strip() else wake_routing.get_forza(key)
    try:
        plan = wake_routing.load_plan(tenant, key, force=force)
    except Exception:
        logger.exception("hub wake: LLM queue of %s/%s unreadable: claude-only path", tenant, key)
        return None
    if plan is not None:
        # The first entry runs on run_claude, whose backend (AI_BACKEND) may not
        # be the one the queue entry names: log the executor that will really run
        # it, not the config line (#95).
        labels = [
            wake_routing.primary_label(e) if e is plan.primary else e.label()
            for e in plan.coda
        ]
        logger.info(
            "hub wake: %s/%s follows %s: %s", tenant, key, plan.source,
            " → ".join(labels),
        )
    return plan


async def _wake_fallback(
    tenant: str,
    agent_id: str,
    conv_id: int,
    plan: "wake_routing.Plan",
    prompt: str,
    since: int,
    *,
    bus_env: dict,
    cwd: str,
) -> "wake_routing.FallbackResult":
    """Run the rest of the queue on the host (#91); True-ish ``ok`` = handled.

    The engine's runs are fresh (no resume) and may not be able to post on the
    bus themselves (a ``claude -p`` without bypass, a dry API answer): when the
    agent did not post, its answer is posted for it, so a working fallback is
    never reported as "did not answer"."""
    logger.warning(
        "hub wake: #%s %s primary path failed — falling back to %s",
        conv_id, agent_id, " → ".join(e.label() for e in plan.fallback),
    )
    await _set_delivery(tenant, agent_id, conv_id, "running")
    async with wake_pool.slot(lambda: None):
        res = await wake_routing.run_fallback(plan, cwd=cwd, prompt=prompt, extra_env=bus_env)
    logger.info(
        "hub wake: #%s %s fallback %s (%s)", conv_id, agent_id,
        f"answered by {res.winner}" if res.ok else "failed",
        "; ".join(f"{a.get('voce')}={a.get('esito')}" for a in res.attempts) or res.error,
    )
    if not res.ok:
        return res
    if await _verify_outcome(tenant, agent_id, conv_id, since):
        await _maybe_surface_escalation(tenant, agent_id, conv_id, since)
        return res
    if chose_not_to_reply(res.text):
        return res
    try:
        await _client(tenant, agent_id).send_message(
            conv_id, res.text.strip(),
            context={"llm_routing": {"voce": res.winner, "ripiego": True}},
        )
    except Exception as exc:
        logger.warning("hub wake: #%s fallback answer not posted: %s", conv_id, exc)
        res.ok = False
        res.error = f"risposta del ripiego non pubblicata: {exc}"
    return res


def _parse_iso(value: str | None) -> "_dt.datetime | None":
    """Parse an ISO-8601 timestamp (accepts a trailing 'Z'); None on failure."""
    if not value:
        return None
    try:
        return _dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


async def _recover_backlog(tenant: str, agent_id: str) -> None:
    """On (re)start, re-wake open conversations left unanswered while down.

    Filters (all must hold): inbox conv updated within recovery_window_h, status
    'open', last message NOT authored by this agent, not already wake_escalated.
    Capped at recovery_max. A double-wake vs the resuming SSE is wasteful but
    harmless (turn_cap bounds it); no extra dedup state is kept (see spec)."""
    cfg = _S["config"]
    try:
        inbox = await _client(tenant, agent_id).get_inbox()
    except Exception:
        logger.exception("recovery: inbox fetch failed (%s/%s)", tenant, agent_id)
        return
    cutoff = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(hours=cfg.recovery_window_h)
    woke = 0
    for item in inbox:
        if woke >= cfg.recovery_max:
            break
        conv_id = int(item["id"])
        lma = _parse_iso(item.get("last_message_at"))
        if lma is not None and lma < cutoff:
            continue
        try:
            conv = await _client(tenant, agent_id).get_conversation(conv_id)
        except Exception:
            logger.warning("recovery: detail fetch failed for #%s", conv_id)
            continue
        if conv.get("status") != "open":
            continue
        msgs = conv.get("messages") or []
        if not msgs or msgs[-1].get("from_agent") == agent_id:
            continue  # empty or already answered by us
        last = msgs[-1]
        # Escalated conversations stay in the human's court only while nothing
        # new has been said: a message NEWER than the one we gave up on is fresh
        # content and deserves a re-wake (#29). Checked here, not before the
        # detail fetch, because only the detail carries the last message id.
        esc_at = hub_store.wake_escalated_at(conv_id)
        if esc_at is not None and int(last.get("id", 0)) <= esc_at:
            continue
        data = {
            "conversation_id": conv_id,
            "message_id": int(last.get("id", 0)),
            "from_agent": last.get("from_agent", "agente"),
            "subject": conv.get("subject", ""),
            "body": last.get("body", ""),
            "priority": conv.get("priority", "normal"),
        }
        woke += 1
        try:
            await _wake_local_agent(tenant, agent_id, "new_message", data)
        except OutcomeUnknown:
            logger.warning("recovery: outcome unknown for #%s", conv_id)
    if woke:
        logger.info("recovery: re-woke %d backlog conv(s) for %s/%s", woke, tenant, agent_id)


def _inbox_text(by_tenant: dict[str, list[dict]]) -> str:
    lines: list[str] = []
    for tenant, items in by_tenant.items():
        for it in items:
            subj = html.escape(it.get("subject", ""))
            preview = html.escape((it.get("last_message_preview") or "")[:120])
            # Surface the local suppression state: an escalated conversation is
            # muted until someone writes something newer, and /hub is the one
            # place a human looks at conversation state (#29).
            esc_at = hub_store.wake_escalated_at(int(it["id"]))
            muted = (
                f"\n   \U0001f507 <i>muta: escalata al messaggio {esc_at}, "
                f"si riapre al primo messaggio successivo</i>"
                if esc_at is not None
                else ""
            )
            lines.append(
                f"\U0001f6f0 <b>#{it['id']}</b> · {subj} "
                f"({tenant}, {it.get('priority','normal')}, {it.get('unread_count',0)} non letti)\n"
                f"   <i>{preview}</i>{muted}"
            )
    if not lines:
        return "\U0001f4ed Nessuna conversazione aperta nel bus."
    return "\U0001f6f0 <b>Conversazioni Network</b>\n\n" + "\n".join(lines)


# --- Remote turns (#83, F3): the poller hands a chat to the node's own device ----


def local_suffix() -> str:
    """This device's suffix on the bus (the one :func:`remote_executor` never picks)."""
    cfg = _S["config"]
    return (cfg.dev_device if cfg else "") or "dev-linux"


async def remote_executor(tenant: str, graph_id: str, ladder=()) -> "remote_turn.Executor | None":
    """The bus identity that runs ``graph_id`` on another device, or None."""
    cfg = _S["config"]
    if not cfg or not cfg.enabled or tenant not in cfg.api_keys:
        return None
    agents = await _client(tenant, cfg.telegram_agent).list_agents(tenant)
    return remote_turn.pick(graph_id, agents, local_suffix=local_suffix(),
                            ladder=ladder, tenant=cfg.canonical_tenant(tenant))


def _remote_conv_for(chat_id: int, topic_id: int, agent: str) -> int | None:
    convs = hub_store._load().get("conversations", {})
    for cid, rec in convs.items():
        rt = rec.get("remote_turn") or {}
        if (rt.get("chat_id"), rt.get("topic_id"), rt.get("agent")) == (chat_id, topic_id, agent):
            return int(cid)
    return None


async def send_remote_turn(tenant: str, agent: str, text: str, *, chat_id: int,
                           topic_id: int, label: str) -> int:
    """Post a chat message to the executor: one conversation per (chat, topic, agent).

    Reusing the conversation keeps the executor's session (sessions belong to the
    conversation and the agent), so a follow-up is a follow-up, not a new stranger.
    """
    cfg = _S["config"]
    client = _client(tenant, cfg.telegram_agent)
    conv_id = _remote_conv_for(chat_id, topic_id, agent)
    if conv_id is not None:
        try:
            await client.send_message(conv_id, text, needs_human=False)
            return conv_id
        except Exception:
            logger.warning("remote turn: conv #%s not writable, opening a new one", conv_id)
    conv = await client.create_conversation(
        to=[agent], subject=f"Telegram \u2192 {label}"[:120], body=text, priority="normal",
        context={"telegram_turn": {"chat_id": chat_id, "topic_id": topic_id}},
    )
    conv_id = int(conv["id"])
    hub_store.set_conv_fields(conv_id, tenant=tenant, counterpart=agent,
                              remote_turn={"chat_id": chat_id, "topic_id": topic_id,
                                           "agent": agent})
    return conv_id


async def _deliver_remote_turn_reply(conv_id: int, data: dict) -> bool:
    """An executor answered a remote turn: the answer goes back into the chat."""
    rec = hub_store.get_conv(conv_id) or {}
    rt = rec.get("remote_turn")
    if not rt:
        return False
    cfg = _S["config"]
    frm = data.get("from_agent") or ""
    body = (data.get("body") or "").strip()
    if not frm or frm == cfg.telegram_agent or not body or is_auto_notice(body):
        return True  # ours, or nothing to show: still a remote turn, handled
    try:
        await _S["bot"].send_message(
            chat_id=rt["chat_id"], message_thread_id=rt.get("topic_id") or None,
            text=remote_turn.reply_text(frm, body))
    except Exception:
        logger.exception("remote turn reply #%s not delivered to chat %s", conv_id, rt["chat_id"])
    return True


async def dispatch_to_agent(tenant: str, agent_id: str, task: str) -> dict:
    """Open a bus conversation from the human face to a (prod) agent. No needs_human:
    agent->agent wakes the remote worker, which executes and replies on the bus."""
    cfg = _S["config"]
    subject = f"Task da {cfg.telegram_agent}"
    if cfg.use_a2a:
        # A2A's natural agent->agent case: message/send with no taskId starts a
        # new task addressed to agent_id. Normalise the Task id (str) back to int
        # so callers that do conv.get("id") behave as with the REST conversation.
        task_obj = await _a2a_client(tenant, cfg.telegram_agent).send_message(
            agent_id, task, subject=subject[:120], priority="normal"
        )
        if task_obj.get("id") is not None:
            task_obj = {**task_obj, "id": int(task_obj["id"])}
        return task_obj
    return await _client(tenant, cfg.telegram_agent).create_conversation(
        to=[agent_id],
        subject=subject[:120],
        body=task,
        priority="normal",
    )


async def forward_dm_reply(agent_id: str, subject: str, body: str, context: dict) -> dict:
    """Open a bus conversation carrying a person's private reply to ``agent_id`` (#55).

    Raises when the bus is not configured or refuses (unknown agent, 4xx/5xx):
    the caller then falls back to telling an admin, so a reply is never lost
    silently the way it was before #55.
    """
    cfg = _S["config"]
    if not cfg or not cfg.enabled:
        raise RuntimeError("bus non configurato")
    tenant = _default_tenant()
    if not tenant:
        raise RuntimeError("nessun tenant sul bus")
    return await _client(tenant, cfg.telegram_agent).create_conversation(
        to=[agent_id], subject=subject[:120], body=body,
        priority="high", context=context,
    )


def _default_tenant() -> str | None:
    cfg = _S["config"]
    return next(iter(cfg.api_keys), None) if cfg else None


def _project_names(project) -> tuple[str, tuple[str, ...]]:
    """(bus slug, every other name) of a project, for the HubConfig resolvers."""
    slug = getattr(project, "bus_slug", "") or project.key
    others = tuple(
        n for n in dict.fromkeys((project.key, *getattr(project, "aliases", ())))
        if n != slug
    )
    return slug, others


def dev_agent_of(project) -> str:
    """This device's bus identity for ``project`` (#79): never a key-derived alias."""
    cfg = _S["config"]
    slug, others = _project_names(project)
    return cfg.dev_agent(slug, others) if cfg else slug


def prod_agents_of(project) -> list[str]:
    """The prod bus identities of ``project`` (#79), slug-derived unless overridden."""
    cfg = _S["config"]
    slug, others = _project_names(project)
    return list(cfg.prod_agents(slug, others)) if cfg else [f"{slug}-prod"]


def bus_agent_for_node(node: str) -> str:
    """The bus identity that answers for ``node``, a project name or an agent.

    Notifications name their node by project key (``offerte``), and the bus
    never had an agent called like that: the suffix rule wants a device, and
    Network#42 wants the slug (``hubOfferte-dev-linux``). A name that resolves
    to no project is taken as an agent name already and passed through.
    """
    from projects import resolve_project

    project = resolve_project(node)
    return dev_agent_of(project) if project is not None else node


_PROD_PREFIX = "prod:"


def is_dispatch_prefix(text: str) -> bool:
    """Whether ``text`` asks for a prod dispatch (``prod:``): a ``publish`` action (#83)."""
    return text.strip().lower().startswith(_PROD_PREFIX)


async def maybe_dispatch_prefix(project, text: str) -> tuple[bool, dict | None]:
    """If text starts with 'prod:' and the project maps to a prod agent, dispatch
    the remainder to that agent over the bus. Returns (handled, conversation|None).

    Only an explicit HUB_PROD_DISPATCH entry opts a project in, looked up by any
    of its names: the table is keyed by the names of the day it was written
    (`segnalatori`), not by today's slug (#79)."""
    cfg = _S["config"]
    if not cfg or not text.strip().lower().startswith(_PROD_PREFIX):
        return False, None
    slug, others = (project, ()) if isinstance(project, str) else _project_names(project)
    explicit = cfg._override(cfg.prod_dispatch, (slug, *others))
    agent_id = explicit.split("|")[0].strip() if explicit else ""
    if not agent_id:
        return False, None
    task = text.strip()[len(_PROD_PREFIX) :].strip()
    if not task:
        return False, None
    tenant = _default_tenant()
    if not tenant:
        return False, None
    conv = await dispatch_to_agent(tenant, agent_id, task)
    return True, conv


async def cmd_dispatch(update, context) -> None:
    """/dispatch <agent> <task...> — send a task to a (prod) agent over the bus."""
    from telegram.constants import ParseMode

    cfg = _S["config"]
    if not cfg or not cfg.enabled:
        await update.effective_message.reply_text("Integrazione Network non configurata.")
        return
    args = context.args or []
    if len(args) < 2:
        await update.effective_message.reply_text(
            "Uso: /dispatch <agente> <task>\nEs: /dispatch offerte-prod allinea e deploya"
        )
        return
    agent_id, task = args[0], " ".join(args[1:])
    tenant = _default_tenant()
    if not tenant:
        await update.effective_message.reply_text("Bus non configurato.")
        return
    try:
        conv = await dispatch_to_agent(tenant, agent_id, task)
        await update.effective_message.reply_text(
            f"\U0001f680 Dispatch a <b>{html.escape(agent_id)}</b> (conv #{conv.get('id')}).",
            parse_mode=ParseMode.HTML,
        )
    except Exception:
        logger.exception("dispatch failed")
        await update.effective_message.reply_text("❌ Dispatch fallito (vedi log).")


async def cmd_hub(update, context) -> None:
    """/hub — show open conversations across tenants (human agent inbox)."""
    cfg = _S["config"]
    if not cfg or not cfg.enabled:
        await update.effective_message.reply_text("Integrazione Network non configurata.")
        return
    by_tenant: dict[str, list[dict]] = {}
    for tenant in cfg.api_keys:
        try:
            by_tenant[tenant] = await _client(tenant, cfg.telegram_agent).get_inbox()
        except Exception:
            logger.exception("hub inbox failed for %s", tenant)
            by_tenant[tenant] = []
    from telegram.constants import ParseMode

    await update.effective_message.reply_text(_inbox_text(by_tenant), parse_mode=ParseMode.HTML)
