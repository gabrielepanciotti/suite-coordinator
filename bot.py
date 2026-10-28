"""Coordinator: a single Telegram bot that is a *session dashboard* over N Claude
Code projects. Each forum *topic* is an independent "tab" bound to a
``(project, agent)`` pair and keeps its own rolling ``claude -p`` session.

Model: in a Telegram **forum group**, every topic is an independent tab. Each
topic is bound to one *project* (a workspace) and one of its *agents* (a
subagent in ``<workspace>/.claude/agents/*.md`` or the synthetic 💬 Generale),
and keeps its own rolling Claude session — so you can have e.g. "Cosmos ·
builder" and "Cosmos · solver" running side by side, each replying in its own
topic. In a private chat (no topics) everything lives in a single implicit
topic (id 0), so the bot still works 1:1.

Routing key is ``(chat_id, topic_id)`` where ``topic_id = message_thread_id or
0``. Binding (which project+agent a topic talks to) and live sessions are
persisted to ``STATE_PATH`` so a restart keeps topics working.

Auth: subscription OAuth (mount ``~/.claude`` read-only). Never set
``ANTHROPIC_API_KEY`` — it would override the subscription.
"""

from __future__ import annotations

import asyncio
import collections
import contextvars
import dataclasses
import datetime as dt
import html as html_mod
import json
import logging
import io
import os
import re
import secrets
import shlex
import time
from pathlib import Path
from typing import NamedTuple

from telegram import (
    Bot,
    BotCommand,
    BotCommandScopeChat,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputFile,
    Update,
)
from telegram.constants import ChatAction, ParseMode
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    ApplicationHandlerStop,
    CallbackQueryHandler,
    ChatMemberHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    TypeHandler,
    filters,
)

import audio_preprocess
import bot_registry
import chat_session
import claude_runner
import dm_replies
import hub
import group_menu
import group_register
import hub_config
import life_registry_client
import llm_usage
import indicazioni
import manifest
import migrate
import model_menu
import model_prefs
import node_menu
import node_routing
import reparto_routing
import agent_nodes
import ownership
import pipeline
import pipeline_stages
import profiles
import instance_acl
import instance_routing
import life_access
import dm_routing
import projects
import remote_cards
import replay_unanswered
import restructure
import scheduler
import task_card
import task_routing
import message_guard
import taskgen
import team_commands
import telegram_api
import wake_routing
import voice_inbox
from atomic_io import replace_with_retry
from cb_pack import pack_cb, parse_cb
from claude_runner import (
    CLAUDE_BIN,
    CLAUDE_IDLE_TIMEOUT,
    CLAUDE_MAX_TIMEOUT,
    CLAUDE_MODEL,
    CLAUDE_SETTING_SOURCES,
    CLAUDE_STREAM_LIMIT,
    CLAUDE_STRICT_MCP,
    SUITE_MCP_CONFIG,
    _claude_gate,
    _event_progress,
    _parse_stream_line,
    _result_from_event,
    run_claude,
)
from functionalities import (
    Functionality,
    functionalities_of,
    has_functionalities,
    resolve_functionality,
)
from live_session import LiveSessionManager, LiveSessionUnavailable
from profiles import Profile, can_access, profile_for, visible_projects
from projects import (
    Agent,
    Project,
    agent_button_label,
    agents_of,
    list_projects,
    resolve_agent,
    resolve_agent_by_intent,
    resolve_project,
)

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s | %(message)s",
    level=logging.INFO,
)
# httpx logs one line per Telegram getUpdates poll (~every 10s); keep it out of
# the way so `docker logs` shows only our routing/claude lines.
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger("coordinator")

# Un solo controllo su tutto cio' che esce verso Telegram (vuoti, segnaposto, ripetizioni).
message_guard.install(Bot)

# --- Config (use .get so importing the module never raises in tests) ---------
TOKEN = bot_registry.default_token()  # TELEGRAM_BOT_TOKEN_FILE, else TELEGRAM_BOT_TOKEN (#83)
# Authorize by *user* id (works both in private chats and in groups, where the
# chat id is the group's). Accept ALLOWED_USER_IDS, fall back to the older
# ALLOWED_CHAT_IDS (same values: Telegram user ids).
_allowed_raw = os.environ.get("ALLOWED_USER_IDS") or os.environ.get(
    "ALLOWED_CHAT_IDS", ""
)
ALLOWED_USER_IDS = {int(x) for x in _allowed_raw.split(",") if x.strip()}
# Progetti il cui gruppo Telegram autorizza *chiunque ne sia membro*, senza
# passare dalla lista di user id. Non e' un modello nuovo: i gruppi dedicati
# (handle_project_group_message) sono gia' 'whole group trusted'; questo porta
# la stessa regola ai forum a topic, che passano invece da handle_message.
#
# E' opt-in per progetto e vuoto di default: aprire un gruppo significa che la
# sicurezza si sposta su CHI PUO' AGGIUNGERE MEMBRI in quel gruppo. Se i membri
# possono invitare (can_invite_users), chiunque sia dentro puo' far entrare
# altri e l'accesso si propaga senza passare da qui.
#
# L'ospite non e' mai admin e vede SOLO il progetto del suo gruppo: i comandi
# amministrativi restano dietro _allowed()/_is_admin(), che non tocca.
OPEN_GROUP_PROJECTS = {
    s.strip() for s in os.environ.get("OPEN_GROUP_PROJECTS", "").split(",")
    if s.strip()
}
# Progetti i cui turni Telegram girano SULL'HOST invece che nel container,
# con la stessa parita' di un terminale (issue #46, finora solo per i wake del
# bus): hook e plugin utente, `gh`/ssh dell'utente, modello del device, cwd
# reale. Nato il 2026-09-30 per ComUnity, dove il gruppo deve poter fare quello
# che fa una sessione aperta sul PC — correggere, pushare, ridistribuire — e
# nel container lean mancavano pezzi (jq, flutter, credenziali, hook di sync).
# Richiede HOST_EXEC_SSH configurato e un host_path del progetto su questo
# device; se manca uno dei due il turno resta nel container, come prima.
HOST_EXEC_TOPIC_PROJECTS = {
    s.strip() for s in os.environ.get("HOST_EXEC_TOPIC_PROJECTS", "").split(",")
    if s.strip()
}


def _topic_host_cwd(project) -> str | None:
    """Host path a Telegram turn of ``project`` runs in, or ``None`` = container."""
    if project.key not in HOST_EXEC_TOPIC_PROJECTS:
        return None
    return hub.wake_host_cwd(project)
# Per-progetto override delle chiavi API dei provider (DEEPSEEK_API_KEY,
# OPENCODE_API_KEY per i modelli "Zen", ANTHROPIC_API_KEY per Claude a consumo),
# per tenere separato il monitoraggio costi di un progetto (es. ComUnity,
# gruppo con ospiti esterni) dal resto senza toccare le chiavi globali.
# Convenzione: `<PROVIDER>_API_KEY_<PROJECT_KEY maiuscolo>` in .env -> applicata
# SOLO alle chiamate run_claude di quel progetto (extra_env); tutti gli altri
# progetti continuano a usare le chiavi globali. ANTHROPIC_API_KEY non va MAI
# messa globale (vedi il docstring del modulo): solo come override per-progetto,
# cosi' la subscription OAuth resta il default ovunque finche' un progetto non
# sceglie esplicitamente "Claude (API key)" da /modello.
_PROJECT_ENV_PREFIXES = ("DEEPSEEK_API_KEY_", "OPENCODE_API_KEY_", "ANTHROPIC_API_KEY_")
PROJECT_API_KEY_OVERRIDES: dict[str, dict[str, str]] = {}
for _k, _v in os.environ.items():
    if not _v.strip():
        continue
    for _prefix in _PROJECT_ENV_PREFIXES:
        if _k.startswith(_prefix):
            _proj = _k[len(_prefix):].lower()
            _var = _prefix[:-1]  # drop the trailing "_" before the project key
            PROJECT_API_KEY_OVERRIDES.setdefault(_proj, {})[_var] = _v.strip()
            break


class ModelChoice(NamedTuple):
    """One /modello button: which backend+model it runs, and what unlocks it.

    ``key_env is None`` means always available (Claude subscription OAuth,
    mounted read-only, no key to set). Otherwise the choice only appears once
    that env var is set -- globally, or as a `<key_env>_<PROJECT>` override --
    so the catalog activates/deactivates itself as keys are added, instead of
    a hardcoded per-deployment list.
    """

    id: str
    backend: str  # "claude" | "opencode"
    model: str  # backend-specific: claude alias (sonnet/opus/...) or "provider/model-id"
    label: str
    key_env: str | None


# The Zen entries are opencode's free-tier catalog as of 2026-09-01 (`opencode
# models` inside the bot container) -- it rotates, re-check there periodically.
MODEL_CATALOG: list[ModelChoice] = [
    ModelChoice("claude-sub", "claude", "sonnet", "Claude (abbonamento)", None),
    ModelChoice("claude-api", "claude", "sonnet", "Claude (API key)", "ANTHROPIC_API_KEY"),
    ModelChoice("deepseek-flash", "opencode", "deepseek/deepseek-v4-flash", "DeepSeek V4 Flash", "DEEPSEEK_API_KEY"),
    ModelChoice("deepseek-pro", "opencode", "deepseek/deepseek-v4-pro", "DeepSeek V4 Pro", "DEEPSEEK_API_KEY"),
    ModelChoice("zen-big-pickle", "opencode", "opencode/big-pickle", "Zen . Big Pickle", "OPENCODE_API_KEY"),
    ModelChoice("zen-hy3", "opencode", "opencode/hy3-free", "Zen . HY3", "OPENCODE_API_KEY"),
    ModelChoice("zen-mimo", "opencode", "opencode/mimo-v2.5-free", "Zen . MiMo v2.5", "OPENCODE_API_KEY"),
    ModelChoice("zen-muse-spark", "opencode", "opencode/muse-spark-1.2-contributor-free", "Zen . Muse Spark 1.2", "OPENCODE_API_KEY"),
    ModelChoice("zen-nemotron-ultra", "opencode", "opencode/nemotron-3-ultra-free", "Zen . Nemotron 3 Ultra", "OPENCODE_API_KEY"),
    ModelChoice("zen-nemotron-lightning", "opencode", "opencode/nemotron-3.5-lightning-free", "Zen . Nemotron 3.5 Lightning", "OPENCODE_API_KEY"),
    # Generic OpenCode pick from the live `opencode models` list (#68): the
    # real model rides in Prefs.model, this entry only says "opencode backend,
    # OPENCODE_API_KEY". `model` is the fallback if a card lost its model.
    ModelChoice("opencode", "opencode", claude_runner.OPENCODE_MODEL, "OpenCode", "OPENCODE_API_KEY"),
]
CATALOG_BY_ID: dict[str, ModelChoice] = {c.id: c for c in MODEL_CATALOG}


def _project_key_for_topic(chat_id: int, topic_id: int) -> str:
    binding = TOPIC_BINDING.get((chat_id, topic_id))
    return binding[0] if binding else ""


def _choice_key(project_key: str, key_env: str) -> str | None:
    """Resolve one provider key for this project: its override if set, else
    the global env var, else None (choice unavailable)."""
    override = PROJECT_API_KEY_OVERRIDES.get(project_key.lower(), {}).get(key_env)
    return override or os.environ.get(key_env, "").strip() or None


def _choice_available(project_key: str, choice: ModelChoice) -> bool:
    if choice.key_env is None:
        # The subscription has no key to set, but it does need OAuth
        # credentials: without them the button promised a turn that 401s (#68).
        return model_menu.claude_subscription_connected()
    return _choice_key(project_key, choice.key_env) is not None


def _available_choices(project_key: str) -> list[ModelChoice]:
    return [c for c in MODEL_CATALOG if _choice_available(project_key, c)]


def _default_opencode_model() -> str:
    """The OpenCode model /compara's "opencode" alias runs, and the fallback
    when no per-choice model applies. Overridable at runtime with
    /opencodemodel (persisted in _RUNTIME_SETTINGS); falls back to the
    deployment env default (claude_runner.OPENCODE_MODEL) untouched."""
    return _RUNTIME_SETTINGS.get("opencode_default_model") or claude_runner.OPENCODE_MODEL


def _set_default_opencode_model(model: str | None) -> None:
    if model:
        _RUNTIME_SETTINGS["opencode_default_model"] = model
    else:
        _RUNTIME_SETTINGS.pop("opencode_default_model", None)
    _save_state()


class _CompareEntry(NamedTuple):
    """One resolved /compara column: what to run and with which key."""

    label: str
    backend: str
    model: str
    extra_env: dict[str, str] | None


def _resolve_compara_token(token: str, project_key: str) -> "_CompareEntry | str":
    """One /compara token -> what to run, or an error string if unavailable.

    Accepts three short aliases (deepseek/claude/opencode -- the trio /compara
    defaults to with no arguments), ``opencode:<model-id>`` for an arbitrary
    OpenCode model (the Zen catalog rotates, so a fixed alias per model would
    go stale), and any id from /modello's MODEL_CATALOG for a specific pinned
    choice (e.g. ``zen-hy3``, ``claude-api``). Same key-isolation discipline as
    _resolve_turn: extra_env carries only the one key this entry needs.
    """
    raw = token.strip()
    t = raw.lower()
    if t in ("opencode", "oc"):
        model = _default_opencode_model()
    elif t.startswith("opencode:") or t.startswith("oc:"):
        model = raw.split(":", 1)[1].strip()
        if not model:
            return f"{raw}: manca il modello dopo i due punti"
    else:
        model = None

    if model is not None:
        key = _choice_key(project_key, "OPENCODE_API_KEY")
        if not key:
            return f"{raw}: manca OPENCODE_API_KEY"
        return _CompareEntry(f"OpenCode \u00b7 {model}", "opencode", model, {"OPENCODE_API_KEY": key})

    if t in ("deepseek", "ds"):
        t = "deepseek-flash"
    elif t in ("claude", "claude-sub", "abbonamento"):
        t = "claude-sub"

    choice = CATALOG_BY_ID.get(t)
    if choice is None:
        return (
            f"{raw}: non riconosciuto (deepseek, claude, opencode, opencode:<modello>, "
            "o un id di /modello)"
        )
    if not _choice_available(project_key, choice):
        return f"{choice.label}: manca {choice.key_env}"
    extra_env = None
    if choice.key_env:
        val = _choice_key(project_key, choice.key_env)
        if val:
            extra_env = {choice.key_env: val}
    return _CompareEntry(choice.label, choice.backend, choice.model, extra_env)


def _resolve_turn(
    project_key: str, prefs: model_prefs.Prefs
) -> tuple[str | None, str | None, dict[str, str] | None]:
    """(backend, model, extra_env) for this turn from sticky/inline prefs.

    extra_env carries ONLY the single key the chosen catalog entry needs --
    never a blanket per-project dump -- so picking "Claude (abbonamento)"
    never leaks an ANTHROPIC_API_KEY into the process (that would silently
    switch the CLI from subscription to metered billing) even when this same
    project also has an "API key" override configured for the other choice.
    A plain-text model (legacy ``/modello opus`` or an inline ``opus:``
    prefix) still wins over the catalog's default model, e.g. picking
    "Claude (abbonamento)" then typing ``opus: ...`` for one turn keeps the
    subscription auth but switches the model for that message.
    """
    choice = CATALOG_BY_ID.get(prefs.choice_id) if prefs.choice_id else None
    if choice is None:
        return None, prefs.model, None
    backend = choice.backend
    model = prefs.model or choice.model
    extra_env = None
    if choice.key_env:
        val = _choice_key(project_key, choice.key_env)
        if val:
            extra_env = {choice.key_env: val}
    return backend, model, extra_env


# Per-message ``claude -p`` boot knobs (CLAUDE_BIN, CLAUDE_MODEL,
# CLAUDE_SETTING_SOURCES, CLAUDE_STRICT_MCP, timeouts, stream limit, spawn
# concurrency) and the spawner itself now live in claude_runner so headless
# workers can reuse them without importing Telegram; imported at the top.
# Persistent interactive sessions (Step 4): keep ONE warm ``claude`` process per
# (chat, topic) and feed turns over its stdin — no cold start per message, live
# context, mid-task interrupt. Off by default; falls back to the cold spawn on any
# anomaly. Opt in with CLAUDE_LIVE_SESSIONS=1.
CLAUDE_LIVE_SESSIONS_REQUESTED = os.environ.get(
    "CLAUDE_LIVE_SESSIONS", "0"
).strip().lower() not in (
    "0",
    "false",
    "no",
    "",
)
# ...but the warm path speaks the claude stream-json protocol and spawns
# CLAUDE_BIN: it has no opencode implementation. Enabling it under
# AI_BACKEND=opencode would silently split the deployment in two — interactive
# Telegram turns on claude, everything else (wakes, notifiche, butler) on
# opencode — with a different model, different tooling and no trace in the logs.
# The backend choice wins: the live path stays off and every turn goes through
# claude_runner.run_claude, which honours AI_BACKEND. on_startup says so out loud.
CLAUDE_LIVE_SESSIONS = (
    CLAUDE_LIVE_SESSIONS_REQUESTED and claude_runner.AI_BACKEND == "claude"
)
# Max warm live processes kept resident at once (LRU-evicted past this). Each is a
# ~150 MB node, so this is the warm-path analogue of CLAUDE_MAX_CONCURRENCY.
# 0 = unlimited (rely on the idle TTL only).
CLAUDE_MAX_LIVE_SESSIONS = int(os.environ.get("CLAUDE_MAX_LIVE_SESSIONS", "4"))
# CLAUDE_TIMEOUT, CLAUDE_IDLE_TIMEOUT, CLAUDE_MAX_TIMEOUT, CLAUDE_STREAM_LIMIT and
# CLAUDE_MAX_CONCURRENCY live in claude_runner (imported above) — the cold-spawn
# path uses them there; the live-session path reads them via those imports too.
# Sliding inactivity window for a conversation/topic -> Claude session_id mapping
# (persisted in state.json, resumed via --resume). 86400 = 24h so a follow-up
# after a long pause (e.g. a ~90-min pressure-cooker run, an overnight gap) still
# resumes the same session instead of starting empty.
SESSION_TTL_SEC = int(os.environ.get("SESSION_TTL_SEC", "86400"))
STALE_MAX_AGE_SEC = int(os.environ.get("STALE_MAX_AGE_SEC", "600"))
# How often to re-fetch Coordinator's project registry from Life's live graph
# (see projects.refresh_projects, Task 4). Default 7 minutes.
PROJECTS_REFRESH_INTERVAL_SEC = int(
    os.environ.get("PROJECTS_REFRESH_INTERVAL_SEC", str(7 * 60))
)
TZ_NAME = os.environ.get("TZ", "Europe/Rome")
STATE_PATH = os.environ.get("STATE_PATH", "/app/data/state.json")
# Append-only conversation log (one JSON object per turn): the source of truth
# for debugging and for reconstructing a topic's history without screenshots.
CONV_LOG_PATH = os.environ.get("CONV_LOG_PATH", "/app/data/conversations.jsonl")
# Registro degli invii, per /pulisci: accanto all'archivio dei turni.
message_guard.configure(
    os.environ.get("SENT_LOG_PATH") or os.path.join(os.path.dirname(CONV_LOG_PATH), "sent_messages.jsonl")
)
MSG_LOG_PATH = os.environ.get("MSG_LOG_PATH", "/app/data/messages.jsonl")
# Messages a handler answered WITHOUT an agent turn (a voice note queued to the
# brain, a lesson acknowledged, a DM routed to a node): conversations.jsonl
# never hears of them, and the replay must not take them for unanswered.
HANDLED_LOG_PATH = os.environ.get("HANDLED_LOG_PATH", "/app/data/handled.jsonl")
# (chat_id, message_id) of the human message the running turn answers. Set by
# _invoke_and_reply, read by _log_turn: the turn row then names its message,
# whatever the prompt looks like (a voice transcript, a photo path...).
_TURN_MSG: contextvars.ContextVar[tuple | None] = contextvars.ContextVar(
    "turn_msg", default=None
)
# Set while the replay pushes an old message back through the handlers: a dict
# the handlers fill with the outcome ("ok"/"failed") and read the archived row
# from ("rec"). None on every live update.
_REPLAY: contextvars.ContextVar[dict | None] = contextvars.ContextVar(
    "replay", default=None
)
RATING_LOG_PATH = os.environ.get("RATING_LOG_PATH", "/app/data/ratings.jsonl")
# Shared Coordinator forum hub: the default landing place for M5 task.due cards
# (one auto-created forum topic per specialist project). A specialist project's
# own dedicated group is a shared ingestor channel and must NOT receive personal
# task cards — hence the hub, not the project group, is the default target.
# Sourced (in order) from COORDINATOR_FORUM_CHAT_ID, else the existing per-device
# HUB_TELEGRAM_CHAT_ID (the "Agents" forum this bot already manages topics in);
# 0 disables the hub route (callers then fall back to the owner's DM).
COORDINATOR_FORUM_CHAT_ID = int(
    os.environ.get("COORDINATOR_FORUM_CHAT_ID")
    or os.environ.get("HUB_TELEGRAM_CHAT_ID")
    or "0"
)
# Scheduled notifications run on exactly one device (the always-on minipc):
# with two coordinator-bot instances a shared default would double-send.
SCHEDULER_ENABLED = os.environ.get("SCHEDULER_ENABLED", "1").strip().lower() not in (
    "0",
    "false",
    "no",
)
# Dynamic ownership runtime refs (manager + config), set in main() when enabled.
_OWNERSHIP: dict = {}

TELEGRAM_CHUNK = 3500

# --- State (in-memory, mirrored to STATE_PATH) -------------------------------
# (chat_id, topic_id) -> (project_key, agent_name)   what a topic is bound to
TOPIC_BINDING: dict[tuple[int, int], tuple[str, str]] = {}
# Explicit instance of a binding (#83, F1), stored only when the bind menu had
# to ask: a node living in more than one instance. Everything else is derived
# (instance forum, then the graph) by _binding_instance().
TOPIC_INSTANCE: dict[tuple[int, int], str] = {}
# (chat_id, topic_id) -> (session_id, last_used)
SESSIONS: dict[tuple[int, int], tuple[str, dt.datetime]] = {}
# (chat_id, topic_id) -> sticky model/effort chosen with /modello. Absent means
# "use the env default"; an inline ``opus/high:`` prefix overrides it per turn.
TOPIC_PREFS: dict[tuple[int, int], model_prefs.Prefs] = {}
# (chat_id, topic_id) -> key of the last group-menu button tapped there, for
# projects declaring ``[telegram] menu_resets_session`` (persisted).
_LAST_MENU_KEY: dict[tuple[int, int], str] = {}
# (chat_id, thread_id) -> forum topic name. Telegram does not put the name on
# ordinary messages: it is learnt when the bot creates a topic, from the
# forum_topic_created/edited service messages, and lazily from the implicit
# reply every topic message carries (persisted).
TOPIC_NAMES: dict[tuple[int, int], str] = {}
# (chat_id, topic_id) -> sticky MODE chosen from a drill-down menu:
# {project, menu_key, label, cmd, path, breadcrumb, day}. While set, free-text
# turns carry "[Modalità: …]" so nobody retypes the command (persisted).
TOPIC_MODES: dict[tuple[int, int], dict] = {}
# Drill-down menu buttons: short id -> action (callback_data is 64 bytes max).
_DRILL_REGISTRY = chat_session.CallbackRegistry()
# Deployment-wide settings changeable at runtime via a command instead of a
# .env edit + restart -- currently just the OpenCode default model used by
# /compara's "opencode" alias (/opencodemodel to change it). Small and
# JSON-persisted (see _save_state/_load_state's "settings" key) on purpose:
# a dict, not a dataclass, so a second such setting doesn't need new plumbing.
_RUNTIME_SETTINGS: dict[str, str] = {}
# taskgen (M5 Slice B): anti-doppione set across watcher ticks, persisted like
# the rest of this module's state (see _save_state/_load_state's
# "taskgen_dedup" key) so a bot restart doesn't re-emit a suggestion still
# inside its TTL. Rebound (not mutated in place) by _load_state on startup.
_TASKGEN_DEDUP = taskgen.Dedup()

# butler (M5 maggiordomo proattivo, Slice A reorg + Slice C prep): singleton
# dedup + in-memory caches, gemelli of _TASKGEN_DEDUP/_TASK_DUE_CACHE above.
# Rebound (not mutated in place) by _load_state on startup.
_BUTLER_DEDUP = taskgen.Dedup()
# token (secrets.token_urlsafe) -> reorg proposal dict (kind/st_ids/new_start_iso/
# rationale/telegram_chat_id) — the "rg:apply|no:<token>" callback data can only
# carry a short token (Telegram's 64-byte callback_data cap), so the actual
# proposal lives here, keyed by that token.
_REORG_CACHE: dict[str, dict] = {}
# scheduled_task_id -> prepared artifact text (Slice C), shown on tap of the
# task.due card's optional "Preparazione" button.
_PREP_CACHE: dict[int, str] = {}
# token (secrets.token_urlsafe) -> preparation action dict proposed by the
# `side_effects` prep mode (#20): {"kind", "to", "subject", "body", "rationale",
# "st_id", "st_name"}. Twin of _REORG_CACHE — the "pa:ok|no:<token>" callback
# data only carries the token, the action itself lives here. NOTHING in this
# cache has been executed: an entry is a REQUEST for confirmation, and it turns
# into a real side effect only on an explicit [✅ Conferma] tap.
_PREP_ACTION_CACHE: dict[str, dict] = {}
# Append-only audit of every proposed/confirmed/discarded/executed action (#20).
# Capped at _BUTLER_AUDIT_MAX so the persisted state cannot grow without bound.
_BUTLER_AUDIT: list[dict] = []
_BUTLER_AUDIT_MAX = 200


def _save_state() -> None:
    """Persist bindings + sessions to STATE_PATH (best-effort)."""
    d = os.path.dirname(STATE_PATH)
    if d and not os.path.isdir(d):
        return  # no writable data dir (e.g. under tests) -> stay in-memory
    try:
        data = {
            "topics": {
                f"{c}:{t}": [pk, an] for (c, t), (pk, an) in TOPIC_BINDING.items()
            },
            "instances": {f"{c}:{t}": i for (c, t), i in TOPIC_INSTANCE.items()},
            "sessions": {
                f"{c}:{t}": [sid, last.isoformat()]
                for (c, t), (sid, last) in SESSIONS.items()
            },
            # Sticky per-card model/effort (/modello): survives restarts, so a
            # card set to opus/max stays there without being re-armed.
            "prefs": {f"{c}:{t}": p.to_json() for (c, t), p in TOPIC_PREFS.items()},
            # M5 task-due tracking: persist so a bot restart still supersedes the
            # prior live card (task_due_msg) and its taps still resolve context
            # (task_due_cache). Keyed by scheduled_task_id (str for JSON).
            "task_due_msg": {str(k): [c, m] for k, (c, m) in _TASK_DUE_MSG.items()},
            "task_due_cache": {str(k): v for k, v in _TASK_DUE_CACHE.items()},
            # taskgen (M5 Slice B) dedup set — see _TASKGEN_DEDUP comment above.
            "taskgen_dedup": _TASKGEN_DEDUP.to_dict(),
            # butler (M5 maggiordomo proattivo): reorg proposal cache, prep
            # artifact cache, dedup set — see the _BUTLER_DEDUP block above.
            "reorg_cache": dict(_REORG_CACHE),
            "prep_cache": {str(k): v for k, v in _PREP_CACHE.items()},
            "butler_dedup": _BUTLER_DEDUP.to_dict(),
            # #20 side effects: pending confirmations + the capped audit trail.
            "prep_action_cache": dict(_PREP_ACTION_CACHE),
            "butler_audit": _BUTLER_AUDIT[-_BUTLER_AUDIT_MAX:],
            "settings": dict(_RUNTIME_SETTINGS),
            "gruppi_in_attesa": {str(k): v for k, v in _GRUPPI_IN_ATTESA.items()},
            "last_menu_key": {f"{c}:{t}": k for (c, t), k in _LAST_MENU_KEY.items()},
            "topic_names": {f"{c}:{t}": n for (c, t), n in TOPIC_NAMES.items()},
            "modes": {f"{c}:{t}": m for (c, t), m in TOPIC_MODES.items()},
        }
        tmp = STATE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        replace_with_retry(tmp, STATE_PATH)
    except Exception:
        logger.exception("could not persist state to %s", STATE_PATH)


def _load_state() -> None:
    """Load bindings + sessions from STATE_PATH (best-effort).

    Migrates the legacy single-level shape (``topics`` value was a bare agent
    key) to the two-level ``(project_key, agent_name)`` shape, defaulting the
    agent to ``generale``. Bindings whose project no longer resolves are dropped.
    """
    try:
        with open(STATE_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return
    except Exception:
        logger.exception("could not load state from %s", STATE_PATH)
        return

    def _key(s: str) -> tuple[int, int]:
        c, t = s.split(":", 1)
        return int(c), int(t)

    for s, v in (data.get("topics") or {}).items():
        if isinstance(v, str):  # legacy: bare agent/project key
            pk, an = v, "generale"
        else:
            pk, an = v[0], (v[1] if len(v) > 1 else "generale")
        if resolve_project(pk):
            TOPIC_BINDING[_key(s)] = (pk, an)
    for s, iid in (data.get("instances") or {}).items():
        try:
            if isinstance(iid, str) and iid:
                TOPIC_INSTANCE[_key(s)] = iid
        except ValueError:
            continue
    for s, (sid, iso) in (data.get("sessions") or {}).items():
        try:
            SESSIONS[_key(s)] = (sid, dt.datetime.fromisoformat(iso))
        except Exception:
            continue
    _RUNTIME_SETTINGS.clear()
    _RUNTIME_SETTINGS.update(data.get("settings") or {})
    for s, v in (data.get("prefs") or {}).items():
        try:
            p = model_prefs.Prefs.from_json(v)
        except Exception:
            continue
        if p:  # drop empties rather than persisting no-op entries
            TOPIC_PREFS[_key(s)] = p
    # M5 task-due tracking (restart-durable supersede + tap context).
    for k, v in (data.get("task_due_msg") or {}).items():
        try:
            _TASK_DUE_MSG[int(k)] = (int(v[0]), int(v[1]))
        except Exception:
            continue
    for k, v in (data.get("task_due_cache") or {}).items():
        try:
            _TASK_DUE_CACHE[int(k)] = v
        except Exception:
            continue
    # taskgen (M5 Slice B) dedup set — rebind (not mutate) from the persisted dict.
    global _TASKGEN_DEDUP
    td = data.get("taskgen_dedup")
    if isinstance(td, dict):
        try:
            _TASKGEN_DEDUP = taskgen.Dedup.from_dict(td)
        except Exception:
            logger.exception("could not restore taskgen dedup from state")
    # butler (M5 maggiordomo proattivo): reorg/prep caches + dedup set. Retro-compat:
    # keys absent (older state.json) -> defaults stay empty, never crashes.
    for tok, proposal in (data.get("reorg_cache") or {}).items():
        if isinstance(tok, str) and isinstance(proposal, dict):
            _REORG_CACHE[tok] = proposal
    for k, v in (data.get("prep_cache") or {}).items():
        try:
            _PREP_CACHE[int(k)] = str(v)
        except Exception:
            continue
    for tok, action in (data.get("prep_action_cache") or {}).items():
        if isinstance(tok, str) and isinstance(action, dict):
            _PREP_ACTION_CACHE[tok] = action
    for rec in (data.get("butler_audit") or [])[-_BUTLER_AUDIT_MAX:]:
        if isinstance(rec, dict):
            _BUTLER_AUDIT.append(rec)
    global _BUTLER_DEDUP
    bd = data.get("butler_dedup")
    if isinstance(bd, dict):
        try:
            _BUTLER_DEDUP = taskgen.Dedup.from_dict(bd)
        except Exception:
            logger.exception("could not restore butler dedup from state")
    for s_key, v in (data.get("last_menu_key") or {}).items():
        try:
            _LAST_MENU_KEY[_key(s_key)] = str(v)
        except (TypeError, ValueError):
            continue
    for s_key, v in (data.get("modes") or {}).items():
        try:
            if isinstance(v, dict):
                TOPIC_MODES[_key(s_key)] = dict(v)
        except (TypeError, ValueError):
            continue
    for s_key, v in (data.get("topic_names") or {}).items():
        try:
            TOPIC_NAMES[_key(s_key)] = str(v)
        except (TypeError, ValueError):
            continue
    for k, v in (data.get("gruppi_in_attesa") or {}).items():
        try:
            _GRUPPI_IN_ATTESA[int(k)] = [str(x) for x in v]
        except (TypeError, ValueError):
            continue
    logger.info(
        "loaded %d topic bindings, %d sessions", len(TOPIC_BINDING), len(SESSIONS)
    )


# Gruppi che hanno visto il menu «a quale progetto» e non sono stati legati:
# {chat_id: [chiavi offerte]}. Persistito, cosi' il menu si ripropone quando il
# registro porta un progetto che quel gruppo non ha mai visto (#53).
_GRUPPI_IN_ATTESA: dict[int, list[str]] = {}


def seed_group_bindings() -> None:
    """Pre-bind each manifest group's General topic to its (project, agent).

    Lets a project's own group route to its claude session without anyone opening
    a /progetti session there. Idempotent; persisted via _bind_topic.
    """
    import manifest

    for chat_id, thread_id, project_key, agent_name in manifest.group_bindings():
        if resolve_project(project_key) is not None:
            _bind_topic(chat_id, thread_id, project_key, agent_name)



# --- Legare un gruppo a un progetto, senza cercare il chat_id a mano ----------
# La parte pura (menu, riscrittura del manifesto) sta in group_register.py.


# Quale meta' della rete serve questo bot. Un gruppo appartiene a un mondo solo,
# e lo serve il bot di quel mondo: il bot work lega gruppi work, quello personale
# gruppi personali. Non dichiararlo significa "non lo so" e mostra tutto.
COORDINATOR_KIND = os.environ.get(group_register.AMBITO_ENV, "").strip()


def _progetti_legabili() -> list[tuple[str, str]]:
    """I progetti che questo bot puo' legare a un gruppo, nel suo ambito.

    In coda le istanze Life registrate sul device che non hanno ancora un gruppo
    Telegram (#82): collegare un'istanza chiede all'umano solo di creare il
    gruppo e aggiungere il bot. Non filtrate per ambito: un'istanza e' un
    mondo intero, e il bot del device e' l'unico che la puo' collegare.
    """
    by_instance, unmapped = _progetti_per_istanza()
    # Instance first (#83, F1): the nodes of an instance are one tap below it.
    # Projects the graph does not place in any instance keep today's flat list,
    # filtered by COORDINATOR_KIND as before.
    import instance_link

    istanze = set(by_instance) | set(instance_link.unlinked())
    flat = group_register.visibili(
        [(k, lbl, kind) for k, lbl, kind in unmapped], COORDINATOR_KIND
    )
    return instance_routing.instance_entries(istanze) + flat


def _progetti_per_istanza():
    """``({instance: [(key, label)]}, [(key, label, kind)] unmapped)`` of the registry."""
    progetti = list_projects()
    nodes = _routing_index()
    by_instance, _ = instance_routing.group_by_instance(
        [(p.key, p.label, instance_routing.project_node(p)) for p in progetti], nodes
    )
    unmapped = [
        (p.key, p.label, p.kind) for p in progetti
        if not instance_routing.instances_of_node(instance_routing.project_node(p), nodes)
    ]
    return by_instance, unmapped


def _menu_istanza(instance_id: str, indice: int = 0) -> InlineKeyboardMarkup:
    """The second level: the nodes of one instance (+ its forum, if unlinked)."""
    import instance_link

    by_instance, _ = _progetti_per_istanza()
    entries = instance_routing.node_entries(instance_id, by_instance.get(instance_id, []))
    channel = (f"{instance_link.KEY_PREFIX}{instance_id}"
               if instance_id in instance_link.unlinked() else None)
    righe = instance_routing.node_page(instance_id, entries, indice, channel_key=channel)
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(b.testo, callback_data=b.dato) for b in riga] for riga in righe]
    )


def _menu_gruppo(indice: int = 0) -> InlineKeyboardMarkup:
    """La tastiera dei progetti a cui un gruppo puo' essere legato."""
    righe, _ = group_register.pagina(_progetti_legabili(), indice)
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(b.testo, callback_data=b.dato) for b in riga]
         for riga in righe]
    )


async def on_my_chat_member(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Il bot e' appena entrato in un gruppo: chiede subito a chi serve.

    E' l'unico momento in cui la domanda si fa da se'. Aspettare che qualcuno si
    ricordi un comando e' esattamente il passo che rendeva la procedura una
    caccia al `chat_id`.
    """
    ev = update.my_chat_member
    if ev is None or ev.chat.type not in ("group", "supergroup"):
        return
    dentro = {"member", "administrator"}
    if ev.new_chat_member.status not in dentro:
        # Uscito dal gruppo: non c'e' piu' nessuno a cui riproporre il menu.
        if _GRUPPI_IN_ATTESA.pop(ev.chat.id, None) is not None:
            _save_state()
        return
    if ev.old_chat_member.status in dentro:
        return  # promozioni di un bot gia' dentro: il menu l'ha gia' avuto
    chat = ev.chat
    gia = TOPIC_BINDING.get((chat.id, 0))
    if gia is not None:
        await context.bot.send_message(
            chat.id, f"Sono gia' legato a *{gia[0]}* qui.", parse_mode=ParseMode.MARKDOWN
        )
        return
    logger.info("aggiunto al gruppo %s (%s)", chat.title, chat.id)
    await _chiedi_a_chi_serve(context, chat, entrato=True)


async def _chiedi_a_chi_serve(context, chat, *, entrato: bool) -> None:
    """Manda nel gruppo la domanda + il menu dei progetti legabili.

    Due porte d'ingresso, un solo testo: l'ingresso del bot nel gruppo e il
    comando `/lega`. Il comando non e' un ripiego per quando l'automatismo
    fallisce — serve il caso in cui l'automatismo NON PUO' scattare: il bot era
    gia' nel gruppo prima che questo flusso esistesse, e `my_chat_member` e' un
    evento di transizione che non si ripete. Senza il comando quei gruppi
    resterebbero legabili solo togliendo e rimettendo il bot.
    """
    legabili = _progetti_legabili()
    # Registrato prima di sapere se il menu e' vuoto: un gruppo senza progetti da
    # offrire e' proprio quello che il prossimo refresh deve ritrovare (#53).
    _GRUPPI_IN_ATTESA[chat.id] = [key for key, _ in legabili]
    _save_state()
    if not legabili:
        # Meglio dire perche' il menu e' vuoto che mostrarne uno con la sola
        # uscita, che sembrerebbe "non c'e' niente da legare" invece di
        # "questo bot non serve quel mondo".
        await context.bot.send_message(
            chat.id,
            f"Non ho progetti di ambito `{COORDINATOR_KIND or 'non dichiarato'}` "
            f"da legare a *{chat.title}*.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return
    ambito = f" ({COORDINATOR_KIND})" if COORDINATOR_KIND else ""
    apertura = (
        f"Sono stato aggiunto a *{chat.title}*."
        if entrato
        else f"Gruppo *{chat.title}*."
    )
    await context.bot.send_message(
        chat.id,
        f"{apertura}\nA quale istanza o progetto{ambito} lego questo gruppo?",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=_menu_gruppo(0),
    )


async def cmd_lega(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """`/lega` — riapre il menu di legatura nel gruppo in cui viene scritto.

    Rilegare un gruppo gia' legato e' voluto: e' il modo di correggere una
    scelta sbagliata senza toccare nessun file a mano.
    """
    chat = update.effective_chat
    if chat is None or chat.type not in ("group", "supergroup"):
        await update.effective_message.reply_text(
            "`/lega` si usa dentro il gruppo da legare.", parse_mode=ParseMode.MARKDOWN
        )
        return
    gia = TOPIC_BINDING.get((chat.id, 0))
    if gia is not None:
        await update.effective_message.reply_text(
            f"Gia' legato a *{gia[0]}*. Scegli di nuovo per cambiare.",
            parse_mode=ParseMode.MARKDOWN,
        )
    await _chiedi_a_chi_serve(context, chat, entrato=False)


async def on_group_bind_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Una voce del menu e' stata premuta: pagina, salta, oppure lega davvero."""
    q = update.callback_query
    scelta = instance_routing.read_choice(q.data or "") or group_register.leggi_scelta(
        q.data or ""
    )
    if scelta is None:
        return
    tipo, valore = scelta
    utente = getattr(q, "from_user", None)
    chi = profile_for(utente.id) if utente is not None else None

    if tipo == "apri_istanza":
        if not instance_acl.authorize(chi, valore):
            await q.answer("Non hai accesso a questa istanza.", show_alert=True)
            return
        await q.answer()
        await q.edit_message_text(f"Istanza *{valore}*: quale nodo lego qui?",
                                  parse_mode=ParseMode.MARKDOWN,
                                  reply_markup=_menu_istanza(valore))
        return

    if tipo == "pagina_istanza":
        iid, _, n = valore.rpartition(":")
        await q.answer()
        await q.edit_message_reply_markup(
            reply_markup=_menu_istanza(iid, int(n) if n.isdigit() else 0))
        return

    scelta_istanza = None
    if tipo == "nodo":
        coppia = instance_routing.split_node(valore)
        if coppia is None:
            await q.answer("Scelta non valida.", show_alert=True)
            return
        scelta_istanza, valore = coppia
        tipo = "progetto"

    if tipo == "pagina":
        await q.answer()
        if (q.message.text or "").startswith("Istanza "):
            # «⬅ Istanze» from the second level: back to the top question.
            await q.edit_message_text("A quale istanza o progetto lego questo gruppo?",
                                      reply_markup=_menu_gruppo(int(valore or 0)))
            return
        await q.edit_message_reply_markup(reply_markup=_menu_gruppo(int(valore or 0)))
        return

    if tipo == "salta":
        await q.answer("Ok, per ora niente.")
        await q.edit_message_text(
            "Nessun progetto legato. Aggiungimi di nuovo o usa /progetti quando vuoi."
        )
        return

    if tipo == "istanza":
        if not instance_acl.authorize(chi, valore):
            await q.answer("Non hai accesso a questa istanza.", show_alert=True)
            return
        await _collega_istanza(q, valore)
        return

    project = resolve_project(valore)
    if project is None:
        await q.answer("Progetto sconosciuto.", show_alert=True)
        return
    nodo = instance_routing.project_node(project)
    istanza = scelta_istanza or instance_routing.instance_of_binding(
        q.message.chat.id, nodo, explicit=None,
        chat_owner=_CHAT_INDEX.instance_of(q.message.chat.id), nodes=_routing_index())
    if chi is None or not instance_acl.authorize(chi, istanza):
        # Binding a group to an instance is an admin's or a member's of it (§5.1.4).
        await q.answer("Non puoi legare gruppi a questa istanza.", show_alert=True)
        return
    if scelta_istanza and instance_routing.needs_explicit(nodo, _routing_index()):
        TOPIC_INSTANCE[(q.message.chat.id, 0)] = scelta_istanza
    else:
        TOPIC_INSTANCE.pop((q.message.chat.id, 0), None)
    _save_state()

    import manifest

    chat_id = q.message.chat.id
    tg = manifest.telegram_of(project)
    agente = tg.agent if tg is not None else "generale"
    _bind_topic(chat_id, 0, project.key, agente)
    if _GRUPPI_IN_ATTESA.pop(chat_id, None) is not None:
        _save_state()

    # Il legame vive in due posti apposta: TOPIC_BINDING lo rende attivo subito,
    # il manifesto lo rende la verita' anche dopo un riavvio con lo stato pulito.
    percorso = manifest.toml_path(project)
    scritto = True
    try:
        with open(percorso, encoding="utf-8") as fh:
            testo = fh.read()
        with open(percorso, "w", encoding="utf-8") as fh:
            fh.write(group_register.con_group_chat_id(testo, chat_id))
    except OSError as exc:
        # Un workspace non montato non deve far fallire il legame: quello in
        # memoria funziona lo stesso, e dirlo e' meglio che fingere.
        scritto = False
        logger.warning("manifesto non scritto per %s: %s", project.key, exc)

    await q.answer("Legato.")
    coda = (
        "Scritto anche nel manifesto del progetto."
        if scritto
        else "\u26a0\ufe0f Il manifesto non e' scrivibile da qui: il legame vale "
             "fino al prossimo riavvio."
    )
    await q.edit_message_text(
        f"\u2705 Questo gruppo e' legato a *{project.label}*.\n{coda}\n"
        "Scrivimi qui: quello che mando finisce a questo progetto.",
        parse_mode=ParseMode.MARKDOWN,
    )


async def _collega_istanza(q, instance_id: str) -> None:
    """Il gruppo diventa il canale Telegram dell'istanza (#82).

    Scrittura e push avvengono sull'host (`instance_link`): qui si risponde
    subito alla callback, perche' un push puo' superare i pochi secondi che
    Telegram concede prima di dare il bottone per morto.
    """
    import instance_link

    chat_id = q.message.chat.id
    if instance_id not in instance_link.unlinked():
        await q.answer("Istanza gia' collegata o non registrata qui.", show_alert=True)
        return
    await q.answer("Collego...")
    await q.edit_message_text(f"⏳ Collego questo gruppo all'istanza *{instance_id}*...",
                              parse_mode=ParseMode.MARKDOWN)
    esito = await instance_link.link_telegram(
        instance_id, chat_id, instance_link.telegram_agent()
    )
    if esito.ok:
        if _GRUPPI_IN_ATTESA.pop(chat_id, None) is not None:
            _save_state()
        logger.info("gruppo %s collegato all'istanza %s: %s", chat_id, instance_id, esito.detail)
        await q.edit_message_text(
            f"✅ Questo gruppo e' il canale Telegram dell'istanza *{instance_id}*.\n"
            f"Scritto in instance.config.json e pushato ({esito.detail}).",
            parse_mode=ParseMode.MARKDOWN,
        )
        return
    logger.warning("collegamento istanza %s fallito: %s", instance_id, esito.detail)
    stato = ("scritto ma non pushato" if esito.written else "non scritto")
    await q.edit_message_text(
        f"⚠️ Istanza *{instance_id}*: collegamento {stato}.\n{esito.detail}\n"
        "Usa /lega per riprovare.",
    )


# Soglia di rotazione degli archivi JSONL. Non li legge nessun programma --
# solo chi ci fa grep -- quindi rotazione per dimensione e basta: niente date,
# niente indici. Tre generazioni: al peggio l'archivio occupa quattro volte la
# soglia, e resta leggibile in ordine con `cat x.jsonl.3 ... x.jsonl`.
ARCHIVE_MAX_BYTES = int(os.environ.get("ARCHIVE_MAX_BYTES", str(8 * 1024 * 1024)))
ARCHIVE_GENERATIONS = 3


def _ruota_archivio(path: str) -> None:
    """Ruota ``path`` se ha superato la soglia. Best-effort: non solleva mai.

    Entrambi gli archivi crescevano senza limite su un minipc che non si spegne
    mai. Un file che cresce all'infinito non da' fastidio finche' non riempie il
    disco, e allora fa cadere il bot per un motivo che non c'entra col bot.
    """
    try:
        if ARCHIVE_MAX_BYTES <= 0 or os.path.getsize(path) < ARCHIVE_MAX_BYTES:
            return
    except OSError:
        return  # non esiste ancora, o non e' leggibile: niente da ruotare
    try:
        ultimo = f"{path}.{ARCHIVE_GENERATIONS}"
        if os.path.exists(ultimo):
            os.remove(ultimo)
        for n in range(ARCHIVE_GENERATIONS - 1, 0, -1):
            vecchio = f"{path}.{n}"
            if os.path.exists(vecchio):
                os.replace(vecchio, f"{path}.{n + 1}")
        os.replace(path, f"{path}.1")
    except OSError:
        logger.exception("rotazione fallita per %s", path)



def _log_turn(
    *,
    chat_id: int,
    topic_id: int,
    project_key: str,
    agent_name: str,
    prompt: str,
    reply: str,
    resume_id: str | None,
    session_id: str | None,
    elapsed_ms: int,
    ok: bool,
) -> None:
    """Append one turn to CONV_LOG_PATH (best-effort, never raises).

    Each line is a self-contained JSON object with the FULL prompt and reply, so
    a topic's history can be reconstructed by filtering on (chat, topic). Kept
    separate from Claude's own transcripts (which are keyed by container cwd and
    not reliably persisted)."""
    d = os.path.dirname(CONV_LOG_PATH)
    if d and not os.path.isdir(d):
        return  # no writable data dir (e.g. under tests) -> skip
    try:
        rec = {
            "ts": _local_now().isoformat(timespec="seconds"),
            "chat": chat_id,
            "topic": topic_id,
            "project": project_key,
            "agent": agent_name,
            "ok": ok,
            "ms": elapsed_ms,
            "resume": resume_id,
            "session": session_id,
            "in": prompt,
            "out": reply,
        }
        answering = _TURN_MSG.get()
        if answering is not None and answering[0] == chat_id:
            rec["msg_id"] = answering[1]
        _ruota_archivio(CONV_LOG_PATH)
        with open(CONV_LOG_PATH, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        logger.exception("could not append turn to %s", CONV_LOG_PATH)

# Messaggi gia' archiviati, per (chat, message_id, edit_date) (#64). Quando due
# bot dello stesso processo sono membri dello stesso gruppo (il default e il bot
# del nodo) ricevono entrambi l'update e lo archiviavano due volte nello stesso
# file. Le Application condividono il modulo, quindi basta un insieme qui. La
# data di modifica sta nella chiave: una modifica e' una voce nuova, legittima.
_ARCHIVED: "collections.OrderedDict[tuple, None]" = collections.OrderedDict()
_ARCHIVED_MAX = 4096


def _first_archive_of(msg) -> bool:
    """True la prima volta che questo messaggio (o questa sua modifica) arriva."""
    try:
        edit = getattr(msg, "edit_date", None)
        key = (msg.chat.id, msg.message_id, edit.isoformat() if edit else None)
    except Exception:
        return True  # nel dubbio si archivia: meglio doppio che perso
    if key in _ARCHIVED:
        return False
    _ARCHIVED[key] = None
    while len(_ARCHIVED) > _ARCHIVED_MAX:
        _ARCHIVED.popitem(last=False)
    return True


# (archive tipo, attribute of telegram.Message) in the order a message is
# classified: a voice note with a caption is still a voice note.
_ARCHIVE_MEDIA = (
    ("vocale", "voice"),
    ("audio", "audio"),
    ("videonota", "video_note"),
    ("video", "video"),
    ("foto", "photo"),
    ("documento", "document"),
)


def _archive_kind(msg) -> tuple[str, dict | None]:
    """(tipo, media summary) of an incoming message, for messages.jsonl.

    The summary keeps the ``file_id`` -- the only handle that lets a replay
    download the file again, from the same bot -- plus what helps a human read
    the archive. Never raises: an odd message is archived as best it can be.
    """
    text = getattr(msg, "text", None)
    if text and text.startswith("/"):
        return "comando", None
    for tipo, attr in _ARCHIVE_MEDIA:
        obj = getattr(msg, attr, None)
        if not obj:
            continue
        if isinstance(obj, (list, tuple)):  # photo: sizes, largest last
            obj = obj[-1]
        media = {"tipo": tipo}
        for key, field in (
            ("file_id", "file_id"),
            ("file_unique_id", "file_unique_id"),
            ("mime", "mime_type"),
            ("durata", "duration"),
            ("nome", "file_name"),
            ("byte", "file_size"),
        ):
            val = getattr(obj, field, None)
            if isinstance(val, dt.timedelta):
                val = int(val.total_seconds())
            if isinstance(val, (str, int)) and not isinstance(val, bool):
                media[key] = val
        return tipo, media
    if text:
        return "testo", None
    return "servizio", None  # ingressi, uscite, pin, migrazioni


def _message_raw(msg) -> dict | None:
    """The message as Telegram sent it (JSON-safe), or None if it can't be had.

    ``to_dict`` is the inverse of the ``de_json`` the replay uses to rebuild
    an Update: what goes back through the handler is the message itself, not a
    lossy copy of it.
    """
    to_dict = getattr(msg, "to_dict", None)
    if not callable(to_dict):
        return None
    try:
        raw = to_dict()
        json.dumps(raw)
        return raw
    except Exception:
        logger.debug("archive: message not serialisable", exc_info=True)
        return None


def _mark_handled(msg, how: str) -> None:
    """Record that ``msg`` got its answer from a handler, with no agent turn.

    Best-effort and never raises, like the other archives. During a replay it
    also reports the outcome to the replay loop.
    """
    holder = _REPLAY.get()
    if holder is not None and holder.get("outcome") is None:
        holder["outcome"] = "ok"
    d = os.path.dirname(HANDLED_LOG_PATH)
    if d and not os.path.isdir(d):
        return
    try:
        rec = {
            "ts": _local_now().isoformat(timespec="seconds"),
            "chat": msg.chat.id,
            "msg_id": msg.message_id,
            "come": how,
        }
        _ruota_archivio(HANDLED_LOG_PATH)
        with open(HANDLED_LOG_PATH, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        logger.exception("could not append to %s", HANDLED_LOG_PATH)


async def archive_message(update, context) -> None:
    """Archivia OGNI messaggio che il bot vede, prima di ogni altro handler.

    ``conversations.jsonl`` registra i *turni degli agenti*: un messaggio che
    non sveglia nessun agente -- un comando, una riga scritta in un topic senza
    binding, due persone che si parlano fra loro -- non lasciava traccia da
    nessuna parte. Questo handler chiude quel buco.

    Gira in ``group=-1``, quindi prima di ogni altro handler, e non consuma
    l'update: non solleva ``ApplicationHandlerStop``, cosi' il messaggio
    prosegue verso l'handler che lo avrebbe gestito comunque. Sta *prima* e non
    dopo di proposito: se un handler a valle si ferma o va in errore, il
    messaggio e' gia' archiviato.

    Il token ha la privacy mode disattivata (vedi la nota in
    ``handle_chat_migration``), quindi Telegram consegna al bot ogni messaggio
    dei gruppi di cui e' membro: qui arriva davvero tutto.

    Best-effort come ``_append_conv``: un archivio che fa cadere il bot e'
    peggio di un archivio incompleto.
    """
    msg = getattr(update, "effective_message", None)
    if msg is None:
        return
    if _REPLAY.get() is not None:
        return  # a replayed message is already in the archive, as it came
    d = os.path.dirname(MSG_LOG_PATH)
    if d and not os.path.isdir(d):
        return  # nessuna data dir scrivibile (es. sotto test)
    if not _first_archive_of(msg):
        return
    try:
        chat = msg.chat
        autore = msg.from_user
        testo = msg.text or msg.caption or ""
        tipo, media = _archive_kind(msg)
        rec = {
            "ts": _local_now().isoformat(timespec="seconds"),
            "chat": chat.id,
            "chat_tipo": chat.type,
            "chat_nome": getattr(chat, "title", None),
            "topic": msg.message_thread_id,
            "msg_id": msg.message_id,
            "da_id": getattr(autore, "id", None),
            "da": getattr(autore, "full_name", None),
            "username": getattr(autore, "username", None),
            "tipo": tipo,
            "testo": testo,
            "reply_a": (msg.reply_to_message.message_id
                        if msg.reply_to_message else None),
            "modificato": getattr(update, "edited_message", None) is not None,
        }
        # What a replay needs to put the message back through its handler:
        # which of our bots got it (it is the one that can download the file)
        # and the message itself, file_id included. Commands and service lines
        # are never replayed, so they stay as small as before.
        if tipo not in ("comando", "servizio"):
            rec["bot"] = _current_bot_name(context)
            if media:
                rec["media"] = media
            mgid = getattr(msg, "media_group_id", None)
            if isinstance(mgid, str) and mgid:
                rec["media_group_id"] = mgid
            raw = _message_raw(msg)
            if raw is not None:
                rec["raw"] = raw
        _ruota_archivio(MSG_LOG_PATH)
        with open(MSG_LOG_PATH, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        logger.exception("could not archive message to %s", MSG_LOG_PATH)


def _append_rating(rec: dict) -> None:
    """Best-effort append of one vote to RATING_LOG_PATH, mirroring
    archive_message's append style. Never raises: a lost vote must not
    break the callback ack."""
    d = os.path.dirname(RATING_LOG_PATH)
    if d and not os.path.isdir(d):
        return
    try:
        with open(RATING_LOG_PATH, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        logger.exception("could not append rating to %s", RATING_LOG_PATH)


def _rating_keyboard() -> InlineKeyboardMarkup:
    """5 pulsanti di voto (1-5) per una risposta ComUnity. L'identita' del
    messaggio votato arriva gratis da query.message quando l'utente tocca un
    pulsante: il callback_data porta solo il voto."""
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(str(n), callback_data=pack_cb("rt", str(n))) for n in range(1, 6)]]
    )


async def on_rating_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Chiunque nel gruppo puo' votare (nessun controllo su chi ha scritto la
    richiesta originale): il voto e' su quanto la risposta e' andata bene,
    non un permesso."""
    q = update.callback_query
    if not q or not q.data or not q.message:
        return
    try:
        _, rating_s = parse_cb(q.data)
        rating = int(rating_s)
    except (ValueError, IndexError):
        await q.answer()
        return
    msg = q.message
    voter = q.from_user
    rec = {
        "ts": _local_now().isoformat(timespec="seconds"),
        "chat_id": msg.chat_id,
        "topic_id": _topic_id(msg),
        "project": "comunity",
        "message_id": msg.message_id,
        "voter_id": getattr(voter, "id", None),
        "voter_name": getattr(voter, "full_name", None),
        "rating": rating,
    }
    _append_rating(rec)
    await q.answer(f"Voto registrato: {rating}/5")
    try:
        await q.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass


# --- Pure helpers (unit-tested; keep names stable) ---------------------------
def _parse_claude_json(raw: str) -> tuple[str, str | None]:
    """Parse ``claude -p --output-format json`` stdout into (text, session_id).

    Real shape: ``{"result": "<reply text>", "session_id": "<uuid>", ...}`` —
    ``result`` is a plain string. Falls back to raw text if not JSON.
    """
    try:
        data = json.loads(raw)
        text = (data.get("result") or "").strip() or "(risposta vuota)"
        return text, data.get("session_id")
    except (json.JSONDecodeError, AttributeError):
        return (raw.strip() or "(risposta vuota)"), None


# Stream-json helpers (_parse_stream_line / _event_progress / _result_from_event)
# moved to claude_runner (imported above) so the spawner is self-contained.


def chunk_text(text: str, size: int = TELEGRAM_CHUNK):
    """Yield Telegram-safe chunks, splitting on paragraph/line where possible."""
    if len(text) <= size:
        yield text
        return
    while text:
        if len(text) <= size:
            yield text
            return
        cut = text.rfind("\n\n", 0, size)
        if cut < size // 2:
            cut = text.rfind("\n", 0, size)
        if cut < size // 2:
            cut = size
        yield text[:cut].rstrip()
        text = text[cut:].lstrip()


def _local_now() -> dt.datetime:
    try:
        from zoneinfo import ZoneInfo

        return dt.datetime.now(ZoneInfo(TZ_NAME))
    except Exception:
        return dt.datetime.now(dt.UTC)


def _get_session(chat_id: int, topic_id, *, scope: str | None = None) -> str | None:
    """Return the live session id for (chat, topic), evicting if expired.

    ``scope="day"`` (manifest ``[telegram] session_scope``) also expires a
    session last used on an earlier local day (TZ_NAME).
    """
    key = (chat_id, topic_id)
    item = SESSIONS.get(key)
    if not item:
        return None
    sid, last = item
    if chat_session.session_expired(
        last, dt.datetime.now(), ttl_sec=SESSION_TTL_SEC, scope=scope, tz_name=TZ_NAME
    ):
        SESSIONS.pop(key, None)
        _save_state()
        return None
    return sid


def _set_session(chat_id: int, topic_id, sid: str) -> None:
    SESSIONS[(chat_id, topic_id)] = (sid, dt.datetime.now())
    _save_state()


# (chat_id, topic_id) -> how many times the session was cleared on purpose.
# A turn already running when /new, /reset or a menu tap clears the session
# reads it before and after: if it moved, the turn does not store its session
# id, so the reset cannot be undone by a reply that arrives afterwards.
_RESET_GEN: dict[tuple[int, int], int] = {}


def _clear_session(chat_id: int, topic_id) -> bool:
    _RESET_GEN[(chat_id, topic_id)] = _RESET_GEN.get((chat_id, topic_id), 0) + 1
    existed = SESSIONS.pop((chat_id, topic_id), None) is not None
    if existed:
        _save_state()
    return existed


def _get_prefs(chat_id: int, topic_id) -> model_prefs.Prefs:
    """Sticky (model, effort) for a card, set with /modello. Empty = env default."""
    return TOPIC_PREFS.get((chat_id, topic_id), model_prefs.Prefs())


def _project_default_prefs(project: Project | None) -> model_prefs.Prefs:
    """Project-level model default from ``[telegram] model/effort`` (#65).

    ``model`` is either a /modello catalog id (``deepseek-pro``) or a plain
    model token (``opus``). An id whose key is missing on this deployment is
    dropped with a warning instead of producing a turn that cannot run.
    """
    if project is None:
        return model_prefs.Prefs()
    try:
        tg = manifest.telegram_of(project)
    except Exception:
        logger.debug("manifest unreadable for %s", project.key, exc_info=True)
        return model_prefs.Prefs()
    if tg is None or not (tg.model or tg.effort):
        return model_prefs.Prefs()
    model = choice_id = None
    if tg.model:
        choice = CATALOG_BY_ID.get(tg.model.strip().lower())
        if choice is not None:
            if _choice_available(project.key, choice):
                choice_id = choice.id
            else:
                logger.warning(
                    "%s: telegram.model=%r needs %s, not set here: using the env default",
                    project.key, tg.model, choice.key_env,
                )
        else:
            model = model_prefs.normalize_model(tg.model)
            if model is None:
                logger.warning("%s: telegram.model=%r not recognised", project.key, tg.model)
    effort = model_prefs.normalize_effort(tg.effort)
    return model_prefs.Prefs(model, effort, choice_id)


def _set_prefs(chat_id: int, topic_id, prefs: model_prefs.Prefs) -> None:
    key = (chat_id, topic_id)
    if prefs:
        TOPIC_PREFS[key] = prefs
    else:  # /modello auto -> back to the env default
        TOPIC_PREFS.pop(key, None)
    _save_state()


def _h(text) -> str:
    """Escape HTML for parse_mode=HTML replies."""
    return html_mod.escape(str(text), quote=False)


_HTML_STRIP_RE = re.compile(r"<[^>]+>")


def _strip_html(text: str) -> str:
    return html_mod.unescape(_HTML_STRIP_RE.sub("", text))


# --- Markdown -> Telegram-HTML -----------------------------------------------
# Claude agents reply in Markdown, but every reply is sent with parse_mode=HTML,
# so ``**bold**`` / ``` `code` ``` would show their literal markers. Convert the
# small Markdown subset agents actually use into the HTML tags Telegram supports
# (b/i/s/code/pre/a), HTML-escaping <>& everywhere else (which also fixes stray
# ``x < y`` breaking the send). Conservative on purpose: code spans are stashed
# first so their content is never re-parsed, ``_`` emphasis requires non-word
# flanking so file_name paths survive, and unknown constructs pass through as
# escaped text. The senders already fall back to _strip_html on any HTML error.
_CODE_FENCE_RE = re.compile(r"```[^\n]*\n?(.*?)```", re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")
_MD_WIKILINK_RE = re.compile(r"\[\[([^\]]+)\]\]")
_MD_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)")
_MD_HEAD_RE = re.compile(r"(?m)^[ \t]{0,3}#{1,6}[ \t]+(.+?)[ \t]*#*$")
_MD_BULLET_RE = re.compile(r"(?m)^([ \t]*)[-*][ \t]+")
_MD_BOLD_RE = re.compile(r"\*\*(.+?)\*\*", re.DOTALL)
_MD_STRIKE_RE = re.compile(r"~~(.+?)~~", re.DOTALL)
_MD_ITALIC_STAR_RE = re.compile(r"(?<![\*\w])\*(?!\s)([^*\n]+?)(?<!\s)\*(?![\*\w])")
_MD_ITALIC_US_RE = re.compile(r"(?<!\w)_(?!\s)([^_\n]+?)(?<!\s)_(?!\w)")


def _md_to_html(text: str) -> str:
    """Render the Markdown subset agents emit as Telegram-safe HTML."""
    if not text:
        return text
    codes: list[str] = []

    def _stash(frag: str) -> str:
        codes.append(frag)
        return f"\x00C{len(codes) - 1}\x00"

    s = _CODE_FENCE_RE.sub(
        lambda m: _stash("<pre>" + _h(m.group(1).rstrip("\n")) + "</pre>"), text
    )
    s = _INLINE_CODE_RE.sub(lambda m: _stash("<code>" + _h(m.group(1)) + "</code>"), s)
    s = _h(s)
    s = _MD_WIKILINK_RE.sub(r"\1", s)
    s = _MD_LINK_RE.sub(lambda m: f'<a href="{m.group(2)}">{m.group(1)}</a>', s)
    s = _MD_HEAD_RE.sub(r"<b>\1</b>", s)
    s = _MD_BULLET_RE.sub(r"\1• ", s)
    s = _MD_BOLD_RE.sub(r"<b>\1</b>", s)
    s = _MD_STRIKE_RE.sub(r"<s>\1</s>", s)
    s = _MD_ITALIC_STAR_RE.sub(r"<i>\1</i>", s)
    s = _MD_ITALIC_US_RE.sub(r"<i>\1</i>", s)
    for i, frag in enumerate(codes):
        s = s.replace(f"\x00C{i}\x00", frag)
    return s


async def run_claude_html(*args, **kwargs):
    """``run_claude`` whose Markdown reply is rendered as Telegram HTML, for the
    consumers that send the cold-spawn text straight to a chat: scheduled *agent*
    notifications and hub wakes. Interactive turns convert inside _claude_turn, so
    the raw runner stays Telegram-agnostic. Provider/static notifications never
    reach this wrapper (they don't call run_claude), so their HTML stays intact."""
    result = await run_claude(*args, **kwargs)
    reply, session_id = result
    # Rebuild rather than return a 2-tuple: the measurement must survive the
    # HTML conversion, or every consumer that goes through this wrapper
    # (scheduled agent notifications, hub wakes) drops off the ledger.
    return claude_runner.RunResult(
        _md_to_html(reply), session_id, llm_usage.usage_of(result)
    )


# --- Callback-data codec -----------------------------------------------------
# pack_cb/parse_cb now live in cb_pack.py (tiny leaf module, no bot.py/telegram
# imports) so task_card.py can build "tk:*" callback_data without importing
# bot.py back (bot.py imports task_card -> would be circular otherwise).
# Imported at the top of this file; re-used under the historical names so
# every existing call site is unchanged.


# --- Confirm-gate (generic: agent proposes -> buttons -> press re-invokes) ----
CONFIRM_SENTINEL = "⟦CONFIRM⟧"


def _strip_confirm_sentinel(reply: str) -> tuple[str, bool]:
    """If the reply's final non-empty line is the confirm sentinel, strip it and
    return (text_without_sentinel, True). Otherwise return (reply, False)."""
    lines = reply.rstrip().splitlines()
    if lines and lines[-1].strip() == CONFIRM_SENTINEL:
        return "\n".join(lines[:-1]).rstrip(), True
    return reply, False


# --- Production marker: a fix that is live gets its own reaction --------------
# An agent that has deployed a fix AND checked it live ends its reply with this
# line. The bot strips it and marks the message that asked for the fix with
# REACT_PROD, so the group sees at a glance which requests are in production
# (ComUnity, 2026-10-07). The deploy often happens a turn later, after the ✅
# Conferma press, so the request message is remembered per (chat, topic).
PROD_SENTINEL = "⟦PROD⟧"
_LAST_REQUEST_MSG: dict[tuple[int, int], int] = {}


def _strip_prod_sentinel(reply: str) -> tuple[str, bool]:
    """Remove every line that is exactly the production sentinel.

    Returns (text_without_it, True) if it was there. Line-based, not
    end-anchored, so it coexists with the confirm sentinel in either order.
    """
    lines = reply.rstrip().splitlines()
    kept = [line for line in lines if line.strip() != PROD_SENTINEL]
    if len(kept) == len(lines):
        return reply, False
    return "\n".join(kept).rstrip(), True


async def _react_request_in_prod(bot, chat_id: int, topic_id: int) -> None:
    """Best-effort REACT_PROD on the message that started this (chat, topic) work."""
    message_id = _LAST_REQUEST_MSG.get((chat_id, topic_id))
    if message_id is None:
        logger.info("prod reaction skipped: no request message for (%s,%s)", chat_id, topic_id)
        return
    try:
        await bot.set_message_reaction(chat_id, message_id, [REACT_PROD])
    except Exception as exc:
        logger.warning("prod reaction failed: %r", exc)


def _kb_confirm(chat_id: int, topic_id: int) -> InlineKeyboardMarkup:
    """Confirm/cancel keyboard whose callback_data carries the (chat,topic) that
    identifies the resumable session to re-invoke on press."""
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "✅ Conferma",
                    callback_data=pack_cb("cf", "ok", str(chat_id), str(topic_id)),
                ),
                InlineKeyboardButton(
                    "❌ Annulla",
                    callback_data=pack_cb("cf", "no", str(chat_id), str(topic_id)),
                ),
            ]
        ]
    )


async def _send_confirm_buttons(bot, chat_id: int, topic_id: int, thread) -> None:
    """Post the ✅/❌ keyboard for a pending agent proposal into (chat,thread).

    ``topic_id`` keys the callback_data (session identity); ``thread`` is the
    Telegram message_thread_id (None for General/private)."""
    await bot.send_message(
        chat_id,
        "👉 Confermi?",
        reply_markup=_kb_confirm(chat_id, topic_id),
        message_thread_id=thread,
    )


# --- Topic helpers -----------------------------------------------------------
def _topic_id(msg) -> int:
    """The topic a message belongs to. General topic / private chat -> 0.

    Only a real forum-topic message counts: in a non-forum group Telegram puts
    the root of a reply chain in ``message_thread_id`` too, and reading it as a
    topic gave every reply chain its own session. Objects without
    ``is_topic_message`` (test doubles) keep the plain thread id.
    """
    thread = getattr(msg, "message_thread_id", None) or 0
    if hasattr(msg, "is_topic_message") and not getattr(msg, "is_topic_message", None):
        return 0
    return thread


def _group_guest_profile(update: Update) -> Profile | None:
    """Profilo implicito per chi scrive in un gruppo aperto ai suoi membri.

    Restituisce ``None`` (quindi default deny invariato) a meno che TUTTE
    queste condizioni valgano: il messaggio arriva da un gruppo, quel
    (chat, topic) e' associato a un progetto, e quel progetto e' elencato in
    OPEN_GROUP_PROJECTS.

    Il profilo che ne esce non e' admin e ha come unico progetto visibile
    quello del gruppo: chi entra da qui puo' parlare con l'agente del suo
    settore, non amministrare il Coordinator ne' vedere gli altri nodi.
    """
    chat = update.effective_chat
    msg = update.effective_message
    if chat is None or msg is None:
        return None
    if chat.type not in ("group", "supergroup"):
        return None
    # With Life's operator configured, membership of a group is no longer a grant:
    # everyone who is not an owner passes through the operator (#83, 16/10).
    if life_access.configured():
        return None
    binding = TOPIC_BINDING.get((chat.id, _topic_id(msg)))
    if binding is None:
        return None
    project_key = binding[0]
    if project_key not in OPEN_GROUP_PROJECTS:
        return None
    return Profile(
        key="ospite",
        label="Ospite del gruppo",
        is_admin=False,
        projects=(project_key,),
        task_card_target="project_group",
    )


def _bind_topic(chat_id: int, topic_id: int, project_key: str, agent_name: str) -> None:
    """Bind a topic to a (project, agent) pair (persisted)."""
    if TOPIC_BINDING.get((chat_id, topic_id)) != (project_key, agent_name):
        TOPIC_BINDING[(chat_id, topic_id)] = (project_key, agent_name)
        _save_state()


async def _ensure_project_topic(
    bot, project: Project, agent: Agent
) -> tuple[int, int] | None:
    """Return the (hub_chat_id, thread_id) of the specialist's forum topic in the
    shared Coordinator hub, creating it on first use.

    Reverse-looks up an existing binding of ``(hub, tid) -> (project, agent)``;
    if none exists, creates a forum topic named after the project and binds it.
    Returns ``None`` on any failure (no hub configured, API error) so the caller
    can fall back to the owner's DM.
    """
    hub = COORDINATOR_FORUM_CHAT_ID
    if not hub:
        return None
    try:
        for (chat_id, tid), (pk, an) in TOPIC_BINDING.items():
            if chat_id == hub and pk == project.key and an == agent.name:
                return hub, tid
        name = project.label or project.key
        topic = await bot.create_forum_topic(chat_id=hub, name=name)
        _bind_topic(hub, topic.message_thread_id, project.key, agent.name)
        return hub, topic.message_thread_id
    except Exception:
        logger.exception(
            "task.due: failed to ensure hub topic for project %s", project.key
        )
        return None


def _topic_binding(chat_id: int, topic_id: int) -> tuple[Project | None, Agent | None]:
    """The (project, agent) a topic is bound to, or (None, None) if unbound.

    If the stored agent name can't be discovered (e.g. the workspace isn't
    mounted on this host, so ``.claude/agents/`` is unreadable), fall back to a
    bare ``Agent`` carrying the recorded name — never silently change *which*
    agent the topic is bound to. ``generale`` always resolves to GENERALE.
    """
    v = TOPIC_BINDING.get((chat_id, topic_id))
    if v is None:
        return None, None
    proj = resolve_project(v[0])
    if proj is None:
        return None, None
    agent = resolve_agent(proj, v[1])
    if agent is None:
        agent = Agent(name=v[1], description=v[1], persona=None)
    return proj, agent


# --- Claude invocation -------------------------------------------------------
# The cold-spawn path (_claude_gate / run_claude) and its stream helpers now live
# in claude_runner (imported at the top) so headless workers can reuse the spawner
# without pulling in python-telegram-bot. bot.py keeps only the Telegram wiring.


# Lazily-created singleton for the persistent-session path (Step 4). Built inside
# the running loop so the per-key asyncio primitives bind to the right loop, and
# wired with bot.py's own stream helpers/constants + the shared spawn gate.
_live_mgr: LiveSessionManager | None = None


def _live_usage_from_event(ev: dict):
    """Cost of one warm turn (issue #28), read off its ``result`` event.

    A live session spans many turns, so there is no per-turn start time to hand
    the parser: it is reconstructed by walking back from now over the event's
    own ``duration_ms``. Without this the whole interactive half of the bot
    would be missing from the ledger on every deployment where
    ``CLAUDE_LIVE_SESSIONS`` is on — which is the default one.
    """
    usage = claude_runner.usage_from_result_event(
        ev, model=CLAUDE_MODEL or "<default>", started_at=dt.datetime.now(dt.UTC)
    )
    if usage.duration_ms:
        usage.started_at -= dt.timedelta(milliseconds=usage.duration_ms)
    return usage


def _live_manager() -> LiveSessionManager:
    """The process-wide LiveSessionManager for CLAUDE_LIVE_SESSIONS turns.

    Claude-only by construction (``claude_bin``, ``--permission-mode
    bypassPermissions``): there is no opencode implementation of the warm
    stream-json protocol. That asymmetry is handled where the flag is computed —
    ``CLAUDE_LIVE_SESSIONS`` is False whenever ``AI_BACKEND != "claude"`` — so
    this manager is only ever built on a claude deployment and nothing can
    silently run claude for interactive turns and opencode everywhere else.
    """
    global _live_mgr
    if _live_mgr is None:
        _live_mgr = LiveSessionManager(
            claude_bin=CLAUDE_BIN,
            model=CLAUDE_MODEL or None,
            setting_sources=CLAUDE_SETTING_SOURCES or None,
            strict_mcp=CLAUDE_STRICT_MCP,
            mcp_config=SUITE_MCP_CONFIG,
            idle_ttl_sec=SESSION_TTL_SEC,
            max_timeout_sec=CLAUDE_MAX_TIMEOUT,
            idle_event_timeout_sec=CLAUDE_IDLE_TIMEOUT,
            stream_limit=CLAUDE_STREAM_LIMIT,
            max_live=CLAUDE_MAX_LIVE_SESSIONS,
            parse_stream_line=_parse_stream_line,
            result_from_event=_result_from_event,
            event_progress=_event_progress,
            usage_from_event=_live_usage_from_event,
            result_factory=claude_runner.RunResult,
            gate=_claude_gate(),
            logger=logger,
        )
    return _live_mgr


# Background sweep retiring warm live sessions idle past SESSION_TTL_SEC. Started
# from on_startup only when CLAUDE_LIVE_SESSIONS is on, cancelled on shutdown. The
# cold path expires sessions lazily on access, but a warm process holds RAM until
# actively retired — hence this loop (belt-and-suspenders with the LRU cap).
_evict_task: asyncio.Task | None = None


async def _live_evict_loop() -> None:
    interval = max(60, SESSION_TTL_SEC // 2)
    while True:
        try:
            await asyncio.sleep(interval)
            n = await _live_manager().evict_idle()
            if n:
                logger.info("live: evicted %d idle session(s)", n)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("live evict loop error")


_projects_refresh_task: asyncio.Task | None = None


def _sono_il_volto() -> bool:
    """True if this device is the Telegram face (or ownership is off).

    Both devices run the refresh loop; only the one polling speaks in groups,
    or a pending group would get the same menu twice.
    """
    manager = _OWNERSHIP.get("manager")
    return manager is None or bool(manager.is_polling)


async def _riproponi_gruppi_in_attesa(bot) -> None:
    """Rimanda il menu ai gruppi in sospeso che ora hanno un progetto nuovo (#53).

    Best-effort per gruppo: un gruppo che non esiste piu' (o che ha tolto il
    bot senza che l'evento arrivasse) viene dimenticato, non ritentato.
    """
    legabili = [key for key, _ in _progetti_legabili()]
    legati = {c for (c, t) in TOPIC_BINDING if t == 0}
    for chat_id in group_register.da_riproporre(_GRUPPI_IN_ATTESA, legabili, legati):
        try:
            chat = await bot.get_chat(chat_id)
            await bot.send_message(
                chat_id,
                f"Nel registro ci sono progetti nuovi. Gruppo *{chat.title}*: "
                "a quale progetto lo lego?",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=_menu_gruppo(0),
            )
            _GRUPPI_IN_ATTESA[chat_id] = legabili
            logger.info("menu di legatura riproposto al gruppo %s", chat_id)
        except Exception as exc:
            logger.warning("gruppo %s non raggiungibile, lo dimentico: %s", chat_id, exc)
            _GRUPPI_IN_ATTESA.pop(chat_id, None)
        _save_state()


async def _projects_refresh_loop(bot=None) -> None:
    """Re-fetch Coordinator's project registry from Life every N minutes.

    Mirrors ``_live_evict_loop``'s shape: sleep, do the work, keep looping
    across any non-cancellation error so one bad fetch never kills the loop.
    """
    while True:
        try:
            await asyncio.sleep(PROJECTS_REFRESH_INTERVAL_SEC)
            diff = projects.refresh_projects()
            if diff["added"] or diff["removed"]:
                logger.info(
                    "projects: refreshed from Life (added=%s removed=%s)",
                    diff["added"],
                    diff["removed"],
                )
            # Not only on diff["added"]: with a static registry (projects.local.json)
            # a new project arrives with a restart, and the refresh never sees it.
            # The offered-keys dedup makes a check per cycle cost nothing.
            if bot is not None and _GRUPPI_IN_ATTESA and _sono_il_volto():
                await _riproponi_gruppi_in_attesa(bot)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("projects refresh loop error")


# --- Telegram helpers --------------------------------------------------------
# Uniform refusal when a user reaches a project outside their profile (M3).
NO_ACCESS_MSG = "🔒 Non hai accesso a questo progetto."


# Chats declared by more than one instance (#83, F0): routed nowhere until the
# instances are separated. Filled at startup by _check_contested_chats().
_CHAT_INDEX = instance_acl.ChatIndex()
_CONTESTED_WARNED: set[int] = set()


ANONYMOUS_ADMIN_ID = 1087968824  # GroupAnonymousBot: an admin who posts with "Remain anonymous"


def _anonymous_admin_profile(update: Update) -> Profile | None:
    """Participant profile for an anonymous admin of a lesson group, else ``None``.

    Telegram hides who an anonymous admin is, so no id can be checked: only the group's own
    admins can post like this. It gets what an ordinary member gets (the group's project,
    never admin), and only in a chat whose manifest declares ``audio_command``.
    """
    user, chat = update.effective_user, update.effective_chat
    if user is None or chat is None or user.id != ANONYMOUS_ADMIN_ID:
        return None
    binding = manifest.audio_binding(chat.id)
    if binding is None:
        return None
    return Profile(key="anonimo", label="Amministratore anonimo", is_admin=False,
                   projects=(binding[0],), task_card_target="dm", instances=())


def _profile(update: Update) -> Profile | None:
    """The profile of the message's user, or ``None`` if unmapped (default deny).

    A contested chat denies everyone, admins included: which instance's workspace
    and bus the message would reach is exactly what is unknown there.
    """
    chat = update.effective_chat
    if chat is not None and _CHAT_INDEX.is_contested(chat.id):
        if chat.id not in _CONTESTED_WARNED:
            _CONTESTED_WARNED.add(chat.id)
            logger.error("contested chat %s (%s): message dropped", chat.id,
                         ", ".join(_CHAT_INDEX.contested[chat.id]))
        return None
    if (anon := _anonymous_admin_profile(update)) is not None:
        return anon
    user = update.effective_user
    profile = profile_for(user.id) if user is not None else None
    if profile is None and user is not None and chat is not None:
        profile = _life_profile(update)
    if profile is None or profile.is_admin or chat is None:
        return profile
    msg = update.effective_message
    iid = _binding_instance(chat.id, _topic_id(msg) if msg is not None else 0)
    if not instance_acl.authorize(profile, iid):
        logger.warning("user %s (%s) denied on chat %s: instance %s not in profile",
                       user.id, profile.key, chat.id, iid)
        return None
    return profile


_NODE_INDEX: dict[str, tuple[str, ...]] | None = None


def _node_index() -> dict[str, tuple[str, ...]]:
    """``graph id -> instances`` of the nodes hosted here (Life's calculation, cached)."""
    global _NODE_INDEX
    if _NODE_INDEX is None:
        import bus_agents

        got = bus_agents.derived()
        _NODE_INDEX = instance_routing.node_index(got[0] if got else ())
    return _NODE_INDEX


_ROUTING_INDEX: dict[str, tuple[str, ...]] | None = None


def _routing_index() -> dict[str, tuple[str, ...]]:
    """Local nodes (as hosted here) plus the nodes that run elsewhere (#83, F3).

    A remote node listed by several instance graphs takes the device's default
    instance — the same rule Life applies to bus names (``identity.local_agents``).
    """
    global _ROUTING_INDEX
    if _ROUTING_INDEX is None:
        import bus_agents
        import instances

        merged = dict(_node_index())
        default = instances.default_instance_id()
        owners: dict[str, list[str]] = {}
        for iid, gids in bus_agents.graph_nodes().items():
            for gid in gids:
                owners.setdefault(gid.lower(), []).append(iid)
        for gid, iids in owners.items():
            if gid in merged:
                continue
            merged[gid] = (default,) if default in iids else tuple(sorted(iids))
        _ROUTING_INDEX = merged
    return _ROUTING_INDEX


def _binding_instance(chat_id: int, topic_id: int) -> str | None:
    """The instance of what ``(chat, topic)`` is bound to (#83, F1); None = undeclared."""
    binding = TOPIC_BINDING.get((chat_id, topic_id)) or TOPIC_BINDING.get((chat_id, 0))
    node = None
    if binding is not None:
        project = resolve_project(binding[0])
        node = instance_routing.project_node(project) if project else binding[0]
    return instance_routing.instance_of_binding(
        chat_id,
        node,
        explicit=TOPIC_INSTANCE.get((chat_id, topic_id)) or TOPIC_INSTANCE.get((chat_id, 0)),
        chat_owner=_CHAT_INDEX.instance_of(chat_id),
        nodes=_routing_index(),
    )


def _log_instance_bindings() -> None:
    """One line at startup: which instance every existing binding resolves to (#83, F1).

    An unresolved binding is named, not counted: it is the one message nobody will
    be able to route once the per-instance gate bites.
    """
    from collections import Counter

    per: Counter = Counter()
    unresolved: list[str] = []
    for (chat_id, topic_id), (pk, _an) in sorted(TOPIC_BINDING.items()):
        iid = _binding_instance(chat_id, topic_id)
        if iid is None:
            unresolved.append(f"{chat_id}:{topic_id}={pk}")
        else:
            per[iid] += 1
    detail = ", ".join(f"{k}={v}" for k, v in sorted(per.items())) or "-"
    logger.info("instance bindings: %d resolved (%s), %d unresolved%s",
                sum(per.values()), detail, len(unresolved),
                (": " + ", ".join(unresolved)) if unresolved else "")


async def _check_contested_chats(bot) -> None:
    """Index the instances' chats; tell the admins once about any contested one."""
    global _CHAT_INDEX
    _CHAT_INDEX = await asyncio.to_thread(instance_acl.index_from_registry)
    lines = instance_acl.describe_contested(_CHAT_INDEX)
    logger.info("instance chats: %d owned, %d contested",
                len(_CHAT_INDEX.owner), len(_CHAT_INDEX.contested))
    for line in lines:
        logger.error("instance chats: %s", line)
    if not lines:
        return
    text = "⚠️ Chat contesa fra istanze (Coordinator#83):\n" + "\n".join(f"• {l}" for l in lines)
    for uid in profiles.admin_user_ids():
        try:
            await bot.send_message(uid, text)
        except Exception:
            logger.exception("contested-chat notice to %s failed", uid)


def _profile_or_guest(update: Update) -> Profile | None:
    """Il profilo che vale in questo punto: quello mappato, oppure l'ospite.

    Unico posto in cui le due strade si uniscono. Chi la usa apre a chi sta in
    un gruppo aperto; chi chiama ``_profile`` resta chiuso agli id noti. La
    differenza fra i due deve restare una scelta leggibile handler per
    handler, non un default che scivola addosso a tutto.
    """
    return _profile(update) or _group_guest_profile(update)


def _allowed(update: Update) -> bool:
    return _profile(update) is not None


# --- Per-instance access through Life's operator (#83, decisions of 16/10) ----
# The owners are the profiles (ALLOWED_USER_IDS); everyone else is a person in
# Life's registry with a role per instance. The gate runs first on every update of
# every bot (group -3) and asks the operator before anything wakes: a refusal stops
# the update there, a grant is remembered here so that the synchronous _profile()
# of the handlers below sees it.
_LIFE_GRANTS: dict[tuple[int, str], life_access.Decision] = {}
_UNKNOWN_WARNED: set[int] = set()
_REFUSAL_SENT: dict[tuple[int, int], float] = {}
REFUSAL_REPEAT_SEC = 3600.0
LIFE_ACCESS_REFUSALS = {
    life_access.UNKNOWN: "🔒 Non ti conosco ancora: ho avvisato the owner, che può darti accesso.",
    life_access.DENIED: "🔒 Non hai accesso qui.",
    life_access.UNAVAILABLE: "⚠️ Non riesco a verificare il tuo accesso ora: riprova tra poco.",
    "no-instance": "🔒 Questa chat non appartiene a un'istanza che ti riguarda.",
}


def _chat_project_key(update: Update) -> str | None:
    """The project the update's chat (or topic) is bound to, whichever way it is."""
    chat, msg = update.effective_chat, update.effective_message
    tid = _topic_id(msg) if msg is not None else 0
    binding = TOPIC_BINDING.get((chat.id, tid)) or TOPIC_BINDING.get((chat.id, 0))
    if binding is not None:
        return binding[0]
    found = manifest.group_project_for_chat(chat.id)
    return found[0] if found else None


def _life_profile(update: Update) -> Profile | None:
    """The profile Life granted to a non-owner in this chat, or ``None``.

    Never wider than the chat: one instance, and as projects only the one this
    chat is bound to. A person reaches the nodes of an instance from that
    instance's rooms, never a menu of everything else.
    """
    user, chat, msg = update.effective_user, update.effective_chat, update.effective_message
    iid = _binding_instance(chat.id, _topic_id(msg) if msg is not None else 0)
    grant = _LIFE_GRANTS.get((user.id, iid)) if iid else None
    if grant is None:
        return None
    project_key = _chat_project_key(update)
    return Profile(
        key=f"life-{grant.person_id}",
        label=grant.display_name or "Ospite",
        is_admin=False,
        projects=(project_key,) if project_key else (),
        task_card_target="dm",
        instances=(iid,),
    )


async def _life_grant(update: Update, project=None, team=None) -> life_access.Decision | None:
    """The Life role of the sender for this chat, owners included as admin.

    The group -3 gate has already populated ``_LIFE_GRANTS`` for ordinary people;
    a command can be the first update after a restart, so a miss asks once. When
    the feature is off, ``role`` stays ``None``: the historical group trust still
    applies (no role, no level check).
    """
    user, chat = update.effective_user, update.effective_chat
    msg = update.effective_message
    if user is None or chat is None:
        return None
    iid = _binding_instance(chat.id, _topic_id(msg) if msg is not None else 0)
    if iid is None and team is not None and team.instance:
        iid = team.instance
    if profile_for(user.id) is not None:
        return life_access.Decision(life_access.GRANTED, iid or "", role="admin",
                                    display_name=user.full_name)
    if iid is None:
        return None
    if not life_access.configured():
        return life_access.Decision(life_access.GRANTED, iid, role=None,
                                    display_name=user.full_name)
    decision = _LIFE_GRANTS.get((user.id, iid))
    if decision is None:
        cli = life_access.client()
        if cli is None:
            return None
        decision = await cli.check(str(user.id), iid, "use")
        if decision.status != life_access.UNAVAILABLE:
            _LIFE_GRANTS[(user.id, iid)] = decision
    return decision


def _grant_allows(grant: life_access.Decision | None, level: str) -> bool:
    if grant is None or not grant.ok:
        return False
    if grant.role is None:  # feature off: no role to read, legacy trust
        return True
    return life_access.role_allows(grant.role, level)


def _team_instance(team, update: Update) -> str | None:
    if team is not None and team.instance:
        return team.instance
    chat, msg = update.effective_chat, update.effective_message
    if chat is None or msg is None:
        return None
    return _binding_instance(chat.id, _topic_id(msg))


def _topic_agent(update: Update, project) -> str | None:
    chat, msg = update.effective_chat, update.effective_message
    if chat is None or msg is None:
        return None
    binding = TOPIC_BINDING.get((chat.id, _topic_id(msg))) or TOPIC_BINDING.get((chat.id, 0))
    if binding is not None and binding[0] == project.key:
        return binding[1]
    tg = manifest.telegram_of(project)
    return tg.agent if tg is not None else None


async def _maybe_team_chiudi(update: Update, context) -> bool:
    """``/chiudi <id>`` in a team's area topic closes the ClickUp task, not the card.

    The global ``/chiudi`` (archive this card) is registered first, so the team
    behaviour has to live inside this handler: with no argument it keeps the
    historical meaning, with a task id it is the Task 14 wrapper.
    """
    msg, chat = update.effective_message, update.effective_chat
    if msg is None or chat is None:
        return False
    project, _agent = _topic_binding(chat.id, _topic_id(msg))
    if project is None:
        return False
    team = manifest.team_of(project)
    if team is None:
        return False
    args = " ".join(getattr(context, "args", None) or []).strip()
    if not args:
        args = (msg.text or "").partition(" ")[2].strip()
    if not args:
        return False
    grant = await _life_grant(update, project, team)
    if not _grant_allows(grant, "use"):
        await msg.reply_text(LIFE_ACCESS_REFUSALS[life_access.DENIED])
        return True
    iid = _team_instance(team, update)
    if not iid:
        await msg.reply_text("Istanza non risolta per questa chat.")
        return True
    await team_commands.chiudi(update, project, team, iid, grant, args)
    return True


def _access_line(update: Update) -> str | None:
    """What the agent must know about a non-owner sender: who, which role, which levels."""
    user, chat = update.effective_user, update.effective_chat
    if user is None or chat is None or profile_for(user.id) is not None:
        return None
    msg = update.effective_message
    iid = _binding_instance(chat.id, _topic_id(msg) if msg is not None else 0)
    grant = _LIFE_GRANTS.get((user.id, iid)) if iid else None
    if grant is None:
        return None
    role = life_access.canonical_role(grant.role) or grant.role
    hint = life_access.node_level_hint(grant.role)
    # The session of a non-admin is read-only (#89): a contributor's change goes
    # through /proponi (cmd_proponi), never through the agent with a secret.
    return (f"[accesso Life] {grant.display_name or 'persona'} (telegram {user.id}) è "
            f"{role} dell'istanza {iid}: {hint} Ciò che va oltre non lo fai per lui/lei.")


def _assistant_name(update: Update) -> str | None:
    """The name the sender chose for their assistant (#89): static profile env,
    or the Life access answer. ``None`` = not chosen, the bot stays «Life»."""
    user = update.effective_user
    if user is None:
        return None
    profile = profile_for(user.id)
    if profile is not None:
        return profiles.assistant_name_of(profile)
    chat, msg = update.effective_chat, update.effective_message
    iid = _binding_instance(chat.id, _topic_id(msg) if msg is not None else 0) if chat else None
    grant = _LIFE_GRANTS.get((user.id, iid)) if iid else None
    return grant.assistant_name if grant is not None else None


def _assistant_line(update: Update) -> str | None:
    name = _assistant_name(update)
    if not name:
        return None
    return (f"[nome] Per chi ti scrive ti chiami «{name}»: presentati con questo nome "
            f"e firma così quando firmi.")


def _proposals_base_url() -> str:
    return (os.environ.get(life_access.URL_ENV) or os.environ.get("LIFE_BACKEND_URL")
            or "").strip()


async def cmd_proponi(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """``/proponi <testo>``: a contributor proposes a change to this node (#89).

    The intermediate role: who may not change a node may still ask for a change.
    The proposal lands on the operator's review queue (Life), an admin approves
    it; nothing runs here, no session, no tools. Admins don't need it.
    """
    msg, chat, user = update.effective_message, update.effective_chat, update.effective_user
    if msg is None or chat is None or user is None:
        return
    text = " ".join(getattr(context, "args", None) or []).strip()
    if not text:
        text = (msg.text or "").partition(" ")[2].strip()
    if not text:
        await msg.reply_text("Uso: /proponi <cosa cambiare e perché>")
        return
    project, _agent = _topic_binding(chat.id, _topic_id(msg))
    if project is None:
        await msg.reply_text("Usa /proponi dentro la scheda del progetto da cambiare.")
        return
    profile = profile_for(user.id)
    if profile is not None and profile.is_admin:
        await msg.reply_text("Sei admin di questo nodo: scrivi pure la richiesta qui, "
                             "la sessione la esegue.")
        return
    team = manifest.team_of(project)
    grant = await _life_grant(update, project, team)
    if not _grant_allows(grant, "propose") or grant.person_id is None:
        await msg.reply_text("🔒 Proporre una modifica richiede il ruolo contributor.")
        return
    iid = _team_instance(team, update)
    base, token = _proposals_base_url(), os.environ.get("LIFE_ADMIN_TOKEN", "").strip()
    if not iid or not base or not token:
        await msg.reply_text("⚠️ Coda delle proposte non configurata su questa installazione.")
        return
    node = instance_routing.project_node(project) or project.key
    try:
        out = await life_access.propose(
            base, token, iid, grant.person_id, "node_change",
            {"summary": text[:200], "changes": text, "source": "telegram"}, node=node)
    except life_access.ProposalError as exc:
        logger.warning("proposta di %s su %s rifiutata: %s", user.id, node, exc)
        await msg.reply_text(f"⚠️ Proposta non registrata: {exc.detail or exc.status_code}")
        return
    pid = out.get("id")
    await msg.reply_text(f"📝 Proposta{f' #{pid}' if pid else ''} registrata per {node}: "
                         "la valuta un admin.")
    who = html_mod.escape(grant.display_name or user.full_name or str(user.id))
    note = (f"📝 Nuova proposta{f' #{pid}' if pid else ''} da {who} su "
            f"<b>{html_mod.escape(node)}</b> (istanza {html_mod.escape(iid)}):\n"
            f"{html_mod.escape(text[:600])}")
    for uid in profiles.admin_user_ids():
        try:
            await context.bot.send_message(uid, note, parse_mode=ParseMode.HTML)
        except Exception:  # noqa: BLE001 - a DM that fails must not undo the proposal
            logger.warning("avviso proposta all'admin %s non inviato", uid)


async def _warn_unknown_identity(update: Update, context, iid: str | None) -> None:
    """Tell the admins once that an unknown Telegram id wrote, with the link command."""
    user, chat = update.effective_user, update.effective_chat
    if user.id in _UNKNOWN_WARNED:
        return
    _UNKNOWN_WARNED.add(user.id)
    who = " ".join(x for x in (user.full_name, f"@{user.username}" if user.username else "") if x)
    where = getattr(chat, "title", None) or ("chat privata" if chat.type == "private" else str(chat.id))
    text = (
        "👤 Identità sconosciuta (Coordinator#83)\n"
        f"Telegram <code>{user.id}</code> — {html_mod.escape(who or '?')}\n"
        f"ha scritto in {html_mod.escape(where)} (istanza {html_mod.escape(iid or '?')}): rifiutato.\n"
        "Per collegarla, sull'operatore Life:\n"
        f"<code>{html_mod.escape(life_access.invite_hint(user.id, iid))}</code>\n"
        "(aggiungi --tailscale/--github della stessa persona, poi --esegui)"
    )
    default_app = _APPS.get(bot_registry.DEFAULT_BOT)
    senders = [b for b in (getattr(default_app, "bot", None), context.bot) if b is not None]
    for uid in profiles.admin_user_ids():
        for sender in senders:
            try:
                await sender.send_message(uid, text, parse_mode=ParseMode.HTML)
                break
            except Exception:
                logger.warning("unknown-identity notice to %s via %s failed", uid,
                               getattr(sender, "username", "?"))


async def _refuse_access(update: Update, key: str) -> None:
    """One refusal per person and chat per hour: a group keeps talking, we don't."""
    user, chat = update.effective_user, update.effective_chat
    text = LIFE_ACCESS_REFUSALS[key]
    if update.callback_query is not None:
        try:
            await update.callback_query.answer(text, show_alert=True)
        except Exception:
            logger.debug("refusal answer failed", exc_info=True)
        return
    now = time.monotonic()
    last = _REFUSAL_SENT.get((user.id, chat.id))
    if last is not None and now - last < REFUSAL_REPEAT_SEC:
        return
    _REFUSAL_SENT[(user.id, chat.id)] = now
    msg = update.effective_message
    try:
        if msg is not None:
            await msg.reply_text(text)
    except Exception:
        logger.warning("refusal to %s in %s failed", user.id, chat.id)


async def life_access_gate(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Group -3 of every bot: owners pass, everyone else asks Life's operator.

    Refusal raises ``ApplicationHandlerStop``: no archive, no session, no bus, and
    nothing in the reply says which nodes exist.
    """
    user, chat = update.effective_user, update.effective_chat
    if user is None or chat is None or getattr(user, "is_bot", False):
        return
    if update.message is None and update.edited_message is None and update.callback_query is None:
        return
    if profile_for(user.id) is not None:
        return
    cli = life_access.client()
    if cli is None:
        return
    msg = update.effective_message
    iid = _binding_instance(chat.id, _topic_id(msg) if msg is not None else 0)
    if iid is None:
        import instances

        # Nothing to grant here, but an unknown id is still worth knowing: Life
        # answers "unknown identity" before it looks at the instance.
        probe = await cli.check(str(user.id), instances.default_instance_id() or "personal")
        if probe.status == life_access.UNKNOWN:
            await _warn_unknown_identity(update, context, None)
            await _refuse_access(update, life_access.UNKNOWN)
        else:
            await _refuse_access(update, "no-instance")
        logger.warning("life access: %s refused on chat %s (no instance, %s)",
                       user.id, chat.id, probe.status)
        raise ApplicationHandlerStop
    decision = await cli.check(str(user.id), iid, "use")
    if decision.ok:
        _LIFE_GRANTS[(user.id, iid)] = decision
        return
    _LIFE_GRANTS.pop((user.id, iid), None)
    if decision.status == life_access.UNKNOWN:
        await _warn_unknown_identity(update, context, iid)
    logger.warning("life access: %s refused on chat %s instance %s (%s %s)",
                   user.id, chat.id, iid, decision.status, decision.reason)
    await _refuse_access(update, decision.status)
    raise ApplicationHandlerStop


def _is_admin(update: Update) -> bool:
    p = _profile(update)
    return p is not None and p.is_admin


DENIED_TAP = "🔒 Questo pulsante lo può premere solo chi lo ha ricevuto o un admin."


def _tap_allowed(update: Update, owner_id: object = None) -> bool:
    """Whether the presser may act on a card (#89): its owner, or an admin.

    A card can land in a shared group topic, where everyone sees the same
    buttons; the tap acts on the owner's behalf, so only the owner (or an admin)
    may press it. ``SESSION_GATE=off`` restores the old trust.
    """
    if not SESSION_GATE or _is_admin(update):
        return True
    user = update.effective_user
    if user is None or owner_id is None:
        return False
    try:
        return int(owner_id) == user.id
    except (TypeError, ValueError):
        return False


# --- Notifications UI (M4) ---------------------------------------------------
import notif_store
import notifications as _notif


def _notif_line(n) -> str:
    status = "✅" if n.enabled else "🔕"
    return f"{status} {n.title} — <code>{n.schedule}</code> → {', '.join(n.recipients)}"


def _kb_notif_list() -> InlineKeyboardMarkup:
    rows = []
    for n in notif_store.load():
        rows.append(
            [
                InlineKeyboardButton(
                    f"{'✅' if n.enabled else '🔕'} {n.title}",
                    callback_data=pack_cb("notif", "view", n.key),
                )
            ]
        )
    rows.append(
        [InlineKeyboardButton("➕ Nuova", callback_data=pack_cb("notif", "new"))]
    )
    return InlineKeyboardMarkup(rows)


def _kb_notif_view(n) -> InlineKeyboardMarkup:
    toggle = "🔕 Silenzia" if n.enabled else "✅ Attiva"
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    toggle, callback_data=pack_cb("notif", "toggle", n.key)
                )
            ],
            [
                InlineKeyboardButton(
                    "🗑 Elimina", callback_data=pack_cb("notif", "del", n.key)
                )
            ],
            [InlineKeyboardButton("⬅️ Lista", callback_data=pack_cb("notif", "list"))],
        ]
    )


async def cmd_notifiche(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_admin(update):
        await update.effective_message.reply_text(NO_ACCESS_MSG)
        return
    await update.effective_message.reply_text(
        "🔔 <b>Notifiche</b>", parse_mode=ParseMode.HTML, reply_markup=_kb_notif_list()
    )


async def on_notif_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    if not _is_admin(update):
        await q.edit_message_text(NO_ACCESS_MSG)
        return
    parts = parse_cb(q.data)  # ["notif", action, *rest]
    action = parts[1]
    if action == "list":
        await q.edit_message_text(
            "🔔 <b>Notifiche</b>",
            parse_mode=ParseMode.HTML,
            reply_markup=_kb_notif_list(),
        )
    elif action == "view":
        n = notif_store.get(parts[2])
        if n is None:
            await q.edit_message_text(
                "Notifica non trovata.", reply_markup=_kb_notif_list()
            )
            return
        await q.edit_message_text(
            _notif_line(n), parse_mode=ParseMode.HTML, reply_markup=_kb_notif_view(n)
        )
    elif action == "toggle":
        n = notif_store.get(parts[2])
        if n:
            n.enabled = not n.enabled
            notif_store.upsert(n)
            scheduler.apply(n)
            await q.edit_message_text(
                _notif_line(n),
                parse_mode=ParseMode.HTML,
                reply_markup=_kb_notif_view(n),
            )
    elif action == "del":
        scheduler.unschedule(parts[2])
        notif_store.delete(parts[2])
        await q.edit_message_text("🗑 Eliminata.", reply_markup=_kb_notif_list())
    elif action == "new":
        await _notif_wizard_start(update, context)
    elif action == "wp":  # provider chosen
        st = PENDING_NOTIF.get(update.effective_user.id)
        if not st:
            return
        if parts[2] in _notif.MANIFEST_ONLY_PROVIDERS:
            await q.answer("Provider solo da coordinator.toml", show_alert=True)
            return
        st["draft"]["provider"] = parts[2]
        if parts[2] == "agent":
            st["step"] = "project"
            await q.edit_message_text(
                "Quale progetto?", reply_markup=_kb_wizard_projects()
            )
        elif parts[2] == "static":
            st["step"] = "text"
            await q.edit_message_text("Scrivi il testo della notifica:")
        else:  # growbox: no params
            st["step"] = "schedule"
            await q.edit_message_text("Quando?", reply_markup=_kb_wizard_schedule())
    elif action == "wpr":  # agent project chosen
        st = PENDING_NOTIF.get(update.effective_user.id)
        if not st:
            return
        st["draft"]["params"]["project"] = parts[2]
        st["step"] = "prompt"
        await q.edit_message_text("Scrivi il prompt per l'agente:")
    elif action == "ws":  # schedule preset / custom
        st = PENDING_NOTIF.get(update.effective_user.id)
        if not st:
            return
        if parts[2] == "custom":
            st["step"] = "cron"
            await q.edit_message_text("Inserisci un crontab a 5 campi (es. 0 8 * * *):")
        else:
            st["draft"]["schedule"] = _notif.preset_to_cron(parts[2])
            st["step"] = "recipients"
            await q.edit_message_text(
                "A chi?", reply_markup=_kb_wizard_recipients(st["draft"])
            )
    elif action == "wr":  # toggle recipient
        st = PENDING_NOTIF.get(update.effective_user.id)
        if not st:
            return
        _wizard_toggle_recipient(st["draft"], parts[2])
        await q.edit_message_reply_markup(
            reply_markup=_kb_wizard_recipients(st["draft"])
        )
    elif action == "wdone":  # confirm
        await _notif_wizard_finish(update, context)


# --- Notification creation wizard (M4, admin) --------------------------------
PENDING_NOTIF: dict[int, dict] = {}  # user_id -> {"step", "draft"}


def _wizard_toggle_recipient(draft: dict, pkey: str) -> None:
    rec = draft.setdefault("recipients", set())
    rec.symmetric_difference_update({pkey})


def _wizard_build(draft: dict, existing_keys: set[str]):
    base = draft["provider"]
    key = _notif.gen_key(base, existing_keys)
    title = draft.get("title") or f"{draft['provider']} {draft['schedule']}"
    return _notif.Notification(
        key=key,
        title=title,
        provider=draft["provider"],
        schedule=draft["schedule"],
        recipients=tuple(sorted(draft.get("recipients", set()))),
        enabled=True,
        params=draft.get("params", {}),
    )


def _kb_wizard_provider() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "🤖 Agente", callback_data=pack_cb("notif", "wp", "agent")
                ),
                InlineKeyboardButton(
                    "🍄 Coltivazione", callback_data=pack_cb("notif", "wp", "growbox")
                ),
                InlineKeyboardButton(
                    "📝 Testo", callback_data=pack_cb("notif", "wp", "static")
                ),
            ]
        ]
    )


def _kb_wizard_projects() -> InlineKeyboardMarkup:
    rows, row = [], []
    for p in list_projects():
        row.append(
            InlineKeyboardButton(p.label, callback_data=pack_cb("notif", "wpr", p.key))
        )
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return InlineKeyboardMarkup(rows)


def _kb_wizard_schedule() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "Ogni ora", callback_data=pack_cb("notif", "ws", "hourly")
                )
            ],
            [
                InlineKeyboardButton(
                    "Ogni mattina 8:00", callback_data=pack_cb("notif", "ws", "m8")
                )
            ],
            [
                InlineKeyboardButton(
                    "Domenica 11:00", callback_data=pack_cb("notif", "ws", "sun11")
                )
            ],
            [
                InlineKeyboardButton(
                    "Custom (crontab)", callback_data=pack_cb("notif", "ws", "custom")
                )
            ],
        ]
    )


def _kb_wizard_recipients(draft: dict) -> InlineKeyboardMarkup:
    rows = []
    for prof in _notif.profiles.PROFILES:
        mark = "☑️" if prof.key in draft.get("recipients", set()) else "⬜️"
        rows.append(
            [
                InlineKeyboardButton(
                    f"{mark} {prof.label}",
                    callback_data=pack_cb("notif", "wr", prof.key),
                )
            ]
        )
    rows.append(
        [InlineKeyboardButton("✅ Conferma", callback_data=pack_cb("notif", "wdone"))]
    )
    return InlineKeyboardMarkup(rows)


async def _notif_wizard_start(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    uid = update.effective_user.id
    PENDING_NOTIF[uid] = {
        "step": "provider",
        "draft": {"params": {}, "recipients": set()},
    }
    await update.callback_query.edit_message_text(
        "Nuova notifica — scegli la sorgente:", reply_markup=_kb_wizard_provider()
    )


async def _notif_wizard_finish(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    uid = update.effective_user.id
    st = PENDING_NOTIF.get(uid)
    if not st:
        return
    existing = {n.key for n in notif_store.load()}
    notif = _wizard_build(st["draft"], existing)
    if notif.provider in _notif.MANIFEST_ONLY_PROVIDERS:
        await update.callback_query.edit_message_text(
            "❌ provider solo da coordinator.toml")
        PENDING_NOTIF.pop(uid, None)
        return
    errs = _notif.validate(notif)
    if errs:
        await update.callback_query.edit_message_text("❌ " + "; ".join(errs))
        PENDING_NOTIF.pop(uid, None)
        return
    notif_store.upsert(notif)
    scheduler.apply(notif)
    PENDING_NOTIF.pop(uid, None)
    await update.callback_query.edit_message_text(
        "✅ Creata:\n" + _notif_line(notif),
        parse_mode=ParseMode.HTML,
        reply_markup=_kb_notif_list(),
    )


async def _notif_text_step(update: Update, text: str) -> bool:
    """Consume a wizard text step. Return True if handled (caller must stop)."""
    uid = update.effective_user.id
    st = PENDING_NOTIF.get(uid)
    if not st:
        return False
    step, draft = st["step"], st["draft"]
    if step == "text":
        draft["params"]["text"] = text
        st["step"] = "schedule"
        await update.effective_message.reply_text(
            "Quando?", reply_markup=_kb_wizard_schedule()
        )
    elif step == "prompt":
        draft["params"]["prompt"] = text
        st["step"] = "schedule"
        await update.effective_message.reply_text(
            "Quando?", reply_markup=_kb_wizard_schedule()
        )
    elif step == "cron":
        if not _notif.validate_schedule(text):
            await update.effective_message.reply_text("❌ crontab non valido, riprova:")
            return True
        draft["schedule"] = text
        st["step"] = "recipients"
        await update.effective_message.reply_text(
            "A chi?", reply_markup=_kb_wizard_recipients(draft)
        )
    else:
        return False
    return True


def _stale_limit(chat_id) -> int:
    """Max age in seconds for this chat: its manifest's catch-up window, else global."""
    if chat_id is not None:
        try:
            window = manifest.catch_up_seconds(chat_id)
        except Exception:
            logger.exception("catch-up window lookup failed for chat=%s", chat_id)
            window = None
        if window is not None:
            return max(window, STALE_MAX_AGE_SEC)
    return STALE_MAX_AGE_SEC


def _is_stale(msg) -> bool:
    if _REPLAY.get() is not None:
        return False  # old on purpose: the replay is the answer to staleness
    if msg.date is None:
        return False
    age = (dt.datetime.now(dt.UTC) - msg.date).total_seconds()
    limit = _stale_limit(getattr(msg, "chat_id", None))
    if age <= limit:
        return False
    # Logged on purpose: the night of 2026-10-06 a whole group's messages were
    # dropped here with no trace, and it looked like the bot never got them.
    logger.info(
        "stale message dropped chat=%s age=%ds limit=%ds",
        getattr(msg, "chat_id", None), int(age), limit,
    )
    return True


async def _send_html_chunks(target_msg, text: str, reply_markup=None) -> None:
    """Send text in Telegram-safe HTML chunks, plain-text fallback on error.

    ``reply_text`` inherits the incoming message's topic, so replies land in the
    right tab automatically. ``reply_markup`` (if given) attaches only to the
    LAST chunk, so a multi-message reply gets one set of buttons, not one per
    chunk.
    """
    chunks = list(chunk_text(text))
    for i, chunk in enumerate(chunks):
        markup = reply_markup if i == len(chunks) - 1 else None
        try:
            await target_msg.reply_text(
                chunk,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
                reply_markup=markup,
            )
        except Exception as exc:
            logger.warning("HTML reply failed (%s), plain fallback", exc)
            try:
                await target_msg.reply_text(_strip_html(chunk), reply_markup=markup)
            except Exception:
                logger.exception("plain reply also failed")


async def _send_chunks_to(bot, chat_id: int, text: str) -> None:
    """Send ``text`` to an explicit chat id (notifications), HTML with plain
    fallback. Mirrors _send_html_chunks but targets a chat id, not a reply."""
    for chunk in chunk_text(text):
        try:
            await bot.send_message(
                chat_id,
                chunk,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )
        except Exception as exc:
            logger.warning("notif HTML send failed (%s), plain fallback", exc)
            try:
                await bot.send_message(chat_id, _strip_html(chunk))
            except Exception:
                logger.exception("notif plain send also failed")


async def _send_chunks_thread(bot, chat_id: int, thread_id, text: str) -> None:
    """Send ``text`` into a specific forum topic (or the General/private chat
    when ``thread_id`` is falsy), HTML with plain-text fallback. Mirrors
    _send_html_chunks but targets an explicit (chat, thread) instead of replying
    to a message — needed when the bot must write into a topic *other* than the
    one the trigmemberng update came from (e.g. a freshly spawned topic's seed)."""
    thread = thread_id or None  # Telegram wants None (not 0) for General/private
    for chunk in chunk_text(text):
        try:
            await bot.send_message(
                chat_id,
                chunk,
                message_thread_id=thread,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )
        except Exception as exc:
            logger.warning("thread HTML send failed (%s), plain fallback", exc)
            try:
                await bot.send_message(
                    chat_id, _strip_html(chunk), message_thread_id=thread
                )
            except Exception:
                logger.exception("thread plain send also failed")


# --- Pipeline command + gate (M-pipeline) ------------------------------------
# Module-level bot ref, captured in on_startup so _post_gate (called by the
# pipeline with no bot arg) can send into a topic with an inline keyboard.
# Stays the DEFAULT bot forever: every existing consumer (task cards,
# butler, _post_gate, hub) reads it directly and must keep working untouched.
_BOT = None
# Multi-bot lookup: bot name -> telegram.Bot, filled by each app's post_init.
# ``_BOTS["default"]`` is the same object as ``_BOT``; extra, node-dedicated
# bots register themselves here so a node-scoped path can pick its own sender.
_BOTS: dict[str, object] = {}
# Same keys, the PTB Application behind each bot: the replay feeds an old
# message back through ``process_update`` of the bot that would serve it.
_APPS: dict[str, object] = {}


def bot_for(name: str = bot_registry.DEFAULT_BOT):
    """The telegram.Bot serving ``name``, falling back to the default bot.

    The fallback is what keeps every pre-multi-bot caller correct: an unknown
    or not-yet-started extra bot resolves to the Coordinator bot, i.e. today's
    behaviour, instead of None.
    """
    return _BOTS.get(name) or _BOT


def _kb_gate(pid: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "✅ Approva deploy",
                    callback_data=pack_cb("pl", "gate", str(pid), "ok"),
                ),
                InlineKeyboardButton(
                    "❌ Rifiuta", callback_data=pack_cb("pl", "gate", str(pid), "no")
                ),
            ]
        ]
    )


def _parse_pipeline_args(arg: str) -> tuple[str | None, str | None]:
    parts = (arg or "").strip().split(maxsplit=1)
    if len(parts) < 2:
        return (None, None)
    return (parts[0], parts[1])


async def _post_gate(pid: int, chat_id: int, topic_id: int, text: str) -> None:
    """Post a gate prompt with approve/reject buttons into (chat_id, topic_id).

    Injected into ``pipeline.configure(post_gate=...)``; the pipeline calls it
    without a bot arg, so it uses the module-level _BOT captured at startup."""
    if _BOT is None:
        logger.error("pipeline gate: bot not ready (pid=%s)", pid)
        return
    await _BOT.send_message(
        chat_id=chat_id,
        message_thread_id=topic_id or None,
        text=text,
        reply_markup=_kb_gate(pid),
    )


async def on_pipeline_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    q = update.callback_query
    await q.answer()
    _, _, pid_s, decision = parse_cb(q.data)
    try:
        await q.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass
    await pipeline.on_gate_decision(int(pid_s), decision)


async def on_confirm_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Generic confirm-gate: ✅/❌ on an agent proposal. Re-invoke the same
    (chat,topic) session — it remembers what it proposed (resumed transcript)."""
    q = update.callback_query
    if SESSION_GATE and not await _session_full(update):
        # The ✅ makes the agent act with tools: only who may drive a session presses it (#89).
        await q.answer("🔒 Solo un admin può confermare.", show_alert=True)
        return
    await q.answer()
    _, decision, chat_s, topic_s = parse_cb(q.data)
    chat_id, topic_id = int(chat_s), int(topic_s)
    try:
        await q.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass
    project, agent = _topic_binding(chat_id, topic_id)
    if project is None or agent is None:
        logger.warning("confirm callback: no binding for (%s,%s)", chat_id, topic_id)
        return
    if decision == "ok":
        prompt = (
            "[CMD:/confirm-yes] L'utente ha premuto Conferma. "
            "Esegui l'azione che hai proposto e riporta l'esito."
        )
    else:
        prompt = (
            "[CMD:/confirm-no] L'utente ha premuto Annulla. "
            "Non eseguire nulla; conferma in una riga che hai annullato."
        )
    await _invoke_into_topic(context, chat_id, topic_id, project, agent, prompt)


async def cmd_pipeline(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _allowed(update):
        return
    project_key, task = _parse_pipeline_args(" ".join(context.args or []))
    if not project_key or not task:
        await update.message.reply_text(
            "Uso: /pipeline <progetto> <task>\nEs: /pipeline offerte allinea e deploya"
        )
        return
    project = resolve_project(project_key)
    if project is None:
        await update.message.reply_text(f"Progetto sconosciuto: {project_key}")
        return
    stages = manifest.pipeline_stages_of(project)
    if not stages:
        cfg = hub._S["config"]
        dev_agent = hub.dev_agent_of(project)
        # The sidecar deployer is the Coordinator's OWN self-deploy infra (fixed
        # worker_service/project_dir in deployer_config): only route the deploy
        # stage to it when deploying the Coordinator itself. For any other
        # project it would recreate coordinator-worker instead — a silent
        # misdeploy with ok=True. Other projects keep the legacy prod agent.
        # I prodotti restano single-target: `<key>-prod` gira gia' sul server
        # dove quel prodotto vive (un prodotto pubblico sul box pubblico, uno
        # interno su quello interno), quindi il deploy segue il progetto.
        # Il Coordinator e' l'unico che sta su ENTRAMBI, e usa tutti i deployer.
        use_deployer = bool(
            cfg and cfg.deployer_agents and project.key == "coordinator"
        )
        deploy_agent = (
            list(cfg.deployer_agents)
            if use_deployer
            else hub.prod_agents_of(project)
        )
        stages = pipeline_stages.default_stages(dev_agent, deploy_agent)
    tenant = hub._default_tenant()
    pid = await pipeline.start_pipeline(tenant, project.key, task, stages=stages)
    await update.message.reply_text(f"🛠 Pipeline #{pid} avviata per {project.label}.")


def _parse_migrate_args(arg: str) -> tuple[str | None, str]:
    parts = (arg or "").strip().split()
    if not parts:
        return (None, "head")
    project = parts[0]
    rev = parts[1] if len(parts) > 1 else "head"
    return (project, rev)


async def cmd_migrate(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _allowed(update):
        return
    project_key, rev = _parse_migrate_args(" ".join(context.args or []))
    if not project_key:
        await update.message.reply_text(
            "Uso: /migrate <progetto> [rev]\nEs: /migrate db head"
        )
        return
    project = resolve_project(project_key)
    if project is None:
        await update.message.reply_text(f"Progetto sconosciuto: {project_key}")
        return
    targets = manifest.db_targets_of(project)
    derived_note = ""
    if not targets:
        # No explicit [[db_target]] declared: derive a single implicit target from
        # the live registry — the project's own prod bus identity on the prod device
        # (the same identity the pipeline deploys to). Covers the common single-host
        # case ("un agente basta"); a DB genuinely replicated across machines still
        # needs explicit [[db_target]] entries in coordinator.toml.
        cfg = hub._S.get("config")
        if cfg is None:
            await update.message.reply_text(
                f"Nessun db_target per {project.label} e registro non disponibile. "
                f"Aggiungi [[db_target]] in coordinator.toml."
            )
            return
        targets = [
            manifest.DbTargetDecl(
                agent=hub.prod_agents_of(project)[0], host=cfg.prod_device
            )
        ]
        derived_note = " · target derivato dal registro"
    tenant = hub._default_tenant()
    mid = await migrate.start_migration(tenant, project.key, rev, targets=targets)
    await update.message.reply_text(
        f"🗃 Migrazione #{mid} avviata su {len(targets)} target ({rev}).{derived_note}"
    )


def _parse_restructure_args(arg: str) -> str | None:
    parts = (arg or "").strip().split()
    return parts[0] if parts else None


async def cmd_restructure(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _allowed(update):
        return
    token = _parse_restructure_args(" ".join(context.args or []))
    if token:
        proj = resolve_project(token)
        agent = proj.key if proj else token  # raw identity if not in registry
        targets = [{"agent": agent, "host": agent}]
        label = agent
    else:
        # Broadcast to every node in the active registry (design §6 rollout).
        targets = [{"agent": p.key, "host": p.key} for p in projects.PROJECTS]
        label = f"{len(targets)} nodi"
    if not targets:
        await update.message.reply_text("Nessun nodo bersaglio nel registro.")
        return
    tenant = hub._default_tenant()
    rid = await restructure.start_restructure(tenant, targets=targets)
    await update.message.reply_text(
        f"🧬 Restructure #{rid} avviato su {label} (persona canonica)."
    )


# --- Telegram UX: reactions, in-place finish, outbox attachments -------------
# Lightweight ACK reactions on the user's message (received -> done/error).
# All three are in Telegram's default allowed-reaction set.
REACT_WORK = "👀"  # received / working
REACT_DONE = "👍"  # completed
REACT_ERR = "😢"  # error / interrupted
REACT_PROD = "🎉"  # fix live in production (see PROD_SENTINEL)

# Extensions Telegram renders inline as photos; everything else ships as a doc.
_IMG_EXT = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
_OUTBOX_MAX_BYTES = 50 * 1024 * 1024  # Telegram bot upload cap


async def _react(msg, emoji: str) -> None:
    """Best-effort single-emoji reaction as a turn ACK. Reactions can fail (no
    rights in the chat, emoji not allowed) and must never break the turn."""
    try:
        await msg.set_reaction([emoji])
    except Exception as exc:
        logger.warning("reaction %s failed: %r", emoji, exc)


async def _finish_turn(thinking, reply: str, send_long, reply_markup=None) -> None:
    """Turn the ``⏳ sto pensando…`` placeholder into the answer: if the reply
    fits one Telegram message, edit it in place (plugin-style working->done);
    otherwise delete the placeholder and stream the reply as chunks via
    ``send_long`` (a zero-arg coroutine factory). HTML first, plain fallback.
    ``reply_markup`` (if given) attaches to the in-place edit; callers using
    the chunked path must bake it into their ``send_long`` factory too (see
    _invoke_and_reply)."""
    chunks = list(chunk_text(reply))
    if len(chunks) == 1:
        try:
            await thinking.edit_text(
                chunks[0],
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
                reply_markup=reply_markup,
            )
            return
        except Exception:
            try:
                await thinking.edit_text(_strip_html(chunks[0]), reply_markup=reply_markup)
                return
            except Exception:
                pass  # fall through to delete + resend
    try:
        await thinking.delete()
    except Exception:
        pass
    await send_long()


async def _send_outbox(bot, chat_id: int, thread, workspace: str) -> None:
    """Deliver any files an agent dropped in ``<workspace>/outbox/`` as Telegram
    attachments (images inline, the rest as documents), then move each sent file
    into ``outbox/.sent/`` so it is never resent. Best-effort: file/IO or upload
    errors are logged and never break the turn. The ``.sent`` archive mirrors the
    project's ``old/`` philosophy — move, don't delete."""
    outbox = os.path.join(workspace, "outbox")
    if not os.path.isdir(outbox):
        return
    try:
        names = sorted(os.listdir(outbox))
    except OSError:
        return
    sent_dir = os.path.join(outbox, ".sent")
    for name in names:
        path = os.path.join(outbox, name)
        if name == ".sent" or not os.path.isfile(path):
            continue
        try:
            if os.path.getsize(path) > _OUTBOX_MAX_BYTES:
                logger.warning("outbox: %s over 50MB, skipped", name)
                continue
            ext = os.path.splitext(name)[1].lower()
            with open(path, "rb") as fh:
                if ext in _IMG_EXT:
                    await bot.send_photo(chat_id, fh, message_thread_id=thread)
                else:
                    await bot.send_document(
                        chat_id, fh, filename=name, message_thread_id=thread
                    )
        except Exception:
            logger.exception("outbox: failed to send %s", name)
            continue
        try:
            os.makedirs(sent_dir, exist_ok=True)
            replace_with_retry(path, os.path.join(sent_dir, name))
        except OSError:
            logger.warning("outbox: sent %s but could not archive it", name)


async def _claude_turn(
    chat_id: int,
    topic_id: int,
    project: Project,
    agent: Agent,
    prompt: str,
    on_progress,
    prefs: model_prefs.Prefs | None = None,
) -> str:
    """Resume the (chat,topic) session, run claude in the project's workspace,
    persist the new session id and log the turn; return the reply text.

    Shared core of _invoke_and_reply (reply in place) and _invoke_into_topic
    (send into an explicit topic): the only thing that differs between the two
    is where the typing/thinking UI lives and how the reply is delivered.

    The subagent persona is re-appended on EVERY turn (new and resumed): a
    resumed turn replays only the transcript, not the turn-1 system prompt, so
    without re-appending, agent rules (e.g. personalChef's "update the pantry when
    the user mentions an ingredient" reflex) silently evaporate from turn 2 on —
    exactly when the user corrects the pantry mid-conversation. ``--append-system-prompt``
    is additive and idempotent enough that re-sending it each turn is safe.
    """
    # One turn at a time per card (#63): with concurrent_updates on, a photo and
    # its caption (or two quick messages) used to read the same resume id and
    # drive two processes on one session -> opencode "database is locked" and a
    # second turn that never saw the first. Queue, never drop.
    async with _turn_lock(chat_id, topic_id):
        return await _claude_turn_locked(
            chat_id, topic_id, project, agent, prompt, on_progress, prefs
        )


# Per-(chat, topic) turn locks. Never pruned on purpose: one tiny Lock per card
# ever used, and deleting a lock someone is waiting on would break the queue.
_TURN_LOCKS: dict[tuple[int, int], asyncio.Lock] = {}


def _turn_lock(chat_id: int, topic_id) -> asyncio.Lock:
    key = (chat_id, topic_id)
    lock = _TURN_LOCKS.get(key)
    if lock is None:
        lock = _TURN_LOCKS[key] = asyncio.Lock()
    return lock


async def _claude_turn_locked(
    chat_id: int,
    topic_id: int,
    project: Project,
    agent: Agent,
    prompt: str,
    on_progress,
    prefs: model_prefs.Prefs | None,
) -> str:
    _tg = manifest.telegram_of(project)
    _scope = _tg.session_scope if _tg is not None else None
    reset_gen = _RESET_GEN.get((chat_id, topic_id), 0)
    # Only pass scope when set: the historical (chat, topic) call stays intact.
    resume_id = (
        _get_session(chat_id, topic_id, scope=_scope)
        if _scope
        else _get_session(chat_id, topic_id)
    )
    persona = agent.persona
    # No explicit prefs (internal callers: seeds, cards, butler) -> the card's
    # sticky choice still applies, so a topic pinned to opus stays on opus.
    if prefs is None:
        prefs = _get_prefs(chat_id, topic_id)
    # The project's manifest default fills what neither the message nor the
    # card chose (#65): inline > card > project > env.
    prefs = prefs.over_default(_project_default_prefs(project))
    backend, model, extra_env = _resolve_turn(project.key, prefs)
    host_cwd = _topic_host_cwd(project)
    readonly = claude_runner.SESSION_READONLY.get()
    if readonly:
        # Read-only (#89): claude with a closed tool list, in the container, cold.
        backend, host_cwd = "claude", None
        persona = ((persona or "") + "\n\n" + READONLY_PERSONA).strip()
    # An agent node (Life#157 §3.4) answers with its manifest's tools, never with
    # bypassPermissions: claude, in the container, cold, the manifest's model
    # unless the message or the card asked for another claude model.
    agent_node = agent_nodes.node_of(project.workspace)
    tools_token = None
    if agent_node is not None:
        if backend not in (None, "claude"):
            model, extra_env = None, None
        backend, host_cwd = "claude", None
        model = model or agent_node.model
        tools_token = claude_runner.SESSION_TOOLS.set(",".join(agent_node.tools))
    if host_cwd:
        # Terminal parity means claude: with no explicit /modello pick the
        # deployment default (AI_BACKEND, possibly opencode) must not silently
        # pull the turn back into the container. An explicit pick still wins.
        if backend is None:
            backend = "claude"
        if backend == "claude":
            persona = (
                (persona or "") + "\n\n" + hub._WAKE_ENV_HOST.format(cwd=host_cwd)
            ).strip()
        else:
            host_cwd = None
    effort = prefs.effort
    new_sid = None
    ok = True
    t0 = time.monotonic()
    run = None
    try:
        served = "cold"
        # The warm live path only speaks claude's protocol (see CLAUDE_LIVE_SESSIONS
        # above): a turn whose resolved backend is explicitly "opencode" (a /modello
        # DeepSeek/Zen pick) must never go through it even if the deployment default
        # is claude -- backend=None means "no override", which does mean claude here.
        # A host run (HOST_EXEC_TOPIC_PROJECTS) never goes warm: the live
        # process lives in the container, which is exactly what it avoids.
        if (
            CLAUDE_LIVE_SESSIONS
            and backend in (None, "claude")
            and not host_cwd
            and not readonly
            and agent_node is None
        ):
            try:
                run = await _live_manager().run_turn(
                    (chat_id, topic_id),
                    prompt=prompt,
                    workspace=project.workspace,
                    persona=persona,
                    resume_id=resume_id,
                    on_progress=on_progress,
                    model=model,
                    effort=effort,
                )
                reply, new_sid = run
                served = "live"
            except LiveSessionUnavailable as exc:
                logger.info("live session unavailable (%s); cold fallback", exc)
                run = await run_claude(
                    prompt,
                    workspace=project.workspace,
                    persona=persona,
                    resume_id=resume_id,
                    on_progress=on_progress,
                    extra_env=extra_env,
                    model=model,
                    effort=effort,
                    backend=backend,
                    host_cwd=host_cwd,
                )
                reply, new_sid = run
                served = "live->cold"
        else:
            run = await run_claude(
                prompt,
                workspace=project.workspace,
                persona=persona,
                resume_id=resume_id,
                on_progress=on_progress,
                extra_env=extra_env,
                model=model,
                effort=effort,
                backend=backend,
                host_cwd=host_cwd,
            )
            reply, new_sid = run
        logger.info(
            "turn served via %s in %dms (proj=%s)",
            served,
            int((time.monotonic() - t0) * 1000),
            project.key,
        )
        # Ciò che il turno ha cambiato sul disco esce dal container adesso.
        # Senza questo il lavoro resta dirty in un repo che nessuno guarda:
        # tre indicazioni date in chat il 2026-09-06 erano state applicate
        # davvero e non erano mai arrivate su nessun device (vedi
        # indicazioni.py). Un turno di sola lettura non trova nulla di sporco
        # e non costa niente.
        await asyncio.to_thread(
            indicazioni.pubblica,
            project.workspace,
            motivo=f"lavoro dal turno telegram su {project.key}",
        )
        # What this turn cost, into Life's ledger (#28). "telegram" as the
        # caller keeps interactive turns separable from the autonomous spend
        # (notifications, wakes, butler) — the two answer different questions.
        llm_usage.record(run, node=project.key, caller="telegram")
        if new_sid and _RESET_GEN.get((chat_id, topic_id), 0) == reset_gen:
            _set_session(chat_id, topic_id, new_sid)
        elif new_sid:
            logger.info("session of %s:%s reset during the turn: not stored",
                        chat_id, topic_id)
        ok = not reply.startswith(("❌", "⏱"))  # run_claude error/timeout markers
    except Exception as exc:
        logger.exception("claude invocation failed")
        reply = f"❌ errore interno: {exc!r}"
        ok = False
    finally:
        if tools_token is not None:
            claude_runner.SESSION_TOOLS.reset(tools_token)
    _log_turn(
        chat_id=chat_id,
        topic_id=topic_id,
        project_key=project.key,
        agent_name=agent.name,
        prompt=prompt,
        reply=reply,
        resume_id=resume_id,
        session_id=new_sid,
        elapsed_ms=int((time.monotonic() - t0) * 1000),
        ok=ok,
    )
    return _md_to_html(reply)


ENGINE_ERROR_GROUP_MSG = "⚠️ Non ci sono riuscito, ho avvisato the owner."
NEW_CONVERSATION_MSG = "🆕 Nuova conversazione."


def _clear_mode(chat_id: int, topic_id: int) -> bool:
    if TOPIC_MODES.pop((chat_id, topic_id), None) is not None:
        _save_state()
        return True
    return False


def _set_mode(chat_id: int, topic_id: int, mode: dict) -> None:
    TOPIC_MODES[(chat_id, topic_id)] = dict(mode, day=_local_now().date().isoformat())
    _save_state()


def _mode_prefix(chat_id: int, topic_id: int, project: Project) -> str | None:
    """``[Modalità: <label> — <breadcrumb>]`` for the card's active mode, if any.

    A mode of another project is ignored; with ``session_scope = "day"`` a mode
    from an earlier local day is dropped together with the session.
    """
    mode = TOPIC_MODES.get((chat_id, topic_id))
    if not mode or mode.get("project") != project.key:
        return None
    tg = manifest.telegram_of(project)
    if (tg is not None and tg.session_scope == "day"
            and mode.get("day") != _local_now().date().isoformat()):
        _clear_mode(chat_id, topic_id)
        return None
    return group_menu.mode_prefix(mode.get("label", ""), mode.get("breadcrumb", ""))


def _dm_project_for(update: Update) -> Project | None:
    """The DM default project of the sender's profile (PROFILE_<KEY>_DM_PROJECT)."""
    profile = _profile(update)
    if profile is None:
        return None
    pk = profiles.dm_project_for(profile)
    return resolve_project(pk) if pk else None


def _maybe_autobind_dm(update: Update) -> bool:
    """Bind an unbound private chat to the profile's DM project. True when bound now."""
    chat = update.effective_chat
    if chat is None or getattr(chat, "type", None) != "private" or (chat.id, 0) in TOPIC_BINDING:
        return False
    project = _dm_project_for(update)
    if project is None:
        return False
    tg = manifest.telegram_of(project)
    agent = resolve_agent(project, tg.agent if tg else "generale") or projects.GENERALE
    _bind_topic(chat.id, 0, project.key, agent.name)
    logger.info("dm %s auto-bound to %s (profile dm_project)", chat.id, project.key)
    return True


def _menu_binding_here(update: Update) -> tuple[str, list] | None:
    """(project_key, menu items) for this chat/topic: a project group with
    ``[[menu]]``, or a DM bound to the sender's profile DM project."""
    chat, msg = update.effective_chat, update.effective_message
    if chat is None or msg is None:
        return None
    if getattr(chat, "type", None) == "private":
        project = _dm_project_for(update)
        bound = TOPIC_BINDING.get((chat.id, 0))
        if project is None or bound is None or bound[0] != project.key:
            return None
        items = manifest.menu_of(project)
        return (project.key, items) if items else None
    return manifest.menu_binding(chat.id, _topic_id(msg))


async def _notify_owner(tg_bot, text: str) -> None:
    """DM every owner-profile user; never raises."""
    owner_ids = profiles.user_ids_for("owner")
    if not owner_ids:
        logger.warning("owner DM skipped (no owner id mapped): %s", text[:120])
        return
    for uid in owner_ids:
        try:
            await tg_bot.send_message(uid, text)
        except Exception:
            logger.exception("owner DM to %s failed", uid)


def _remember_topic_name(chat_id: int, thread_id: int, name: str | None) -> None:
    name = (name or "").strip()
    if not thread_id or not name:
        return
    if TOPIC_NAMES.get((chat_id, thread_id)) != name:
        TOPIC_NAMES[(chat_id, thread_id)] = name
        _save_state()


def _learn_topic_name_from(msg) -> None:
    """Learn a topic name from a message: its own service payload, or the
    implicit reply to the topic's creation message every topic message has."""
    chat = getattr(msg, "chat", None)
    chat_id = getattr(chat, "id", None)
    if chat_id is None:
        chat_id = getattr(msg, "chat_id", None)
    thread = _topic_id(msg)
    if chat_id is None or not thread:
        return
    for src in (msg, getattr(msg, "reply_to_message", None)):
        if src is None:
            continue
        for attr in ("forum_topic_edited", "forum_topic_created"):
            payload = getattr(src, attr, None)
            name = getattr(payload, "name", None) if payload is not None else None
            if isinstance(name, str) and name:
                _remember_topic_name(chat_id, thread, name)
                return


TOPIC_SERVICE_FILTER = (
    filters.StatusUpdate.FORUM_TOPIC_CREATED | filters.StatusUpdate.FORUM_TOPIC_EDITED
)


async def handle_topic_service(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """forum_topic_created / forum_topic_edited: record the topic name, and
    bind a topic created by hand in a project's group to that project."""
    msg = update.effective_message
    chat = update.effective_chat
    if msg is None:
        return
    _learn_topic_name_from(msg)
    tid = _topic_id(msg)
    if chat is None or not tid or (chat.id, tid) in TOPIC_BINDING:
        return
    owner = _group_owner_binding(chat.id)
    if owner is not None:
        _bind_topic(chat.id, tid, owner[0], owner[1])


def _group_owner_binding(chat_id: int) -> tuple[str, str] | None:
    """(project_key, agent_name) the whole group belongs to: its topic-0
    binding, else the project whose dedicated group it is."""
    bound = TOPIC_BINDING.get((chat_id, 0))
    if bound is not None:
        return bound
    grp = manifest.group_project_for_chat(chat_id)
    return (grp[0], grp[1].agent) if grp is not None else None


def _maybe_log_chat_turn(
    update: Update,
    project: Project,
    group_proj,
    menu_prompt: str | None,
    reply: str,
    topic_name: str | None,
) -> None:
    """Append the turn to the project's ``[telegram] chat_log`` (opt-in).

    Only the project's own group or a private chat count. Never raises.
    """
    try:
        tg = manifest.telegram_of(project)
        if tg is None or not tg.chat_log:
            return
        chat, msg = update.effective_chat, update.effective_message
        if chat.type == "private":
            # DMs are logged only for people routed there by their profile's
            # DM project (e.g. a member), never the owner's own test DMs.
            prof = _profile(update)
            if prof is None or profiles.dm_project_for(prof) != project.key:
                return
        elif group_proj is None or group_proj[0] != project.key:
            return
        user = update.effective_user
        sender = (user.first_name or user.username or str(user.id)) if user else "sconosciuto"
        if menu_prompt is not None:
            user_text, reply_to = f"[menu] {menu_prompt[:300]}", None
        else:
            user_text = (msg.text or msg.caption or "").strip() or "[allegato]"
            reply_to = chat_session.reply_to_line(getattr(msg, "reply_to_message", None))
        chat_session.append_chat_log(
            project.workspace,
            tg.chat_log,
            when=_local_now(),
            sender=sender,
            user_text=user_text,
            reply_text=chat_session.html_to_text(reply),
            reply_to=reply_to,
            topic=topic_name,
        )
    except Exception:
        logger.warning("chat log skipped for %s", project.key, exc_info=True)


def _throttled_progress(thinking):
    """Build an ``on_progress`` callback that edits the ``thinking`` note as
    tools run, throttled to avoid Telegram rate limits and "not modified"."""
    state = {"label": "", "at": 0.0}

    async def on_progress(label: str) -> None:
        now = time.monotonic()
        if label == state["label"] or now - state["at"] < 3.0:
            return
        state["label"], state["at"] = label, now
        try:
            await thinking.edit_text(f"⏳ {label}")
        except Exception:
            pass

    return on_progress


async def _reply_or_plain(msg, text: str, *, quote: bool = False):
    """Reply to ``msg``, falling back to a plain message in the same topic.

    A replayed message may no longer exist (deleted, moved between topics):
    Telegram then answers ``Message to be replied not found`` and the whole
    turn died before producing a single word. The quote is a nicety; the
    answer is not, so a failed quote degrades to an unquoted send."""
    try:
        return await (msg.reply_text(text, do_quote=True) if quote else msg.reply_text(text))
    except BadRequest as exc:
        if "replied not found" not in str(exc).lower():
            raise
        logger.warning("reply target %s gone, sending without quote", msg.message_id)
        return await msg.get_bot().send_message(
            msg.chat_id, text, message_thread_id=getattr(msg, "message_thread_id", None)
        )


# #89: a session (agent with tools) only for owners/admins; everyone else gets a
# read-only one. SESSION_GATE=off restores the old behaviour (kill switch).
SESSION_GATE = os.environ.get("SESSION_GATE", "on").strip().lower() != "off"
READONLY_PERSONA = (
    "[Sessione in SOLA LETTURA] Chi ti scrive non è owner né admin di questo nodo: "
    "puoi leggere file e rispondere, non puoi eseguire comandi né modificare nulla. "
    "Se ti chiede di fare qualcosa, dillo in una riga e indica che serve un admin."
)


async def _session_full(update: Update) -> bool:
    """Whether the sender may drive an agent WITH tools: an admin profile, or a
    Life role that covers ``edit``. Everyone else (a non-admin profile, viewer,
    contributor) is read-only, whatever the message says."""
    user = update.effective_user
    if user is None:
        return False
    profile = profile_for(user.id)
    if profile is not None:
        return profile.is_admin
    grant = await _life_grant(update)
    return grant is not None and grant.ok and grant.role is not None and _grant_allows(grant, "edit")


async def _invoke_and_reply(
    update: Update,
    project: Project,
    agent: Agent,
    prompt: str,
    *,
    context_prefix: str | None = None,
) -> None:
    readonly = SESSION_GATE and not await _session_full(update)
    token = claude_runner.SESSION_READONLY.set(readonly)
    try:
        await _invoke_and_reply_open(update, project, agent, prompt, context_prefix=context_prefix)
    finally:
        claude_runner.SESSION_READONLY.reset(token)


async def _invoke_and_reply_open(
    update: Update,
    project: Project,
    agent: Agent,
    prompt: str,
    *,
    context_prefix: str | None = None,
) -> None:
    """Keep-alive typing, run a turn for the update's (chat,topic), reply in
    place. Replies inherit the incoming message's topic, so they land in the
    right tab automatically.

    This is the human path, so it is where an inline ``opus/high:`` prefix is
    honoured: it wins over the card's sticky /modello choice for this turn only.
    """
    msg = update.effective_message
    chat = update.effective_chat
    if not msg or not chat:
        return
    topic_id = _topic_id(msg)
    context_bot = update.get_bot()
    inline, prompt = model_prefs.parse_prefix(prompt)
    # Project group (dedicated): engine errors are masked, forum topics carry
    # their name. None elsewhere (DMs, the command room, shared forums).
    group_proj = (
        manifest.group_project_for_chat(chat.id)
        if getattr(chat, "type", None) in ("group", "supergroup")
        else None
    )
    is_menu_tap = getattr(update, "callback_query", None) is not None
    topic_name = None
    if group_proj is not None and topic_id:
        if not is_menu_tap:
            _learn_topic_name_from(msg)
        topic_name = TOPIC_NAMES.get((chat.id, topic_id))
    # Extra context goes AFTER the inline "opus/high:" prefix is parsed, so it
    # can never shadow it. Order: topic, reply-to/extra context, active mode.
    mode_prefix = None if is_menu_tap else _mode_prefix(chat.id, topic_id, project)
    prompt = "\n".join(
        p
        for p in (chat_session.topic_prefix(topic_name), context_prefix, mode_prefix, prompt)
        if p
    )
    prefs = inline.merged_over(_get_prefs(chat.id, topic_id)).over_default(
        _project_default_prefs(project)
    )
    # A replayed message: the agent is told it is late and why, whatever path
    # (text, photo, group ingest, lesson) built the prompt.
    replay = _REPLAY.get()
    if replay is not None:
        prompt = f"{replay_unanswered.prompt_header(replay.get('rec') or {})}\n\n{prompt}"

    async def send_typing():
        try:
            await context_bot.send_chat_action(
                chat.id, ChatAction.TYPING, message_thread_id=msg.message_thread_id
            )
        except Exception:
            pass

    await send_typing()
    # Echo the resolved pair when it is not the plain default, so a typo in the
    # prefix ("mzx: ...") is visible instead of silently ignored.
    note = f"⏳ sto pensando… ({prefs.label()})" if prefs else "⏳ sto pensando…"
    if _turn_lock(chat.id, topic_id).locked():
        note = "⏳ in coda: finisco il turno in corso e arrivo…"
    thinking = await _reply_or_plain(msg, REPLAY_NOTE if replay is not None else note,
                                     quote=replay is not None)
    if replay is not None:
        note = REPLAY_NOTE
    on_progress = _throttled_progress(thinking)
    _LAST_REQUEST_MSG[(chat.id, topic_id)] = msg.message_id
    await _react(msg, REACT_WORK)

    async def keep_typing():
        try:
            while True:
                await asyncio.sleep(4)
                await send_typing()
        except asyncio.CancelledError:
            return

    typing_task = asyncio.create_task(keep_typing())
    # The turn row names the message it answers (see _TURN_MSG): a menu tap's
    # message is the bot's own menu, not something a person wrote.
    answering = None if is_menu_tap else _TURN_MSG.set((chat.id, msg.message_id))
    try:
        reply = await _claude_turn(
            chat.id, topic_id, project, agent, prompt, on_progress, prefs=prefs
        )
    except Exception:
        typing_task.cancel()
        try:
            await thinking.delete()
        except Exception:
            pass
        await _react(msg, REACT_ERR)
        if replay is not None:
            replay["outcome"] = "failed"
        raise
    finally:
        if answering is not None:
            _TURN_MSG.reset(answering)
    typing_task.cancel()
    failed = reply.startswith(("❌", "⏱"))
    if replay is not None:
        replay["outcome"] = "failed" if failed else "ok"
        if failed:
            # The replay loop retries, then posts one failure note: an engine
            # error per attempt would only be noise in the thread.
            try:
                await thinking.delete()
            except Exception:
                pass
            return
    owner_wrote = getattr(_profile(update), "key", None) == "owner"
    if group_proj is not None and failed and not owner_wrote:
        # A project group is people, not operators: the raw engine error (❌) or
        # timeout (⏱) means nothing to them. One plain line, the owner the details.
        where = f" topic {topic_id}" if topic_id else ""
        await _notify_owner(
            context_bot,
            f"⚠️ Errore del motore — progetto {project.key}, chat {chat.id}{where}\n"
            f"{chat_session.html_to_text(reply)[-500:]}",
        )
        reply = ENGINE_ERROR_GROUP_MSG
    reply, in_prod = _strip_prod_sentinel(reply)
    reply, wants_confirm = _strip_confirm_sentinel(reply)
    rating_kb = _rating_keyboard() if project.key == "comunity" else None
    await _finish_turn(
        thinking, reply, lambda: _send_html_chunks(msg, reply, reply_markup=rating_kb),
        reply_markup=rating_kb,
    )
    if failed:
        await _react(msg, REACT_ERR)
    else:
        await _react(msg, REACT_PROD if in_prod else REACT_DONE)
    _maybe_log_chat_turn(
        update, project, group_proj, prompt if is_menu_tap else None, reply, topic_name
    )
    if wants_confirm:
        await _send_confirm_buttons(
            context_bot, chat.id, topic_id, msg.message_thread_id
        )
    await _send_outbox(context_bot, chat.id, msg.message_thread_id, project.workspace)


async def _invoke_into_topic(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    topic_id: int,
    project: Project,
    agent: Agent,
    prompt: str,
) -> None:
    """Run a turn whose typing/thinking UI and reply land in an EXPLICIT topic,
    not wherever the trigmemberng update came from. Used for the opening ``seed``
    of a freshly spawned topic: the dashboard tap happens in the General, but
    the seed conversation must live in the new topic so the General stays clean
    and the session binds to the right (chat, topic) key."""
    bot = context.bot
    thread = topic_id or None  # Telegram wants None (not 0) for General/private

    async def send_typing():
        try:
            await bot.send_chat_action(
                chat_id, ChatAction.TYPING, message_thread_id=thread
            )
        except Exception:
            pass

    await send_typing()
    thinking = await bot.send_message(
        chat_id, "⏳ sto pensando…", message_thread_id=thread
    )
    on_progress = _throttled_progress(thinking)

    async def keep_typing():
        try:
            while True:
                await asyncio.sleep(4)
                await send_typing()
        except asyncio.CancelledError:
            return

    typing_task = asyncio.create_task(keep_typing())
    try:
        reply = await _claude_turn(
            chat_id, topic_id, project, agent, prompt, on_progress
        )
    finally:
        typing_task.cancel()
    reply, in_prod = _strip_prod_sentinel(reply)
    reply, wants_confirm = _strip_confirm_sentinel(reply)
    await _finish_turn(
        thinking, reply, lambda: _send_chunks_thread(bot, chat_id, thread, reply)
    )
    if in_prod:
        await _react_request_in_prod(bot, chat_id, topic_id)
    if wants_confirm:
        await _send_confirm_buttons(bot, chat_id, topic_id, thread)
    await _send_outbox(bot, chat_id, thread, project.workspace)


# --- Inline dashboard: keyboards ---------------------------------------------
HOME_TITLE = "📂 <b>Work o Personal?</b>"


def _kb_home(profile: Profile | None) -> InlineKeyboardMarkup:
    """Top-level group picker (Work / Personal). Shows only the groups that have
    at least one project visible to the profile, so the split composes with the
    per-user filtering (a member never sees an empty group)."""
    visible = visible_projects(profile) if profile is not None else []
    kinds_present = {p.kind for p in visible}
    rows = []
    for kind, label in projects.GROUPS:
        if kind in kinds_present:
            rows.append(
                [InlineKeyboardButton(label, callback_data=pack_cb("nav", "g", kind))]
            )
    return InlineKeyboardMarkup(rows)


def _kb_group(kind: str, profile: Profile | None) -> InlineKeyboardMarkup:
    """Projects of one group (2 per row), filtered to the profile, + back to the
    group picker."""
    rows, row = [], []
    visible = visible_projects(profile) if profile is not None else []
    for p in visible:
        if p.kind != kind:
            continue
        row.append(
            InlineKeyboardButton(p.label, callback_data=pack_cb("nav", "p", p.key))
        )
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append(
        [InlineKeyboardButton("↩︎ gruppi", callback_data=pack_cb("nav", "home"))]
    )
    return InlineKeyboardMarkup(rows)


def _kb_project(p: Project) -> InlineKeyboardMarkup:
    """New / resume / back for one project."""
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "➕ Nuova sessione",
                    callback_data=pack_cb("nav", "p", p.key, "new"),
                )
            ],
            [
                InlineKeyboardButton(
                    "↩️ Riprendi sessione",
                    callback_data=pack_cb("nav", "p", p.key, "res"),
                )
            ],
            [
                InlineKeyboardButton(
                    "↩︎ progetti", callback_data=pack_cb("nav", "g", p.kind)
                )
            ],
        ]
    )


def _kb_new_raw(p: Project, *, with_toggle: bool) -> InlineKeyboardMarkup:
    """Raw agent picker (advanced view), with human-readable labels."""
    rows = []
    for i, a in enumerate(agents_of(p)):
        rows.append(
            [
                InlineKeyboardButton(
                    agent_button_label(a),
                    callback_data=pack_cb("act", "new", p.key, str(i)),
                )
            ]
        )
    if with_toggle:
        rows.append(
            [
                InlineKeyboardButton(
                    "\U0001fa84 Vista semplice",
                    callback_data=pack_cb("nav", "p", p.key, "new"),
                )
            ]
        )
    rows.append(
        [InlineKeyboardButton("↩︎ indietro", callback_data=pack_cb("nav", "p", p.key))]
    )
    return InlineKeyboardMarkup(rows)


def _kb_new_functionalities(p: Project, *, show_toggle: bool) -> InlineKeyboardMarkup:
    """Friendly functionality picker (simple view)."""
    rows = []
    for f in functionalities_of(p):
        rows.append(
            [
                InlineKeyboardButton(
                    f.label, callback_data=pack_cb("act", "fnew", p.key, f.key)
                )
            ]
        )
    if show_toggle:
        rows.append(
            [
                InlineKeyboardButton(
                    "\U0001f527 Vista avanzata",
                    callback_data=pack_cb("nav", "p", p.key, "adv"),
                )
            ]
        )
    rows.append(
        [InlineKeyboardButton("↩︎ indietro", callback_data=pack_cb("nav", "p", p.key))]
    )
    return InlineKeyboardMarkup(rows)


def choose_new_view(has_func: bool, is_admin: bool, requested: str) -> str:
    """Which 'new session' view to render: ``'func'`` or ``'raw'``.

    - No functionalities file -> always ``'raw'`` (legacy: everyone sees agents).
    - ``'adv'`` requested by an admin -> ``'raw'``.
    - Otherwise -> ``'func'`` (non-admins cannot force ``'raw'``).
    """
    if not has_func:
        return "raw"
    if requested == "adv" and is_admin:
        return "raw"
    return "func"


def _raw_legend(p: Project) -> str:
    """Advanced-view header: one readable line per agent with its description."""
    lines = [f"{p.label} · <b>agenti</b> (avanzato)"]
    for a in agents_of(p):
        lines.append(f"{_h(agent_button_label(a))} — {_h(a.description)}")
    return "\n".join(lines)


def _new_view(
    p: Project, profile: Profile, requested: str = "new"
) -> tuple[str, InlineKeyboardMarkup]:
    """(text, keyboard) for the 'new session' step, honouring the view gating."""
    if choose_new_view(has_functionalities(p), profile.is_admin, requested) == "raw":
        return _raw_legend(p), _kb_new_raw(p, with_toggle=has_functionalities(p))
    return (
        f"{p.label} · cosa vuoi fare?",
        _kb_new_functionalities(p, show_toggle=profile.is_admin),
    )


async def _spawn_from_functionality(
    update: Update, context: ContextTypes.DEFAULT_TYPE, p: Project, f: Functionality
) -> None:
    """Open a topic for functionality ``f``: bind its primary agent and, if a
    ``seed`` is set, inject it as the opening turn. Companions live in the same
    workspace and are handled by the agent's own persona (no bot wiring).

    The seed is routed into the *spawned* topic (via ``_invoke_into_topic``),
    not wherever the button was tapped — so in a group the General stays clean
    and the seed conversation appears only in the new topic. In a private chat
    the topic is ``0`` and it behaves exactly like talking in the chat."""
    agent = resolve_agent(p, f.agent) or projects.GENERALE
    tid = await _spawn_topic(update, context, p, agent, resume_id=None, name=f.label)
    if f.seed and tid is not None:
        token = claude_runner.SESSION_READONLY.set(
            SESSION_GATE and not await _session_full(update)
        )
        try:
            await _invoke_into_topic(
                context, update.effective_chat.id, tid, p, agent, f.seed
            )
        finally:
            claude_runner.SESSION_READONLY.reset(token)


# --- Phase 2: resume cache + helpers -----------------------------------------
# (chat_id, session_id) -> SessionRef, so an act:res tap can map id -> ref
_RESUME_CACHE: dict[tuple[int, str], projects.SessionRef] = {}


def _rel_time(mtime: float) -> str:
    secs = max(0, int(time.time() - mtime))
    if secs < 3600:
        return f"{secs // 60}m"
    if secs < 86400:
        return f"{secs // 3600}h"
    return f"{secs // 86400}g"


def _kb_resume(project: Project, chat_id: int) -> InlineKeyboardMarkup:
    rows = []
    sessions = projects.sessions_of(project, limit=10)
    for s in sessions:
        _RESUME_CACHE[(chat_id, s.id)] = s
        tag = "PC" if s.source == "PC" else "TG"
        title = (s.title or s.id[:8])[:34]
        rows.append(
            [
                InlineKeyboardButton(
                    f"{tag} · {title} · {_rel_time(s.mtime)}",
                    callback_data=pack_cb("act", "res", project.key, s.id),
                )
            ]
        )
    if not rows:
        rows.append(
            [
                InlineKeyboardButton(
                    "nessuna sessione — ➕ Nuova",
                    callback_data=pack_cb("nav", "p", project.key, "new"),
                )
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(
                "↩︎ indietro", callback_data=pack_cb("nav", "p", project.key)
            )
        ]
    )
    return InlineKeyboardMarkup(rows)


async def _show_resume_list(q, project: Project) -> None:
    chat_id = q.message.chat.id
    await q.edit_message_text(
        f"{project.label} · ultime sessioni",
        parse_mode=ParseMode.HTML,
        reply_markup=_kb_resume(project, chat_id),
    )


async def _resume_into_topic(update, context, project_key, session_id) -> None:
    chat = update.effective_chat
    project = resolve_project(project_key)
    ref = _RESUME_CACHE.get((chat.id, session_id))
    if not project or not ref:
        await update.effective_message.reply_text("Sessione scaduta, riapri /progetti.")
        return
    rid = projects.prepare_resume(project, ref)
    agent = projects.resolve_agent(project, ref.agent) or projects.GENERALE
    name = (ref.title or "")[:24]
    await _spawn_topic(update, context, project, agent, resume_id=rid, name=name)


# --- Topic spawning ----------------------------------------------------------
async def _spawn_topic(
    update, context, project: Project, agent: Agent, *, resume_id, name
) -> int | None:
    """Create a forum topic bound to (project, agent) and welcome into it.

    In a private chat (no topics) bind the implicit topic 0 and talk inline.

    Returns the topic id the session is bound to (``0`` in a private chat / the
    new forum topic's id in a group), or ``None`` if the topic could not be
    created — so a caller can route a follow-up turn (e.g. a seed) into it.
    """
    chat = update.effective_chat
    msg = update.effective_message
    label = "Generale" if agent.name == "generale" else agent.name
    title = f"{project.emoji} {project.label.split(' ', 1)[-1]} · {label}"
    if name:
        title += f" · {name}"
    title = title[:128]
    if chat.type == "private":
        # No topics in private chat: bind the implicit topic 0 and talk inline.
        _bind_topic(chat.id, 0, project.key, agent.name)
        if resume_id:
            _set_session(chat.id, 0, resume_id)
        await msg.reply_text(
            f"🟢 ora parli con <b>{_h(title)}</b> qui. "
            "(Per schede parallele usa il gruppo-forum.)",
            parse_mode=ParseMode.HTML,
        )
        return 0
    try:
        topic = await context.bot.create_forum_topic(chat_id=chat.id, name=title)
    except Exception:
        logger.exception("create_forum_topic failed")
        await msg.reply_text(
            "❌ Non riesco a creare la scheda. Servono i <b>Topic</b> attivi e io "
            "<b>admin</b> con <i>Gestione topic</i>.",
            parse_mode=ParseMode.HTML,
        )
        return None
    tid = topic.message_thread_id
    _bind_topic(chat.id, tid, project.key, agent.name)
    _remember_topic_name(chat.id, tid, title)
    if resume_id:
        _set_session(chat.id, tid, resume_id)
    welcome = f"🟢 <b>{_h(title)}</b>\n" + (
        "Riprendo da dove eravamo — scrivimi pure."
        if resume_id
        else "Scrivimi in linguaggio naturale."
    )
    try:
        await context.bot.send_message(
            chat_id=chat.id,
            message_thread_id=tid,
            text=welcome,
            parse_mode=ParseMode.HTML,
        )
    except Exception:
        logger.exception("welcome into topic failed")
    await msg.reply_text(
        f"✅ creata la scheda <b>{_h(title)}</b>.", parse_mode=ParseMode.HTML
    )
    return tid


# --- Help --------------------------------------------------------------------
HELP_HTML = (
    "<b>🧭 Coordinator · guida</b>\n\n"
    "Un solo bot, tanti <b>progetti</b>; in ciascuno scegli con chi parli "
    "(💬 Generale o un agente). Ogni <b>scheda</b> (topic) è una conversazione a "
    "sé, con la sua memoria: più schede lavorano in parallelo.\n\n"
    "<code>/progetti</code> · dashboard dei progetti (o <code>/menu</code>)\n"
    "<code>/nuovo &lt;progetto&gt; [agente] [nome]</code> · nuova scheda "
    "(es. <code>/nuovo cosmos builder</code>)\n"
    "<code>/riprendi &lt;progetto&gt;</code> · riprendi una sessione\n"
    "<code>/modello [opus|sonnet|haiku] [low…max]</code> · fissa modello ed "
    "effort di questa scheda (<code>auto</code> per il default)\n"
    "<code>/compara [deepseek|claude|opencode…] su: &lt;richiesta&gt;</code> · "
    "stessa richiesta su piu' modelli in parallelo, per confrontarli\n"
    "<code>/reset</code> · dimentica il filo di questa scheda\n"
    "<code>/chiudi</code> · archivia questa scheda\n"
    "<code>/riorganizza</code> · il maggiordomo rivede subito lo schedule\n"
    "<code>/prepara</code> · il maggiordomo pre-esegue i task in arrivo\n"
    "<code>/pulisci</code> · cancella i messaggi vuoti o doppi del bot in questa chat (admin)\n"
    "<code>/azzera</code> · cancella tutti i messaggi dei gruppi (tranne ComUnity), con conferma (admin)\n"
    "<code>/help</code> · questa guida\n\n"
    "Dentro una scheda parli in linguaggio naturale; una <b>foto</b> va "
    "all'agente della scheda (per Toni: aggiorna la dispensa).\n"
    "Per un singolo messaggio puoi anteporre il modello: "
    "<code>opus/high: rifai il poller</code>."
)


# Guida per chi arriva dal gruppo e non amministra il Coordinator: elenca solo
# cio' che gli risponde davvero. Mostrargli /progetti o /nuovo sarebbe peggio
# che non dirgli niente — li proverebbe e otterrebbe silenzio, che e' come si
# comportano i comandi chiusi.
HELP_OSPITE_HTML = (
    "<b>🧭 Come si lavora da qui</b>\n\n"
    "Ogni <b>scheda</b> (topic) di questo gruppo e' un settore, e ha il suo "
    "referente. Scrivi nella scheda giusta, <b>in italiano normale</b>: non ci "
    "sono comandi da imparare e non serve saper programmare.\n\n"
    "Ogni scheda ha una <b>memoria sua</b>: dentro una scheda si prosegue un "
    "discorso, cambiando scheda si cambia argomento e interlocutore.\n\n"
    "<code>/reset</code> · dimentica il filo di questa scheda e riparte pulito\n"
    "<code>/modello [opus|sonnet|haiku] [low…max]</code> · quanto "
    "pensarci su, in questa scheda (<code>auto</code> torna al default)\n"
    "<code>/compara [deepseek|claude|opencode…] su: &lt;richiesta&gt;</code> · "
    "prova la stessa richiesta su piu' modelli in parallelo\n"
    "<code>/help</code> · questa guida\n\n"
    "Puoi mandare anche <b>foto</b>: vanno al referente della scheda.\n\n"
    "Le richieste tecniche finiscono in un <b>ramo con proposta di modifica</b> "
    "su GitHub, non dritte nell'app: c'e' sempre un passaggio in cui qualcuno "
    "guarda prima che diventi vero."
)

# --- Command handlers --------------------------------------------------------
def _group_menu_here(update: Update, context) -> tuple[str, list] | None:
    """(project_key, menu items) when this chat/topic is a project group with ``[[menu]]``.

    A room another bot owns keeps today's behaviour here: its menu is that
    bot's to show, and answering it from both bots is the double-reply bug.
    """
    found = _menu_binding_here(update)
    if found is None:
        return None
    project = resolve_project(found[0])
    if project is None:
        return None
    if _served_by_another_bot(project, _current_bot_name(context)) is not None:
        return None
    return found


async def _maybe_show_group_menu(update: Update, context) -> bool:
    """Show the group's button menu if it declares one. True when handled."""
    found = _group_menu_here(update, context)
    if found is None:
        return False
    if _profile_or_guest(update) is None:
        return True  # default deny, same silence as cmd_help
    # Opening the menu ends any sticky drill-down mode of this card.
    msg = update.effective_message
    _clear_mode(update.effective_chat.id, _topic_id(msg))
    await msg.reply_text(
        group_menu.MENU_TITLE, reply_markup=group_menu.build_keyboard(found[1])
    )
    return True


async def on_group_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """A tap on a group-menu button (``gm:<key>``)."""
    q = update.callback_query
    if q is None:
        return
    if _profile_or_guest(update) is None:
        await q.answer("Non autorizzato", show_alert=True)
        return
    chat, msg = update.effective_chat, update.effective_message
    found = _menu_binding_here(update) if chat is not None and msg else None
    item, arg = group_menu.resolve_arg(found[1], q.data or "") if found else (None, "")
    if item is None:
        await q.answer("Questo bottone non è più attivo: scrivi /menu", show_alert=False)
        return
    await q.answer()
    project_key = found[0]
    project = resolve_project(project_key)
    tg = manifest.telegram_of(project) if project is not None else None
    topic_id = _topic_id(msg)
    # Any tap ends the previous sticky mode; a drill-down selection sets a new one.
    _clear_mode(chat.id, topic_id)
    if tg is not None and tg.menu_resets_session:
        if _LAST_MENU_KEY.get((chat.id, topic_id)) != item.key:
            _clear_session(chat.id, topic_id)
            _LAST_MENU_KEY[(chat.id, topic_id)] = item.key
            _save_state()
    try:
        if item.source and project is not None:
            level = await _run_menu_source(project, item, "")
            if level is not None:
                await msg.reply_text(
                    _drill_title(item, level, ()),
                    reply_markup=_drill_keyboard(
                        project_key, item, level, (), 0, chat.id, topic_id
                    ),
                )
                return
        cap = None
        if item.cmd:
            caps = manifest.capabilities_of(project) if project is not None else []
            cap = next((c for c in caps if "/" + _tg_command(c.key) == item.cmd.lower()), None)
        if item.cmd and (arg or not item.hint):
            if cap is None:
                logger.warning("group menu %r: no capability for %s in %s",
                               item.key, item.cmd, project_key)
                await msg.reply_text(f"⚠️ {item.cmd} non è disponibile in questo gruppo.")
                return
            await _run_capability(update, project_key, cap.key, arg)
            return
        await msg.reply_text(group_menu.hint_text(item, cap.hint if cap else ""))
    finally:
        if tg is not None and tg.menu_autohide:
            try:
                await msg.delete()
            except Exception as exc:
                logger.debug("menu autohide: delete failed: %s", exc)


async def _run_menu_source(project: Project, item, path: str) -> dict | None:
    """Run a [[menu]] item's ``source`` for ``path``; the parsed level or None."""
    try:
        argv = group_menu.build_command(item.source, path)
        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=project.workspace,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            out, err = await asyncio.wait_for(
                proc.communicate(), timeout=group_menu.SOURCE_TIMEOUT_SEC
            )
        except TimeoutError:
            proc.kill()
            logger.warning("menu source %r of %s timed out", item.key, project.key)
            return None
        if proc.returncode != 0:
            logger.warning("menu source %r of %s exit=%s: %s", item.key, project.key,
                           proc.returncode, err.decode(errors="replace")[-300:])
            return None
        level = group_menu.parse_level(out.decode(errors="replace"))
        if level is None:
            logger.warning("menu source %r of %s: invalid JSON", item.key, project.key)
        return level
    except Exception as exc:
        logger.warning("menu source %r of %s failed: %s", item.key, project.key, exc)
        return None


def _drill_title(item, level: dict, trail: tuple) -> str:
    crumb = level.get("title") or group_menu.breadcrumb(n for _p, n in trail) or "scegli"
    return f"{item.label} — {crumb}"


def _drill_keyboard(
    project_key: str, item, level: dict, trail: tuple, page: int, chat_id: int, topic_id: int
):
    def register(payload: dict) -> str:
        return _DRILL_REGISTRY.register(
            dict(payload, project=project_key, menu_key=item.key,
                 all_label=level.get("all_label"), chat=chat_id, topic=topic_id)
        )

    return group_menu.build_drill_keyboard(level, page, trail, register)


DRILL_EXPIRED_MSG = "Menu scaduto, riapri /menu"


async def on_drill_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """A tap inside a drill-down menu (``gx:<id>``): navigate or select."""
    q = update.callback_query
    if q is None:
        return
    if _profile_or_guest(update) is None:
        await q.answer("Non autorizzato", show_alert=True)
        return
    data = q.data or ""
    payload = _DRILL_REGISTRY.get(data[len(group_menu.DRILL_PREFIX):])
    chat_here, msg_here = update.effective_chat, update.effective_message
    if (payload is not None and chat_here is not None and msg_here is not None
            and (payload.get("chat") != chat_here.id
                 or payload.get("topic") != _topic_id(msg_here))):
        payload = None  # an id minted for another chat/topic: never act on it
    found = _menu_binding_here(update)
    item = None
    if payload is not None and found is not None and found[0] == payload.get("project"):
        item = next((it for it in found[1] if it.key == payload.get("menu_key")), None)
    if item is None or not item.source:
        await q.answer(DRILL_EXPIRED_MSG, show_alert=False)
        return
    await q.answer()
    if payload.get("kind") == "noop":
        return
    chat, msg = update.effective_chat, update.effective_message
    project = resolve_project(payload["project"])
    if project is None:
        return
    trail = tuple(tuple(x) for x in payload.get("trail", ()))
    if payload.get("kind") == "nav":
        level = await _run_menu_source(project, item, trail[-1][0] if trail else "")
        if level is None:
            await msg.reply_text(
                "⚠️ Non riesco ad aprire questo elenco ora: riprova o scrivi /menu."
            )
            return
        try:
            await msg.edit_text(
                _drill_title(item, level, trail),
                reply_markup=_drill_keyboard(payload["project"], item, level, trail,
                                             int(payload.get("page", 0)),
                                             chat.id, _topic_id(msg)),
            )
        except Exception as exc:
            logger.debug("drill edit failed: %s", exc)
        return
    # Selection: a leaf, or "this whole level".
    names = [n for _p, n in trail]
    crumb = group_menu.breadcrumb(names)
    if payload.get("all") and not trail:
        crumb = group_menu.clean_label(payload.get("all_label") or "") or "tutto"
    path = trail[-1][0] if trail else ""
    try:
        await msg.edit_text(f"{item.label} — {crumb}")
    except Exception as exc:
        logger.debug("drill edit failed: %s", exc)
    topic_id = _topic_id(msg)
    tg = manifest.telegram_of(project)
    if tg is not None and tg.menu_resets_session:
        _clear_session(chat.id, topic_id)
        _LAST_MENU_KEY[(chat.id, topic_id)] = item.key
    _set_mode(chat.id, topic_id, {
        "project": project.key, "menu_key": item.key, "label": item.label,
        "cmd": item.cmd, "path": path, "breadcrumb": crumb,
    })
    caps = manifest.capabilities_of(project)
    cap = next((c for c in caps if "/" + _tg_command(c.key) == item.cmd.lower()), None)
    if cap is None:
        logger.warning("drill menu %r: no capability for %s in %s", item.key, item.cmd, project.key)
        await msg.reply_text(f"⚠️ {item.cmd} non è disponibile qui.")
        return
    await _run_capability(update, project.key, cap.key, crumb)


async def cmd_new(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/new: forget this card's session (and sticky mode), start a new conversation."""
    if _profile_or_guest(update) is None:
        return
    chat, msg = update.effective_chat, update.effective_message
    if chat is None or msg is None:
        return
    _maybe_autobind_dm(update)
    tid = _topic_id(msg)
    _clear_session(chat.id, tid)
    _clear_mode(chat.id, tid)
    await msg.reply_text(NEW_CONVERSATION_MSG)


ARGOMENTI_USAGE = (
    "Uso: /argomenti Nome 1 | Nome 2 | … — crea un argomento (topic) per ogni nome.\n"
    "Senza nomi uso l'elenco topics del manifesto del progetto, se c'è."
)


ARGOMENTI_PAUSE_SEC = 1.0


def _retry_after_seconds(exc) -> float:
    ra = getattr(exc, "retry_after", 1)
    return float(ra.total_seconds()) if hasattr(ra, "total_seconds") else float(ra)


async def _create_topic_with_retry(tg_bot, chat_id: int, name: str):
    """create_forum_topic, honouring one RetryAfter (flood control)."""
    from telegram.error import RetryAfter

    try:
        return await tg_bot.create_forum_topic(chat_id=chat_id, name=name)
    except RetryAfter as exc:
        await asyncio.sleep(_retry_after_seconds(exc) + 0.5)
        return await tg_bot.create_forum_topic(chat_id=chat_id, name=name)


def _tg_error_text(exc: Exception) -> str:
    """The cause in plain words: rights, flood, or Telegram's own message."""
    from telegram.error import Forbidden, RetryAfter

    text = str(exc) or type(exc).__name__
    low = text.lower()
    if isinstance(exc, RetryAfter):
        return f"Telegram chiede di aspettare {int(_retry_after_seconds(exc))} s, riprova dopo."
    if isinstance(exc, Forbidden) or "not enough rights" in low or "rights" in low:
        return "non ho i permessi: servo admin con «Gestione argomenti»."
    if "not a forum" in low or ("topics" in low and "disabled" in low):
        return "gli Argomenti non sono attivi in questo gruppo."
    return text


async def cmd_argomenti(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/argomenti a | b | c (admin): one forum topic per name, bound to the group's project."""
    chat, msg = update.effective_chat, update.effective_message
    if chat is None or msg is None or not _is_admin(update):
        return
    if chat.type == "private" or not getattr(chat, "is_forum", False):
        await msg.reply_text("Serve un gruppo con gli Argomenti (topic) attivi.")
        return
    bound = TOPIC_BINDING.get((chat.id, 0))
    grp = manifest.group_project_for_chat(chat.id)
    if bound is not None:
        project_key, agent_name = bound
    elif grp is not None:
        project_key, agent_name = grp[0], grp[1].agent
    else:
        await msg.reply_text("Questo gruppo non è legato a un progetto.")
        return
    project = resolve_project(project_key)
    if project is None:
        return
    args = (msg.text or "").partition(" ")[2]
    names = chat_session.parse_topic_names(args)
    if not names and _topic_id(msg):
        # Dentro un topic esistente: il bot ne impara il nome (#93).
        _learn_topic_name_from(msg)
        known = TOPIC_NAMES.get((chat.id, _topic_id(msg)))
        if known:
            await msg.reply_text(f"✅ Ho segnato questo topic come «{known}».")
        else:
            await msg.reply_text(
                "Non riesco a leggere il nome di questo topic: scrivi /argomenti "
                "con un messaggio di risposta, o rinomina il topic e riprova."
            )
        return
    if not names:
        tg = manifest.telegram_of(project)
        names = list(tg.topics) if tg is not None else []
    if not names:
        await msg.reply_text(ARGOMENTI_USAGE)
        return
    existing = {n.casefold() for (c, _t), n in TOPIC_NAMES.items() if c == chat.id}
    created, skipped = [], []
    for name in names:
        if name.casefold() in existing:
            skipped.append(name)
            continue
        if created:
            await asyncio.sleep(ARGOMENTI_PAUSE_SEC)  # Telegram flood limits
        try:
            topic = await _create_topic_with_retry(context.bot, chat.id, name)
        except Exception as exc:
            logger.exception("create_forum_topic %r failed", name)
            await msg.reply_text(f"❌ Non riesco a creare «{name}»: {_tg_error_text(exc)}")
            break
        tid = topic.message_thread_id
        _bind_topic(chat.id, tid, project_key, agent_name)
        _remember_topic_name(chat.id, tid, name)
        existing.add(name.casefold())
        created.append(name)
        try:
            await context.bot.send_message(
                chat_id=chat.id, message_thread_id=tid, text=chat_session.topic_welcome(name)
            )
        except Exception:
            logger.exception("welcome in topic %s failed", tid)
    parts = []
    if created:
        parts.append("✅ Creati: " + ", ".join(created))
    if skipped:
        parts.append("↩️ Già presenti: " + ", ".join(skipped))
    if parts:
        await msg.reply_text("\n".join(parts))


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    _maybe_autobind_dm(update)
    if await _maybe_show_group_menu(update, context):
        return
    if _profile_or_guest(update) is None:
        return
    msg = update.effective_message
    chat = update.effective_chat
    project, agent = _topic_binding(chat.id, _topic_id(msg)) if chat else (None, None)
    # Un ospite del gruppo non e' in PROFILES: la guida piena elencherebbe
    # comandi che, per lui, non rispondono.
    text = HELP_HTML if _profile(update) is not None else HELP_OSPITE_HTML
    if project is not None and agent is not None:
        label = "💬 Generale" if agent.name == "generale" else agent.name
        text += (
            f"\n\n🟢 Scheda attiva: <b>{_h(project.label)}</b> · "
            f"<b>{_h(label)}</b> — parlagli in linguaggio naturale."
        )
    await msg.reply_text(text, parse_mode=ParseMode.HTML)


async def cmd_progetti(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Open the inline dashboard (project picker)."""
    _maybe_autobind_dm(update)
    if await _maybe_show_group_menu(update, context):
        return
    profile = _profile(update)
    if profile is None:
        return
    await update.effective_message.reply_text(
        HOME_TITLE,
        parse_mode=ParseMode.HTML,
        reply_markup=_kb_home(profile),
    )


def _mk_kb(rows: list[list[tuple[str, str]]]) -> InlineKeyboardMarkup:
    """model_menu rows -> Telegram markup (pack_cb enforces the 64-byte cap)."""
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(label, callback_data=pack_cb(data)) for label, data in row] for row in rows]
    )


async def _edit_menu(q, text: str, rows) -> None:
    """Edit the menu in place; re-tapping the current page is not an error."""
    try:
        await q.edit_message_text(
            text, parse_mode=ParseMode.HTML, reply_markup=_mk_kb(rows) if rows else None
        )
    except Exception as exc:  # telegram.error.BadRequest "message is not modified"
        if "not modified" not in str(exc).lower():
            raise


def _opencode_fallback_models() -> list[str]:
    """Static OpenCode list for when `opencode models` cannot be read."""
    out: list[str] = []
    for m in [c.model for c in MODEL_CATALOG if c.backend == "opencode"] + [_default_opencode_model()]:
        if m.startswith("opencode/") and m not in out:
            out.append(m)
    return out


async def _opencode_catalog() -> tuple[list[str], list[str]]:
    """(principali, tutti): newest of each leading family, and the full list."""
    return await model_menu.opencode_catalog(claude_runner.OPENCODE_BIN, _opencode_fallback_models())


async def _opencode_model_list() -> list[str]:
    return (await _opencode_catalog())[1]


def _available_providers(project_key: str) -> list[model_menu.Provider]:
    return [
        p for p in model_menu.PROVIDERS
        if _choice_available(project_key, CATALOG_BY_ID[p.choice_id])
    ]


def _provider_of(prefs: model_prefs.Prefs) -> str | None:
    """Which level-1 provider a card's pick belongs to (for the check mark)."""
    choice = CATALOG_BY_ID.get(prefs.choice_id) if prefs.choice_id else None
    if choice is None:
        return None
    if choice.id == "claude-sub":
        return "csub"
    if choice.id == "claude-api":
        return "capi"
    if choice.id.startswith("deepseek"):
        return "ds"
    return "oc" if choice.backend == "opencode" else None


def _opencode_current(prefs: model_prefs.Prefs) -> str | None:
    choice = CATALOG_BY_ID.get(prefs.choice_id) if prefs.choice_id else None
    if choice is None or _provider_of(prefs) != "oc":
        return None
    return prefs.model or choice.model


def _prefs_choice_label(prefs: model_prefs.Prefs) -> str | None:
    """Label of the pinned pick, model included when the catalog entry alone
    doesn't say it (Claude + alias, generic OpenCode + live id)."""
    choice = CATALOG_BY_ID.get(prefs.choice_id) if prefs.choice_id else None
    if choice is None:
        return None
    if choice.id == "opencode":
        return f"OpenCode · {prefs.model or choice.model}"
    if choice.backend == "claude" and prefs.model:
        return f"{choice.label} · {prefs.model}"
    return choice.label


def _modello_text(prefs: model_prefs.Prefs, head: str | None = None) -> str:
    default_label = (
        claude_runner.OPENCODE_MODEL if claude_runner.AI_BACKEND == "opencode" else CLAUDE_MODEL
    )
    state = model_prefs.describe(
        prefs, default_model=default_label, choice_label=_prefs_choice_label(prefs)
    )
    lead = f"{head}\n\n" if head else ""
    return f"{lead}Questa scheda usa {state}\n\nScegli il provider:"


def _kb_modello_home(prefs: model_prefs.Prefs, project_key: str):
    return model_menu.kb_providers(_available_providers(project_key), _provider_of(prefs))


async def _handle_mdl_callback(update: Update, parts: list[str]) -> None:
    """Navigazione del menu /modello (#68): provider -> modello -> effort, tutto
    in place sullo stesso messaggio. Grammatica in model_menu. Aperto agli
    ospiti (stessa soglia di /modello): un membro del gruppo che non e' mappato
    deve poter scegliere il modello della propria scheda tanto quanto ci puo'
    scrivere dentro. Ogni scelta finale ricontrolla la disponibilita' lato
    server: la tastiera e' filtrata, ma un pulsante vecchio (o una chiave tolta
    dopo l'invio) deve restare bloccato."""
    q = update.callback_query
    chat = update.effective_chat
    tid = _topic_id(q.message)
    project_key = _project_key_for_topic(chat.id, tid)
    cur = _get_prefs(chat.id, tid)
    action = parts[1] if len(parts) > 1 else ""
    expired = "Scelta scaduta, riapri /modello."

    async def home(head: str | None = None) -> None:
        now = _get_prefs(chat.id, tid)
        await _edit_menu(q, _modello_text(now, head), _kb_modello_home(now, project_key))

    def provider(pid: str) -> model_menu.Provider | None:
        p = model_menu.PROVIDER_BY_ID.get(pid)
        if p is None:
            return None
        return p if _choice_available(project_key, CATALOG_BY_ID[p.choice_id]) else None

    if action == "home":
        await home()
        return
    if action == "auto":
        _set_prefs(chat.id, tid, model_prefs.Prefs())
        await home("↩︎ scheda tornata al default.")
        return

    if action == "pv" and len(parts) >= 3:
        prov = provider(parts[2])
        if prov is None:
            await home("Provider non disponibile qui.")
            return
        page = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else 0
        if prov.id in ("csub", "capi"):
            same = cur.choice_id == prov.choice_id
            rows = model_menu.kb_claude_models(prov.id, cur.model if same else None)
        elif prov.id == "ds":
            rows = model_menu.kb_deepseek(cur.choice_id)
        else:
            top, _all = await _opencode_catalog()
            rows = model_menu.kb_opencode_top(top, _opencode_current(cur))
        price = "\n<i>prezzi in $ per 1M token, input/output</i>" if prov.id == "oc" else ""
        await _edit_menu(q, f"<b>{_h(prov.label)}</b> · scegli il modello{price}", rows)
        return

    if action == "oca":
        if provider("oc") is None:
            await home("OpenCode non e' disponibile: manca OPENCODE_API_KEY.")
            return
        page = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0
        rows = model_menu.kb_opencode(
            await _opencode_model_list(),
            _opencode_current(cur),
            page,
            pager="mdl:oca",
            back=("⬅ principali", "mdl:pv:oc:0"),
        )
        await _edit_menu(
            q, "<b>OpenCode</b> · tutti i modelli (dal piu' recente)\n<i>prezzi in $ per 1M token, input/output</i>", rows
        )
        return

    if action in ("cm", "ce") and len(parts) >= 4:
        prov = provider(parts[2])
        alias = model_prefs.normalize_model(parts[3])
        if prov is None or prov.id not in ("csub", "capi") or alias is None:
            await home(expired)
            return
        if action == "cm":
            same = cur.choice_id == prov.choice_id and cur.model == alias
            await _edit_menu(
                q,
                f"<b>{_h(prov.label)} · {alias}</b> · scegli l'effort",
                model_menu.kb_efforts(prov.id, alias, cur.effort if same else None),
            )
            return
        raw_eff = parts[4] if len(parts) > 4 else "auto"
        effort = None if raw_eff == "auto" else model_prefs.normalize_effort(raw_eff)
        if raw_eff != "auto" and effort is None:
            await home(expired)
            return
        _set_prefs(chat.id, tid, model_prefs.Prefs(alias, effort, prov.choice_id))
        await home("✅ scheda aggiornata.")
        return

    if action == "set" and len(parts) >= 3:
        # Also the legacy flat-list button: messages sent before #68 keep working.
        choice = CATALOG_BY_ID.get(parts[2])
        if choice is None:
            await home(expired)
            return
        if not _choice_available(project_key, choice):
            missing = choice.key_env or "il login Claude"
            await home(f"{_h(choice.label)} non e' disponibile: manca {missing}.")
            return
        _set_prefs(chat.id, tid, model_prefs.Prefs(choice_id=choice.id))
        await home("✅ scheda aggiornata.")
        return

    if action == "oc" and len(parts) >= 3:
        if provider("oc") is None:
            await home("OpenCode non e' disponibile: manca OPENCODE_API_KEY.")
            return
        models = await _opencode_model_list()
        model = model_menu.resolve_hash(parts[2], models + _opencode_fallback_models())
        if model is None:
            await home(expired)
            return
        _set_prefs(chat.id, tid, model_prefs.Prefs(model=model, choice_id="opencode"))
        await home("✅ scheda aggiornata.")
        return

    await home(expired)


def _ocdefault_text(head: str | None = None) -> str:
    lead = f"{head}\n\n" if head else ""
    return (
        f"{lead}Modello OpenCode di default: <code>{_h(_default_opencode_model())}</code>\n"
        "(usato da /compara per l'alias «opencode»)\n\nScegli dai pulsanti:"
        "\n<i>prezzi in $ per 1M token, input/output</i>"
    )


async def _kb_ocdefault(page: int | None = None):
    """Principali by default; ``page`` opens the full paginated list."""
    if page is None:
        top, _all = await _opencode_catalog()
        return model_menu.kb_opencode_top(
            top,
            _default_opencode_model(),
            pick="mdl:od",
            more="mdl:oda:0",
            back=("↩︎ default da .env", "mdl:odauto"),
        )
    return model_menu.kb_opencode(
        await _opencode_model_list(),
        _default_opencode_model(),
        page,
        pick="mdl:od",
        pager="mdl:oda",
        back=("⬅ principali", "mdl:odp"),
    )


async def _handle_ocdefault_callback(update: Update, parts: list[str]) -> None:
    """Pulsanti di /opencodemodel: impostazione di deployment, solo admin
    (il chiamante ha gia' verificato il profilo)."""
    q = update.callback_query
    action = parts[1]
    if action == "odauto":
        _set_default_opencode_model(None)
        await _edit_menu(q, _ocdefault_text("↩︎ tornato al default da .env."), await _kb_ocdefault())
        return
    if action == "odp":
        await _edit_menu(q, _ocdefault_text(), await _kb_ocdefault())
        return
    if action == "oda":
        page = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0
        await _edit_menu(q, _ocdefault_text(), await _kb_ocdefault(page))
        return
    models = await _opencode_model_list()
    model = (
        model_menu.resolve_hash(parts[2], models + _opencode_fallback_models())
        if len(parts) > 2
        else None
    )
    if model is None:
        await _edit_menu(q, _ocdefault_text("Scelta scaduta."), await _kb_ocdefault())
        return
    _set_default_opencode_model(model)
    await _edit_menu(q, _ocdefault_text("✅ default aggiornato."), await _kb_ocdefault())


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Inline-keyboard router.

    callback_data grammar:
      ``nav:home`` | ``nav:p:<k>`` | ``nav:p:<k>:new`` | ``nav:p:<k>:adv`` | ``nav:p:<k>:res``
      ``act:new:<k>:<agent_idx>`` | ``act:fnew:<k>:<fkey>`` | ``act:res:<k>:<id>``
      ``mdl:...`` -- menu /modello, aperto agli ospiti (grammatica in model_menu);
      ``mdl:od|odp|oda|odauto`` -- /opencodemodel, solo admin.
    """
    q = update.callback_query
    parts = parse_cb(q.data or "")
    if parts[0] == "mdl" and parts[1:2] and parts[1] in ("od", "odp", "oda", "odauto"):
        if _profile(update) is None:
            await q.answer("Non autorizzato")
            return
        await q.answer()
        await _handle_ocdefault_callback(update, parts)
        return
    if parts[0] == "mdl":
        if _profile_or_guest(update) is None:
            await q.answer("Non autorizzato")
            return
        await q.answer()
        await _handle_mdl_callback(update, parts)
        return
    profile = _profile(update)
    if profile is None:
        await q.answer("Non autorizzato")
        return
    await q.answer()
    if parts[:2] == ["nav", "home"]:
        await q.edit_message_text(
            HOME_TITLE,
            parse_mode=ParseMode.HTML,
            reply_markup=_kb_home(profile),
        )
        return
    if parts[0] == "nav" and len(parts) >= 3 and parts[1] == "g":
        kind = parts[2]
        label = dict(projects.GROUPS).get(kind, kind)
        await q.edit_message_text(
            f"{label} · <b>progetti</b>",
            parse_mode=ParseMode.HTML,
            reply_markup=_kb_group(kind, profile),
        )
        return
    if parts[0] == "nav" and len(parts) >= 3 and parts[1] == "p":
        p = resolve_project(parts[2])
        if not p:
            await q.edit_message_text("Menù scaduto, riapri /progetti")
            return
        # Re-check server-side: buttons are filtered, but a forged callback must
        # still be blocked (default deny).
        if not can_access(profile, p.key):
            await q.edit_message_text(NO_ACCESS_MSG)
            return
        if len(parts) == 3:
            await q.edit_message_text(
                p.label, parse_mode=ParseMode.HTML, reply_markup=_kb_project(p)
            )
            return
        if parts[3] in ("new", "adv"):
            text, markup = _new_view(p, profile, requested=parts[3])
            await q.edit_message_text(
                text, parse_mode=ParseMode.HTML, reply_markup=markup
            )
            return
        if parts[3] == "res":
            await _show_resume_list(q, p)  # implemented in Phase 2
            return
    if parts[:2] == ["act", "new"]:
        p = resolve_project(parts[2])
        if not p:
            await q.edit_message_text("Menù scaduto, riapri /progetti.")
            return
        if not can_access(profile, p.key):
            await q.edit_message_text(NO_ACCESS_MSG)
            return
        agents = agents_of(p)
        try:
            agent = agents[int(parts[3])]
        except (ValueError, IndexError):
            agent = None
        if not agent:
            await q.edit_message_text("Menù scaduto, riapri /progetti.")
            return
        await _spawn_topic(update, context, p, agent, resume_id=None, name="")
        await q.edit_message_reply_markup(reply_markup=None)
        return
    if parts[:2] == ["act", "fnew"] and len(parts) >= 4:
        p = resolve_project(parts[2])
        if not p:
            await q.edit_message_text("Menù scaduto, riapri /progetti.")
            return
        if not can_access(profile, p.key):
            await q.edit_message_text(NO_ACCESS_MSG)
            return
        f = resolve_functionality(p, parts[3])
        if not f:
            text, markup = _new_view(p, profile)
            await q.edit_message_text(
                text, parse_mode=ParseMode.HTML, reply_markup=markup
            )
            return
        await _spawn_from_functionality(update, context, p, f)
        await q.edit_message_reply_markup(reply_markup=None)
        return
    if parts[:2] == ["act", "res"] and len(parts) >= 4:
        p = resolve_project(parts[2])
        if p and not can_access(profile, p.key):
            await q.edit_message_text(NO_ACCESS_MSG)
            return
        await _resume_into_topic(update, context, parts[2], parts[3])
        try:
            await q.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        return
    # Unknown / stale payload (e.g. a button from a previous bot version)
    await q.edit_message_text("Menù scaduto, riapri /progetti.")


async def cmd_nuovo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/nuovo <progetto> [agente] [nome] — agent given: spawn; else show picker."""
    profile = _profile(update)
    if profile is None:
        return
    msg = update.effective_message
    args = context.args or []
    if not args:
        await msg.reply_text("📂 Scegli un progetto:", reply_markup=_kb_home(profile))
        return
    project = resolve_project(args[0])
    if project is None:
        await msg.reply_text(
            "Progetto sconosciuto. Apri /progetti.", reply_markup=_kb_home(profile)
        )
        return
    if not can_access(profile, project.key):
        await msg.reply_text(NO_ACCESS_MSG)
        return
    if len(args) == 1:
        text, markup = _new_view(project, profile)
        await msg.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)
        return
    # arg[1]: prima prova una funzionalità (label human-readable), poi un agente.
    f = resolve_functionality(project, args[1])
    if f is not None:
        await _spawn_from_functionality(update, context, project, f)
        return
    # Percorso agente grezzo: ammesso solo agli admin quando il progetto è curato.
    if has_functionalities(project) and not profile.is_admin:
        text, markup = _new_view(project, profile)
        await msg.reply_text(
            "Scegli una funzionalità:", parse_mode=ParseMode.HTML, reply_markup=markup
        )
        return
    agent = resolve_agent(project, args[1])
    if agent is None:
        text, markup = _new_view(project, profile)
        await msg.reply_text(
            f"Voce sconosciuta per {project.label}.",
            parse_mode=ParseMode.HTML,
            reply_markup=markup,
        )
        return
    name = " ".join(args[2:]).strip()
    await _spawn_topic(update, context, project, agent, resume_id=None, name=name)


async def cmd_riprendi(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/riprendi <progetto> — choose a session to resume."""
    profile = _profile(update)
    if profile is None:
        return
    msg = update.effective_message
    project = resolve_project((context.args or [""])[0])
    if project is None:
        await msg.reply_text(
            "Uso: /riprendi <progetto>. Apri /progetti.", reply_markup=_kb_home(profile)
        )
        return
    if not can_access(profile, project.key):
        await msg.reply_text(NO_ACCESS_MSG)
        return
    await msg.reply_text(
        f"{project.label} · scegli una sessione:",
        parse_mode=ParseMode.HTML,
        reply_markup=_kb_resume(project, update.effective_chat.id),
    )


async def cmd_modello(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """``/modello`` (nessun argomento) mostra i pulsanti dei modelli disponibili;
    ``/modello opus max`` fissa modello/effort via testo (compatibilita');
    ``/modello auto`` riporta la scheda al default.

    Arguments are order-free and may be given as one token (``/modello max``) or
    with a slash (``/modello opus/max``), matching the inline prefix syntax so
    there is one thing to remember, not two."""
    if _profile_or_guest(update) is None:
        return
    chat = update.effective_chat
    msg = update.effective_message
    tid = _topic_id(msg)
    raw_args = context.args or []
    # Engine syntax (#91, Life#164): `/modello claude:owner/opus`,
    # `/modello opencode/deepseek/deepseek-v4-pro` = the `--forza` of this
    # project's bus wakes. Checked before the `/` split below eats it.
    if len(raw_args) == 1 and wake_routing.looks_like_force(raw_args[0]):
        await _modello_forza(update, raw_args[0])
        return
    args = [a for tok in raw_args for a in re.split(r"[/,]", tok) if a]

    if not args:
        cur = _get_prefs(chat.id, tid)
        project_key = _project_key_for_topic(chat.id, tid)
        await msg.reply_text(
            _modello_text(cur),
            parse_mode=ParseMode.HTML,
            reply_markup=_mk_kb(_kb_modello_home(cur, project_key)),
        )
        return

    if len(args) == 1 and args[0].lower() in ("auto", "default", "reset"):
        _set_prefs(chat.id, tid, model_prefs.Prefs())
        if _is_admin(update):
            project_key = _project_key_for_topic(chat.id, tid)
            if project_key and wake_routing.get_forza(project_key):
                wake_routing.set_forza(project_key, None)
        await msg.reply_text(
            f"↩︎ scheda tornata al default: {model_prefs.describe(model_prefs.Prefs(), default_model=CLAUDE_MODEL)}",
            parse_mode=ParseMode.HTML,
        )
        return

    model = effort = None
    unknown = []
    for tok in args:
        mo, ef = model_prefs.normalize_model(tok), model_prefs.normalize_effort(tok)
        if mo and model is None:
            model = mo
        elif ef and effort is None:
            effort = ef
        else:
            unknown.append(tok)
    if unknown:
        await msg.reply_text(
            f"Non riconosco: <b>{_h(', '.join(unknown))}</b>\n\n{model_prefs.known_tokens()}",
            parse_mode=ParseMode.HTML,
        )
        return

    # Partial update: /modello max keeps the model already pinned on this card.
    prefs = model_prefs.Prefs(model, effort).merged_over(_get_prefs(chat.id, tid))
    _set_prefs(chat.id, tid, prefs)
    await msg.reply_text(
        f"✅ scheda fissata su {model_prefs.describe(prefs, default_model=CLAUDE_MODEL)}",
        parse_mode=ParseMode.HTML,
    )


async def _modello_forza(update: Update, spec: str) -> None:
    """Pin ``mezzo:profilo/modello`` as the forced entry of the project's wakes
    (#91). Admin only: it decides what every wake of the project runs on. A
    claude entry also pins the card's model, so the group's turns agree."""
    msg = update.effective_message
    chat = update.effective_chat
    if not _is_admin(update):
        await msg.reply_text("Forzare il mezzo delle sveglie è riservato agli admin.")
        return
    tid = _topic_id(msg)
    project_key = _project_key_for_topic(chat.id, tid)
    if not project_key:
        await msg.reply_text("Questa scheda non è legata a un progetto: niente sveglie da forzare.")
        return
    try:
        entry = wake_routing.parse_force(spec)
    except ValueError as exc:
        await msg.reply_text(f"Non riconosco <b>{_h(spec)}</b>: {_h(exc)}", parse_mode=ParseMode.HTML)
        return
    wake_routing.set_forza(project_key, entry.spec())
    card = ""
    model = model_prefs.normalize_model(entry.modello) if entry.mezzo == "claude" else None
    if model:
        _set_prefs(chat.id, tid, model_prefs.Prefs(model).merged_over(_get_prefs(chat.id, tid)))
        card = f" La scheda passa a {_h(model)}."
    await msg.reply_text(
        f"✅ sveglie di <b>{_h(project_key)}</b> forzate su <code>{_h(entry.spec())}</code>"
        f" (nessun ripiego).{card} <code>/modello auto</code> torna alla coda dell'istanza.",
        parse_mode=ParseMode.HTML,
    )


async def cmd_opencodemodel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """``/opencodemodel`` mostra il modello OpenCode di default; con un
    argomento lo cambia (persistito, sopravvive a un restart). E' il modello
    che /compara usa per l'alias "opencode" e non tocca le scelte gia' fissate
    a un modello preciso (catalogo /modello, o ``opencode:<modello>`` esplicito
    in /compara). Impostazione di deployment, non per-scheda: riservata a chi
    amministra il Coordinator, come /notifiche."""
    if _profile(update) is None:
        return
    msg = update.effective_message
    args = context.args or []
    if not args:
        await msg.reply_text(
            _ocdefault_text(),
            parse_mode=ParseMode.HTML,
            reply_markup=_mk_kb(await _kb_ocdefault()),
        )
        return
    token = args[0].strip()
    if token.lower() in ("auto", "default", "reset"):
        _set_default_opencode_model(None)
        await msg.reply_text(
            f"\u21a9\ufe0e tornato al default da .env: <code>{_h(claude_runner.OPENCODE_MODEL)}</code>",
            parse_mode=ParseMode.HTML,
        )
        return
    _set_default_opencode_model(token)
    await msg.reply_text(
        f"\u2705 modello OpenCode di default fissato su <code>{_h(token)}</code>",
        parse_mode=ParseMode.HTML,
    )


async def cmd_compara(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """``/compara [modelli] su: <richiesta>`` lancia la stessa richiesta in
    parallelo su piu' modelli e risponde con un messaggio per modello (appena
    ciascuno finisce, non tutti insieme alla fine), cosi' si vede come risponde
    ognuno senza doverla riscrivere N volte.

    Senza lista modelli riconosciuta prima di ``su:`` (``/compara su: ...`` o
    anche solo ``/compara <richiesta>``) usa il trio di default: DeepSeek,
    Claude (abbonamento) e il modello OpenCode di default (vedi
    /opencodemodel). Aperto agli ospiti del gruppo, stessa soglia di /modello:
    e' una prova di scrittura, non un'azione amministrativa."""
    if _profile_or_guest(update) is None:
        return
    # Guests get the runs too, but read-only (#89). Set on this update's own task
    # context (one task per update), so it dies with the handler.
    claude_runner.SESSION_READONLY.set(SESSION_GATE and not await _session_full(update))
    chat = update.effective_chat
    msg = update.effective_message
    tid = _topic_id(msg)
    project, agent = _topic_binding(chat.id, tid)
    if project is None:
        await msg.reply_text(
            "Questa scheda non e' legata a un progetto: usa /nuovo o /riprendi prima di /compara."
        )
        return

    raw = (msg.text or "").partition(" ")[2].strip()
    m = re.search(r"\bsu\s*:\s*", raw, flags=re.IGNORECASE)
    if m:
        head, prompt = raw[: m.start()].strip(), raw[m.end() :].strip()
    else:
        head, prompt = "", raw
    if not prompt:
        await msg.reply_text(
            "Manca la richiesta: <code>/compara deepseek claude su: &lt;cosa vuoi&gt;</code>",
            parse_mode=ParseMode.HTML,
        )
        return
    tokens = [t for t in re.split(r"[\s,]+", head) if t] or ["deepseek", "claude", "opencode"]

    project_key = project.key
    entries: list[_CompareEntry] = []
    errors: list[str] = []
    for tok in tokens:
        r = _resolve_compara_token(tok, project_key)
        if isinstance(r, str):
            errors.append(r)
        else:
            entries.append(r)
    if errors:
        await msg.reply_text(
            "Non disponibili:\n" + "\n".join(f"\u2022 {_h(e)}" for e in errors),
            parse_mode=ParseMode.HTML,
        )
        return

    persona = agent.persona

    async def run_one(entry: _CompareEntry) -> None:
        sent = await msg.reply_text(
            f"\u23f3 <b>{_h(entry.label)}</b> in corso\u2026", parse_mode=ParseMode.HTML
        )
        t0 = time.monotonic()
        try:
            reply, _sid = await run_claude(
                prompt,
                workspace=project.workspace,
                persona=persona,
                resume_id=None,
                on_progress=None,
                extra_env=entry.extra_env,
                model=entry.model,
                effort=None,
                backend=entry.backend,
            )
        except Exception:
            logger.exception("compara: %s failed", entry.label)
            try:
                await sent.edit_text(
                    f"<b>{_h(entry.label)}</b>: \u274c errore, vedi log.",
                    parse_mode=ParseMode.HTML,
                )
            except Exception:
                pass
            return
        dt_ms = int((time.monotonic() - t0) * 1000)
        text = f"<b>{_h(entry.label)}</b> \u00b7 {dt_ms}ms\n\n{reply}"
        await _finish_turn(sent, text, lambda: _send_html_chunks(msg, text))

    await asyncio.gather(*(run_one(e) for e in entries))


async def cmd_chiudi(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Archive the current topic and drop its binding/session.

    In a project that configures ``[team]``, ``/chiudi <id>`` is the Task 14
    wrapper (close the ClickUp task); no argument keeps the archive meaning.
    """
    if await _maybe_team_chiudi(update, context):
        return
    if not _allowed(update):
        return
    msg = update.effective_message
    chat = update.effective_chat
    tid = _topic_id(msg)
    if tid == 0:
        await msg.reply_text(
            "Qui non c'è una scheda da chiudere (è il Generale o una chat diretta)."
        )
        return
    _clear_session(chat.id, tid)
    if TOPIC_BINDING.pop((chat.id, tid), None) is not None:
        _save_state()
    await msg.reply_text("🗄 scheda archiviata.")
    try:
        await context.bot.close_forum_topic(chat_id=chat.id, message_thread_id=tid)
    except Exception:
        logger.exception("close_forum_topic failed")


async def cmd_reset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if _profile_or_guest(update) is None:
        return
    chat = update.effective_chat
    msg = update.effective_message
    tid = _topic_id(msg)
    arg = " ".join(context.args or []).strip().lower()
    if arg == "all":
        keys = [k for k in list(SESSIONS) if k[0] == chat.id]
        for key in keys:
            SESSIONS.pop(key, None)
        _save_state()
        await msg.reply_text(f"🧹 ok, dimenticati {len(keys)} fili conversazionali.")
        return
    had = _clear_session(chat.id, tid)
    had = _clear_mode(chat.id, tid) or had
    project, agent = _topic_binding(chat.id, tid)
    if project and agent:
        label = "💬 Generale" if agent.name == "generale" else agent.name
        label = f"{project.label} · {label}"
    else:
        label = "questa scheda"
    await msg.reply_text(
        (
            f"🧹 ok, riparto da capo con <b>{_h(label)}</b>."
            if had
            else "Niente da resettare."
        ),
        parse_mode=ParseMode.HTML,
    )


# --- Pin helper and /pin command ---------------------------------------------


def _pin_text_for(project) -> str | None:
    import manifest

    tg = manifest.telegram_of(project)
    return tg.pin if (tg and tg.pin) else None


async def _send_and_pin(bot, chat_id: int, text: str) -> bool:
    """Send a message to a chat and pin it. Returns True if pinned, False if the pin failed."""
    sent = await bot.send_message(
        chat_id, text, parse_mode=ParseMode.HTML, disable_web_page_preview=True
    )
    try:
        await bot.pin_chat_message(chat_id, sent.message_id, disable_notification=True)
        return True
    except Exception:
        logger.exception("pin failed for chat %s", chat_id)
        return False


async def cmd_pin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/pin <project> — pin the project's [telegram].pin message in its group (admin)."""
    msg = update.effective_message
    if not msg:
        return
    if not _is_admin(update):
        await msg.reply_text(NO_ACCESS_MSG)
        return
    token = (msg.text or "").partition(" ")[2].strip()
    project = resolve_project(token)
    if project is None:
        await msg.reply_text("Uso: /pin <progetto>")
        return
    import manifest

    tg = manifest.telegram_of(project)
    text = _pin_text_for(project)
    if not tg or tg.group_chat_id is None or not text:
        await msg.reply_text("Il progetto non ha gruppo o pin nel manifesto.")
        return
    pinned = await _send_and_pin(update.get_bot(), tg.group_chat_id, text)
    if pinned:
        await msg.reply_text("📌 Pin aggiornato.")
    else:
        await msg.reply_text(
            "Messaggio inviato, ma il pin è fallito (controlla i permessi del bot)."
        )


async def cmd_handoff(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/handoff <device> [dominio] — move Telegram polling ownership to a device.

    Without args, reports the current ownership status (main + domains). With a
    domain (e.g. ``/handoff server comunity``) only that bot's ladder moves.
    Applies the flag locally and forwards it to the peers (best-effort: they
    converge at their next probe anyway).
    """
    msg = update.effective_message
    if not msg or not _is_admin(update):
        return
    manager = _OWNERSHIP.get("manager")
    own_cfg = _OWNERSHIP.get("config")
    if manager is None:
        await msg.reply_text("Ownership dinamica non attiva su questa istanza.")
        return
    domains: dict = _OWNERSHIP.get("domains") or {}
    args = [a.strip().lower() for a in (context.args or [])]
    target = args[0] if args else ""
    if not target:
        lines = []
        for label, m in [("principale", manager), *domains.items()]:
            st = m.status()
            lines.append(
                f"<b>{html_mod.escape(label)}</b> — {html_mod.escape(st['device'])} "
                f"(prio {st['priority']}): polling "
                f"{'sì' if st['is_polling'] else 'no'}, vuole l'ownership "
                f"{'sì' if st['wants_ownership'] else 'no'}"
            )
        lines.append("Uso: /handoff &lt;device&gt; [dominio] — es. /handoff minipc")
        await msg.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)
        return
    domain = args[1] if len(args) > 1 else ""
    if domain:
        manager = domains.get(domain)
        if manager is None:
            known = ", ".join(sorted(domains)) or "nessuno"
            await msg.reply_text(f"Dominio sconosciuto. Disponibili: {known}.")
            return
        own_cfg = manager.cfg
    await manager.apply_handoff(target)
    peer_st = await ownership.forward_handoff(own_cfg, target)
    note = (
        ""
        if peer_st
        else "\n⚠️ Peer non raggiungibile: si allineerà al prossimo probe."
    )
    where = f" per <b>{html_mod.escape(domain)}</b>" if domain else ""
    await msg.reply_text(
        f"🔁 Ownership Telegram{where} verso <b>{html_mod.escape(target)}</b>.{note}",
        parse_mode=ParseMode.HTML,
    )


# --- Message / photo handlers ------------------------------------------------
def _current_bot_name(context) -> str:
    """Which of our tokens is handling this update.

    ``handle_message`` is not the Coordinator's alone: every node bot delegates
    to it for an already-bound chat, so "us" is whoever is running right now, not
    a constant. ``_BOTS`` is the only place that knows the mapping; an unknown
    object falls back to the default bot, which is what every pre-multi-bot
    caller assumed anyway.
    """
    me = getattr(context, "bot", None)
    if me is not None:
        for name, obj in _BOTS.items():
            if obj is me:
                return name
    return bot_registry.DEFAULT_BOT


def _served_by_another_bot(
    project: "projects.Project", me: str = bot_registry.DEFAULT_BOT
) -> str | None:
    """The name of the dedicated bot that owns this project, if it is not us.

    ``me`` is what makes "another" mean anything. Without it the owner bot read
    its own name here and stood down in favour of itself: writing to
    @home_life_ai_bot in DM got silence plus one log line, ``proj=homeresearcher
    -> bot 'home' owns it``, written by 'home' about 'home'. The default keeps
    every existing caller on today's meaning — the Coordinator asking.

    The registration-time filter (``moved_chats``) can only silence chats a
    manifest actually DECLARES. A node with a dedicated bot but no
    ``group_chat_id`` — it binds itself when the first message lands — slips
    through it, and since every one of our tokens has privacy mode off, both the
    Coordinator and that node's bot would answer. Same double-processing bug,
    reached by the runtime-binding path instead of the seeded one.

    So the rule lives here too, where it holds regardless of how the binding was
    made: one room, one bot. Registration-time filtering stays — it is cheaper and
    it keeps the handler list honest — but it is no longer the only guard.

    Standing down is only correct while the owner can actually answer. A manifest
    that names a bot which is *not a member of the declared chat* used to silence
    us into a room where nobody else was listening — the declaration reads like a
    handover but is really a mute button. ``_ABSENT_OWNERS`` holds the pairs
    startup positively confirmed absent, and those never take the room from us.
    A check that could not be made is deliberately NOT treated as absent: a
    transient Telegram error at boot must not flip routing for the whole run.
    """
    tg = manifest.telegram_of(project)
    if tg is None or tg.bot == bot_registry.DEFAULT_BOT or tg.bot == me:
        return None
    # An unknown name means the declaration was ignored and the project really is
    # still ours; answering is correct. Mirrors _reassigned_projects().
    if tg.bot not in bot_registry.configured_names():
        return None
    if tg.group_chat_id is not None and (tg.bot, tg.group_chat_id) in _ABSENT_OWNERS:
        return None
    return tg.bot


def _node_label(node: str) -> str:
    """Human name of a node key, for the "lo passo a …" acknowledgement."""
    for p in projects.get_projects():
        if p.key.lower() == node.lower():
            return p.label.split(" ", 1)[-1]
    return node[:1].upper() + node[1:]


async def _route_dm_reply(update, context, profile: Profile, entry: dict, text: str) -> None:
    """Send a private reply back to the node whose notification asked for it (#55).

    Bus first; if the bus refuses (the node has no agent there, the bus is
    down), every admin gets the reply in DM instead. Either way the person is
    told where the answer went, so they never need to send it twice, and the
    reply lands in the ``dm_replies`` ledger (``GET /api/dm-replies``).
    """
    msg = update.effective_message
    chat = update.effective_chat
    node = entry["node"]
    label = _node_label(node)
    now = _local_now()
    body = dm_replies.forward_body(entry, profile.label, text)
    rec = {
        "ts": now.isoformat(timespec="seconds"),
        "node": node,
        "key": entry.get("key"),
        "from_profile": profile.key,
        "chat": chat.id,
        "msg_id": msg.message_id,
        "text": text,
    }
    try:
        agent = hub.bus_agent_for_node(node)
        conv = await hub.forward_dm_reply(
            agent,
            f"Risposta di {profile.label}: {entry.get('title') or entry.get('key')}",
            body,
            {"kind": "dm_reply", "notification": entry.get("key"),
             "from_profile": profile.key, "telegram_chat": chat.id,
             "telegram_msg_id": msg.message_id},
        )
        rec.update(routed="bus", agent=agent, conversation=conv.get("id"))
        logger.info("dm reply chat=%s -> bus agent %s (conv %s)", chat.id, agent, conv.get("id"))
    except Exception as exc:
        reason = f"{type(exc).__name__}: {exc}"[:200]
        logger.warning("dm reply chat=%s -> %s not routable on bus: %s", chat.id, node, reason)
        rec.update(routed="admin", error=reason)
        alert = (
            f"⚠️ <b>{_h(profile.label)}</b> ha risposto in privato alla notifica "
            f"<code>{_h(entry.get('key'))}</code> di <b>{_h(label)}</b>, ma il bus non "
            f"l'ha accettata ({_h(reason)}). Passala tu al nodo:\n\n{_h(text)}"
        )
        delivered = 0
        for uid in profiles.admin_user_ids():
            try:
                await context.bot.send_message(chat_id=uid, text=alert, parse_mode=ParseMode.HTML)
                delivered += 1
            except Exception:
                logger.exception("dm reply admin alert to %s failed", uid)
        rec["admins_alerted"] = delivered
    dm_replies.mark_forwarded(chat.id, now)
    dm_replies.log_reply(rec)
    await msg.reply_text(f"📨 Ricevuto, lo passo a {label}.")


def _visible_instances(profile) -> list[str]:
    """The instances registered here that ``profile`` reaches (admins: all)."""
    import instances

    try:
        known = instances.known_ids()
    except instances.InstanceError:
        known = []
    if profile is None:
        return []
    if profile.is_admin:
        return known
    allowed = set(profiles.instances_of(profile))
    return [i for i in known if i in allowed]


def _dm_nodes_by_instance() -> dict[str, list[dm_routing.Node]]:
    """``{instance: [(key, label, (slug, *aliases))]}`` from the registry and the graph."""
    nodes = _routing_index()
    out: dict[str, list[dm_routing.Node]] = {}
    for p in list_projects():
        for iid in instance_routing.instances_of_node(instance_routing.project_node(p), nodes):
            out.setdefault(iid, []).append(
                (p.key, p.label, tuple(x for x in (p.slug, *p.aliases) if x)))
    return out


def _bind_dm(chat_id: int, project: Project, instance_id: str | None) -> Agent:
    """Bind the DM to ``project`` (of ``instance_id``) and return the agent it talks to."""
    tg = manifest.telegram_of(project)
    agent = resolve_agent(project, tg.agent if tg else "generale") or projects.GENERALE
    _bind_topic(chat_id, 0, project.key, agent.name)
    node = instance_routing.project_node(project)
    if instance_id and instance_routing.needs_explicit(node, _routing_index()):
        TOPIC_INSTANCE[(chat_id, 0)] = instance_id
    else:
        TOPIC_INSTANCE.pop((chat_id, 0), None)
    _save_state()
    return agent


def _dm_buttons(decision: dm_routing.Decision) -> InlineKeyboardMarkup:
    """Instances first; inside one instance, its nodes (labels from the registry)."""
    if decision.kind == "ask_instance":
        rows = [[InlineKeyboardButton(f"\U0001f3db {i}", callback_data=f"{dm_routing.CB}:i:{i}")]
                for i in decision.options]
        return InlineKeyboardMarkup(rows)
    labels = {k: lbl for k, lbl, _ in _dm_nodes_by_instance().get(decision.instance or "", [])}
    keys = list(decision.options)[:24]
    rows = [[InlineKeyboardButton(labels.get(k, k),
                                  callback_data=f"{dm_routing.CB}:n:{decision.instance}:{k}")
             for k in keys[i:i + 2]] for i in range(0, len(keys), 2)]
    return InlineKeyboardMarkup(rows or [[InlineKeyboardButton("—", callback_data="noop")]])


async def _route_dm(update: Update, profile, text: str) -> bool:
    """Instance -> node for a DM (#83, F2). True when the message must stop here."""
    chat, msg = update.effective_chat, update.effective_message
    import instances

    bound = TOPIC_BINDING.get((chat.id, 0))
    decision = dm_routing.decide(
        text,
        bound=bound is not None,
        bound_instance=_binding_instance(chat.id, 0) if bound else None,
        visible=_visible_instances(profile),
        all_instances=instances.known_ids(),
        nodes_by_instance=_dm_nodes_by_instance(),
    )
    if decision.kind == "keep":
        return False
    if decision.kind == "denied":
        await msg.reply_text(f"Non hai accesso all'istanza {decision.instance}.")
        return True
    if decision.kind == "route":
        project = resolve_project(decision.project_key)
        if project is not None:
            _bind_dm(chat.id, project, decision.instance)
            logger.info("dm %s -> %s/%s", chat.id, decision.instance, project.key)
            return False
    if bound is None and not _visible_instances(profile):
        return False  # nothing to choose from: today's "non legata" answer follows
    _park_pending((chat.id, 0), update)
    domanda = ("In quale istanza?" if decision.kind == "ask_instance"
               else f"Istanza {decision.instance}: quale nodo?")
    await msg.reply_text(domanda, reply_markup=_dm_buttons(decision))
    return True


async def on_dm_route_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """A button of the DM question: open an instance, or bind a node and replay."""
    q = update.callback_query
    choice = dm_routing.read_callback(q.data or "")
    if choice is None:
        return
    utente = getattr(q, "from_user", None)
    profile = profile_for(utente.id) if utente is not None else None
    kind, iid, key = choice
    if not instance_acl.authorize(profile, iid) or iid not in _visible_instances(profile):
        await q.answer("Non hai accesso a questa istanza.", show_alert=True)
        return
    if kind == "i":
        nodes = [k for k, _, _ in _dm_nodes_by_instance().get(iid, [])]
        await q.answer()
        await q.edit_message_text(
            f"Istanza {iid}: quale nodo?",
            reply_markup=_dm_buttons(dm_routing.Decision("ask_node", instance=iid,
                                                         options=tuple(nodes))))
        return
    project = resolve_project(key)
    if project is None or not can_access(profile, project.key):
        await q.answer("Nodo non raggiungibile.", show_alert=True)
        return
    chat_id = q.message.chat.id
    _bind_dm(chat_id, project, iid)
    await q.answer("Ok.")
    await q.edit_message_text(f"\u2705 {iid} \u2192 {project.label}")
    parked = _PENDING_ROUTE.pop((chat_id, 0), None)
    if parked is not None:
        await handle_message(parked, context)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    chat = update.effective_chat
    if msg is None or chat is None or msg.text is None:
        return
    profile = _profile_or_guest(update)
    if profile is None:
        logger.warning(
            "blocked user=%s text=%r",
            update.effective_user.id if update.effective_user else None,
            msg.text[:80],
        )
        return
    if _is_stale(msg):
        return
    text = msg.text.strip()
    if not text:
        return
    if _is_admin(update) and await _notif_text_step(update, text):
        _mark_handled(msg, "notifiche")
        return
    # #55: a private message after a node asked this person something is the
    # answer, and belongs to that node — not to whatever the chat is bound to.
    if chat.type == "private":
        entry = dm_replies.pending_for(chat.id, _local_now())
        if entry is not None:
            await _route_dm_reply(update, context, profile, entry, text)
            _mark_handled(msg, "risposta-a-nodo")
            return
    # Hub: a reply typed inside a 🛰 conversation topic goes to the bus, not to claude.
    if update.effective_chat and update.effective_message:
        thread = getattr(update.effective_message, "message_thread_id", None) or 0
        try:
            if await hub.handle_topic_reply(update.effective_chat.id, thread, text):
                _mark_handled(msg, "bus")
                return
        except Exception:
            logger.exception("hub topic reply failed")
    tid = _topic_id(msg)
    if chat.type == "private":
        _maybe_autobind_dm(update)
        if await _route_dm(update, profile, text):
            _mark_handled(msg, "scelta-istanza")
            return
    project, agent = _topic_binding(chat.id, tid)
    if project is None:
        # Named on purpose: from the logs alone, a bot sitting in a group nobody
        # wired up looked exactly like a bot that was never added to it. The
        # chat id is also the one thing needed to wire it up, and there was no
        # way to read it back out of a running deployment.
        logger.info("chat=%s topic=%s: nessun progetto legato (chat non configurata)",
                    chat.id, tid)
        await msg.reply_text(
            "Questa scheda non è legata a un progetto. Apri /progetti.",
            reply_markup=_kb_home(profile),
        )
        return
    if (owner := _served_by_another_bot(project, _current_bot_name(context))) is not None:
        # Silent on purpose: the owning bot is answering this very message.
        logger.info("chat=%s topic=%s proj=%s -> bot %r owns it", chat.id, tid, project.key, owner)
        return
    if not can_access(profile, project.key):
        await msg.reply_text(NO_ACCESS_MSG)
        return
    # Il testo grezzo nel diario del nodo PRIMA di far partire il modello: e'
    # l'unico momento in cui e' ancora garantito che esista. Sta QUI, e non su
    # uno dei gestori a monte, perche' questa e' la strada che percorre ogni
    # messaggio umano di ogni bot: registrarlo piu' su significa registrarne
    # solo una parte. Il 2026-09-08 stava solo sul gestore del bot extra e i
    # messaggi del gruppo, che arrivano per di qua, non finivano da nessuna
    # parte — con `pubblica` gia' al posto giusto (in `_claude_turn`), che e'
    # esattamente la meta' di un cablaggio.
    _registra_indicazione(update, project, text)
    # Intent routing: a strong keyword match to a specialised agent overrides the
    # topic-bound default (which stays as fallback on no match / ties).
    routed = resolve_agent_by_intent(project, text, agent)
    if routed.name != agent.name:
        logger.info(
            "intent-route chat=%s topic=%s %s->%s",
            chat.id,
            tid,
            agent.name,
            routed.name,
        )
        agent = routed
    logger.info(
        "chat=%s topic=%s proj=%s agent=%s prompt=%r",
        chat.id,
        tid,
        project.key,
        agent.name,
        text[:120],
    )
    if _access_line(update) is not None and hub.is_dispatch_prefix(text):
        grant = _LIFE_GRANTS.get((update.effective_user.id, _binding_instance(chat.id, tid)))
        if grant is None or not life_access.role_allows(grant.role, "publish"):
            await msg.reply_text("🔒 Portare in prod richiede il livello publish.")
            return
    handled, conv = await hub.maybe_dispatch_prefix(project, text)
    if handled:
        await msg.reply_text(
            f"\U0001f680 Dispatch a prod (conv #{conv.get('id')}).",
            parse_mode=ParseMode.HTML,
        )
        _mark_handled(msg, "dispatch")
        return
    if await _maybe_remote_turn(update, project, text):
        _mark_handled(msg, "esecutore-remoto")
        return
    prefix = "\n".join(x for x in (
        _access_line(update),
        _assistant_line(update),
        chat_session.reply_to_line(getattr(msg, "reply_to_message", None)),
    ) if x) or None
    await _invoke_and_reply(update, project, agent, text, context_prefix=prefix)


def _instance_ladder(instance_id: str) -> tuple[str, ...]:
    """The executors ladder of an instance, as bus suffixes, if it declares one."""
    import instances
    import remote_turn

    inst = instances.get(instance_id)
    if inst is None:
        return ()
    cfg = instances.instance_config(inst)
    devices = cfg.get("devices")
    return remote_turn.ladder_suffixes(
        cfg.get("executors") or [], devices if isinstance(devices, dict) else None)


async def _maybe_remote_turn(update: Update, project: Project, text: str) -> bool:
    """Hand the message to the node's own device when it is not hosted here (#83, F3).

    Local nodes are untouched. A node no executor on the bus claims also stays on
    today's path: the remote route is taken only when the bus names who runs it.
    """
    import remote_turn

    node = instance_routing.project_node(project)
    local = node in _node_index()
    chat, msg = update.effective_chat, update.effective_message
    tid = _topic_id(msg)
    iid = _binding_instance(chat.id, tid)
    if iid is None:
        return False
    ladder = _instance_ladder(iid)
    # A local node stays here unless the ladder ranks another device first (conv 723):
    # then this device is the reserve and runs it only while that one is down.
    if local and not ladder:
        return False
    import instances

    tenant = instances.channels_of(instances.get(iid)).bus_tenant
    if not tenant:
        return False
    try:
        executor = await hub.remote_executor(tenant, node, ladder)
    except Exception:
        logger.exception("remote executor lookup failed for %s/%s", iid, node)
        return False
    if executor is None:
        return False
    if local and not remote_turn.outranks(executor, hub.local_suffix(), ladder):
        return False
    try:
        conv_id = await hub.send_remote_turn(tenant, executor.name, text, chat_id=chat.id,
                                             topic_id=tid, label=project.label)
    except Exception:
        logger.exception("remote turn to %s failed", executor.name)
        await msg.reply_text(f"\u274c Non riesco a passare il messaggio a {executor.name}.")
        return True
    logger.info("remote turn chat=%s topic=%s %s/%s -> %s (conv #%s)",
                chat.id, tid, iid, node, executor.name, conv_id)
    coda = "" if executor.listening else " \u2014 non è in ascolto ora: lo riceve appena torna"
    await msg.reply_text(f"\U0001f6f0 Passato a {executor.name} ({executor.device}){coda}.")
    return True


# A Telegram album (several photos sent together) is delivered as N separate
# updates that share a ``media_group_id``. Debounce them into ONE agent turn:
# otherwise the (chat, topic) session is hit with N serialized turns that pile
# up behind the first and hang. ``_album_buffer`` accumulates an album's photos
# until ``ALBUM_DEBOUNCE_SEC`` passes with no new photo, then fires once.
ALBUM_DEBOUNCE_SEC = 2.5
_album_buffer: dict[tuple[int, str], dict] = {}


def _ocr_image(path: str) -> str:
    """Best-effort OCR of a photo, so an agent whose model has no vision still
    gets readable content instead of just a file path. Not every backend this
    bot can run (see AI_BACKEND / MODEL_CATALOG in claude_runner.py) can see
    an image: a Telegram user sent a photo, the reply said "non supporto le
    immagini", and only then did the agent try OCR on its own (07/09/2026).
    Running OCR up front and handing the text over means every backend gets
    something readable by default, with no per-model vision detection needed.
    Never raises: a missing tesseract binary, a corrupt file, or a photo with
    no text at all all just yield an empty string, same as no OCR ran."""
    try:
        import pytesseract
        from PIL import Image

        with Image.open(path) as img:
            text = pytesseract.image_to_string(img, lang="ita+eng")
        return text.strip()
    except Exception:
        logger.debug("OCR fallita per %s", path, exc_info=True)
        return ""


def _photo_prompt(paths: list[str], caption: str, ocr_texts: list[str] | None = None) -> str:
    """Build the ingest prompt for one or more photos sent together.

    ``ocr_texts`` (parallel to ``paths``, from ``_ocr_image``) is appended as
    plain text when non-empty; a photo with no extractable text (or OCR that
    failed) changes nothing, so this is invisible when there is nothing to add.
    """
    caption = (caption or "").strip()
    ocr_texts = ocr_texts or []
    if len(paths) == 1:
        head = f"[CMD:/dispensa] {paths[0]} {caption}".strip()
        ocr = (ocr_texts[0] if ocr_texts else "").strip()
        return f"{head}\n\nTesto estratto dalla foto (OCR):\n{ocr}" if ocr else head
    listed = "\n".join(paths)
    head = f"[CMD:/dispensa] album di {len(paths)} foto inviate insieme:\n{listed}"
    body = f"{head}\nDidascalia: {caption}" if caption else head
    blocks = [f"{p}:\n{t.strip()}" for p, t in zip(paths, ocr_texts) if t and t.strip()]
    if blocks:
        body += "\n\nTesto estratto (OCR) per foto:\n" + "\n\n".join(blocks)
    return body


# A file must never be written into a workspace that holds no repo. Compose
# mounts every un-cloned node from the SHARED `.ws-placeholder`, read-write: a
# photo sent to a project this device does not have would be saved — happily,
# with no error — into Coordinator/.ws-placeholder/raw/telegram, mixed in with
# every other absent project. The live registry already drops those nodes
# (life_registry_client._servable); the static fallback does not, so the write
# path itself has to refuse. Loud beats silent-and-elsewhere.
WORKSPACE_MISSING_MSG = (
    "\u26a0\ufe0f Il progetto <b>{label}</b> non e' clonato su questo device: "
    "non ho un posto dove salvare il file. Mandalo dal device che lo ospita."
)


def _workspace_ready(project: Project) -> bool:
    """Does this project's workspace actually hold its repo on this device?"""
    return life_registry_client._has_checkout(project.workspace)


async def _download_photo(
    msg, chat_id: int, project: Project, agent: Agent, tid: int
) -> str:
    """Save the highest-resolution photo into the project's photo dir.

    Defaults to ``raw/telegram``; a project whose vault keeps originals elsewhere
    sets ``[telegram] photo_dir`` in its coordinator.toml (e.g. ``vault/raw``).
    """
    tg = manifest.load(project).telegram
    subdir = (tg.photo_dir if tg and tg.photo_dir else None) or "raw/telegram"
    photo_dir = f"{project.workspace}/{subdir}"
    os.makedirs(photo_dir, exist_ok=True)
    photo = msg.photo[-1]  # highest resolution
    stamp = _local_now().strftime("%Y-%m-%d_%H%M%S")
    path = os.path.join(photo_dir, f"{stamp}_{chat_id}_{msg.message_id}.jpg")
    tg_file = await photo.get_file()
    await tg_file.download_to_drive(path)
    logger.info(
        "photo saved chat=%s topic=%s proj=%s agent=%s path=%s",
        chat_id,
        tid,
        project.key,
        agent.name,
        path,
    )
    return path


# Photos already ingested, by (chat_id, message_id): an edit that adds the
# caption arrives as a second update with the same message_id (#63). Bounded
# FIFO -- an edit comes seconds or minutes later, never hundreds of photos on.
_PHOTOS_SEEN: "collections.OrderedDict[tuple[int, int], dict]" = collections.OrderedDict()
_PHOTOS_SEEN_MAX = 512


def _photo_caption_followup(path: str, caption: str) -> str:
    """Prompt for a caption added later to a photo that was already ingested."""
    return (
        f"{caption}\n\n(Didascalia aggiunta modificando la foto gia' inviata: {path}. "
        "E' la stessa foto del turno precedente, non una nuova: prosegui da li', "
        "senza saltare le conferme gia' chieste.)"
    )


async def _flush_album(key: tuple[int, str]) -> None:
    """After the debounce window, run ONE agent turn for the whole album."""
    try:
        await asyncio.sleep(ALBUM_DEBOUNCE_SEC)
    except asyncio.CancelledError:
        return  # a newer photo of the same album rescheduled the flush
    acc = _album_buffer.pop(key, None)
    if not acc or not acc["paths"]:
        return
    caption = " ".join(acc["captions"]).strip()
    prompt = _photo_prompt(acc["paths"], caption, acc.get("ocr", []))
    await _invoke_and_reply(acc["update"], acc["project"], acc["agent"], prompt)


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Save the largest photo and forward it to the topic's agent as an ingest.

    A single photo runs immediately; an album (``media_group_id``) is debounced
    so all its photos collapse into one turn (see ``_album_buffer``)."""
    msg = update.effective_message
    chat = update.effective_chat
    if not msg or not chat or not msg.photo:
        return
    profile = _profile_or_guest(update)
    if profile is None:
        logger.warning(
            "blocked photo user=%s",
            update.effective_user.id if update.effective_user else None,
        )
        return
    if _is_stale(msg):
        return
    tid = _topic_id(msg)
    project, agent = _topic_binding(chat.id, tid)
    if project is None:
        # Named on purpose: from the logs alone, a bot sitting in a group nobody
        # wired up looked exactly like a bot that was never added to it. The
        # chat id is also the one thing needed to wire it up, and there was no
        # way to read it back out of a running deployment.
        logger.info("chat=%s topic=%s: nessun progetto legato (chat non configurata)",
                    chat.id, tid)
        await msg.reply_text(
            "Questa scheda non è legata a un progetto. Apri /progetti.",
            reply_markup=_kb_home(profile),
        )
        return
    if (owner := _served_by_another_bot(project, _current_bot_name(context))) is not None:
        logger.info("photo chat=%s topic=%s proj=%s -> bot %r owns it", chat.id, tid, project.key, owner)
        return
    if not can_access(profile, project.key):
        await msg.reply_text(NO_ACCESS_MSG)
        return

    # A command in the caption (e.g. /segnala with a screenshot) is a command,
    # not an ingest: CommandHandler never sees captions.
    cap = _caption_capability(update, project)
    if cap is not None:
        _mark_handled(msg, "capability")
        await _run_capability(update, project.key, cap.key)
        return

    if not _workspace_ready(project):
        logger.warning(
            "foto scartata chat=%s topic=%s proj=%s: %s non ha il repo su questo device",
            chat.id, tid, project.key, project.workspace,
        )
        await msg.reply_text(
            WORKSPACE_MISSING_MSG.format(label=project.label), parse_mode=ParseMode.HTML
        )
        return
    seen_key = (chat.id, msg.message_id)
    seen = _PHOTOS_SEEN.get(seen_key)
    if seen is not None:
        # Same message_id again (#63): Telegram sends an edit when the caption
        # is added after the photo. Re-ingesting it was a second turn on the
        # same receipt; the caption is now a follow-up on the photo already
        # saved, queued behind its turn by the card lock.
        caption = (msg.caption or "").strip()
        if getattr(update, "edited_message", None) is None or not caption:
            return
        try:
            await asyncio.wait_for(seen["ready"].wait(), timeout=60)
        except asyncio.TimeoutError:
            pass
        if not seen["path"]:
            return  # the original download failed: nothing to refer to
        await _invoke_and_reply(update, project, agent, _photo_caption_followup(seen["path"], caption))
        return
    seen = {"path": None, "ready": asyncio.Event()}
    _PHOTOS_SEEN[seen_key] = seen
    while len(_PHOTOS_SEEN) > _PHOTOS_SEEN_MAX:
        _PHOTOS_SEEN.popitem(last=False)
    try:
        path = await _download_photo(msg, chat.id, project, agent, tid)
        seen["path"] = path
    finally:
        seen["ready"].set()
    ocr = await asyncio.to_thread(_ocr_image, path)
    caption = (msg.caption or "").strip()
    mgid = getattr(msg, "media_group_id", None)

    # Lone photo: run now.
    if not mgid:
        await _invoke_and_reply(update, project, agent, _photo_prompt([path], caption, [ocr]))
        return

    # Album: accumulate, then (re)arm the debounced flush.
    key = (chat.id, mgid)
    acc = _album_buffer.get(key)
    if acc is None:
        acc = {
            "paths": [],
            "ocr": [],
            "captions": [],
            "update": update,  # first photo anchors the reply
            "project": project,
            "agent": agent,
            "task": None,
        }
        _album_buffer[key] = acc
    acc["paths"].append(path)
    acc["ocr"].append(ocr)
    if caption:
        acc["captions"].append(caption)
    if acc["task"] is not None:
        acc["task"].cancel()
    acc["task"] = asyncio.create_task(_flush_album(key))


# --- Voice notes -> brain queue (issue #56) -----------------------------------
VOICE_EMPTY_MSG = "🎙 Non ho sentito parole in questo vocale: niente in coda."
VOICE_FAILED_MSG = "⚠️ Non sono riuscito a trascrivere il vocale. Riprova, o scrivilo."
VOICE_NO_NODE_MSG = (
    "🎙 «{text}»\n\n⚠️ Il nodo <b>{node}</b> non è nel registro: non l'ho messo in coda."
)
VOICE_MISSING_MSG = (
    "🎙 «{text}»\n\n⚠️ Nessuna istanza Life collegata al bot e <b>{label}</b> non è "
    "clonato su questo device: non l'ho messo in coda. Copia il testo e dallo a una "
    "sessione di quel nodo."
)
VOICE_QUEUED_MSG = (
    "🎙 «{text}»\n\n📌 In coda per il brain di <b>{istanza}</b> (nodo <b>{label}</b>)."
)
VOICE_QUEUED_LOCAL_MSG = (
    "🎙 «{text}»\n\n📌 In coda per il brain di <b>{istanza}</b> (nodo <b>{label}</b>), "
    "ma solo su questo device: il push non è riuscito, partirà col prossimo."
)
VOICE_QUEUED_NODE_MSG = (
    "🎙 «{text}»\n\n📌 In coda nel nodo <b>{label}</b>: nessuna istanza Life è "
    "collegata al bot (LIFE_INSTANCE_DIR), quindi la revisione generale non la vede."
)

# Swappable in tests: the real one loads faster-whisper.
_transcribe = voice_inbox.whisper_transcribe


async def _voice_to_text(msg) -> str:
    """Download the note to a temp file, transcribe it off the loop, delete it."""
    import tempfile

    fd, path = tempfile.mkstemp(prefix="voice_", suffix=".ogg")
    os.close(fd)
    try:
        tg_file = await msg.voice.get_file()
        await tg_file.download_to_drive(path)
        return await asyncio.to_thread(_transcribe, path)
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


async def _voice_answer(msg, text: str, how: str) -> None:
    """Reply to a voice note (HTML) and record that it was answered.

    A replayed note says it is late and quotes the original: in a busy group an
    answer to a voice note from three hours ago means nothing without both.
    """
    kwargs: dict = {"parse_mode": ParseMode.HTML}
    if _REPLAY.get() is not None:
        text = f"{REPLAY_LATE_LINE}\n{text}"
        kwargs["do_quote"] = True
    await msg.reply_text(text, **kwargs)
    _mark_handled(msg, how)


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """A voice note becomes a row in the Life instance's brain queue (see voice_inbox).

    Group: the node the room (or topic) is bound to. Direct chat: the node named
    in the first words ("Casa, ..."), else ``VOICE_DEFAULT_NODE`` (``life``).
    The reply always carries the transcription, so a misheard word is seen at
    once — and so the idea survives even when it could not be queued.
    """
    msg = update.effective_message
    chat = update.effective_chat
    if not msg or not chat or not getattr(msg, "voice", None):
        return
    # A lesson group (manifest audio_command) wants the recording itself, not
    # an idea in the brain queue. Every other chat keeps the path below.
    if getattr(chat, "type", "") != "private" and audio_command_for(chat.id):
        await handle_project_audio(update, context)
        return
    profile = _profile_or_guest(update)
    if profile is None:
        logger.warning(
            "blocked voice user=%s",
            update.effective_user.id if update.effective_user else None,
        )
        return
    if _is_stale(msg):
        return

    private = getattr(chat, "type", "") == "private"
    project = None
    if not private:
        tid = _topic_id(msg)
        project, _agent = _topic_binding(chat.id, tid)
        if project is None:
            logger.info("voice chat=%s topic=%s: nessun progetto legato", chat.id, tid)
            await msg.reply_text("Questa scheda non è legata a un progetto. Apri /progetti.")
            return
        if (owner := _served_by_another_bot(project, _current_bot_name(context))) is not None:
            logger.info("voice chat=%s proj=%s -> bot %r owns it", chat.id, project.key, owner)
            return
        if not can_access(profile, project.key):
            await msg.reply_text(NO_ACCESS_MSG)
            return

    try:
        text = await _voice_to_text(msg)
    except Exception:
        logger.exception("voice: trascrizione fallita chat=%s msg=%s", chat.id, msg.message_id)
        replay = _REPLAY.get()
        if replay is not None:
            replay["outcome"] = "failed"  # the replay loop retries, then gives up once
            return
        await msg.reply_text(VOICE_FAILED_MSG)
        return
    if not text:
        await _voice_answer(msg, VOICE_EMPTY_MSG, "vocale-vuoto")
        return

    await _queue_idea(msg, chat, profile, project, private, text)


def _legacy_instance_id() -> str | None:
    import instances

    return instances.legacy_instance_id()


def _idea_instance(chat_id: int, topic_id: int, project, private: bool) -> str | None:
    """Which instance's queue a voice note / ``/ricorda`` goes to (#78).

    A room answers with its own instance (the same derivation as the access
    gate: explicit choice, instance forum, graph). A private chat has no room:
    the named node's instance if the graph gives exactly one, else the device's
    default. ``None`` only when nothing at all is declared.
    """
    if not private:
        return _binding_instance(chat_id, topic_id)
    found = instance_routing.instances_of_node(
        instance_routing.project_node(project), _routing_index()
    )
    if len(found) == 1:
        return found[0]
    import instances

    return instances.default_instance_id()


async def _queue_idea(msg, chat, profile, project, private: bool, text: str,
                      *, icon: str = "🎙", suffix: str = "",
                      marker: str = voice_inbox.MARKER) -> None:
    """Resolve the node for ``text`` and append it to the brain queue.

    Shared by voice notes and ``/ricorda`` (typed): same node naming rule
    ("Casa, ..." in a private chat), same queue, same replies. ``icon`` replaces the
    microphone in the replies; ``suffix`` tags the ``_mark_handled`` outcome.
    """
    async def _answer(body: str, how: str) -> None:
        await _voice_answer(msg, body.replace("🎙", icon, 1), how + suffix)

    if private:
        names = voice_inbox.node_names(
            list_projects(), voice_inbox.parse_aliases(os.environ.get(voice_inbox.ALIASES_ENV))
        )
        named, rest = voice_inbox.split_named_node(text, names)
        node = named or os.environ.get(voice_inbox.DEFAULT_NODE_ENV) or voice_inbox.DEFAULT_NODE
        if named and rest:
            text = rest
        project = resolve_project(node)
        shown = html_mod.escape(text)
        if project is None:
            await _answer(
                VOICE_NO_NODE_MSG.format(text=shown, node=html_mod.escape(node)),
                "vocale-senza-nodo",
            )
            return
        if not can_access(profile, project.key):
            await _answer(f"🎙 «{shown}»\n\n{NO_ACCESS_MSG}", "vocale-negato")
            return

    shown = html_mod.escape(text)
    label = html_mod.escape(project.label)
    nodo = voice_inbox.node_dir_name(project)
    session = f"telegram:{chat.id}:{msg.message_id}"
    # The queue belongs to the Life instance, not to the node (contract:
    # Life/docs/brain-coda-istanza.md) — and to the instance of THIS chat, not
    # to the one a bot happens to have mounted (#78, operator). The checkouts
    # are read-only in the container, so the host writes and pushes the row.
    iid = _idea_instance(chat.id, _topic_id(msg), project, private)
    if iid and claude_runner.host_exec_enabled():
        import instance_link

        res = await instance_link.queue_idea(iid, nodo, text, session, marker)
        if res.written:
            logger.info("voice queued instance=%s chat=%s proj=%s pushed=%s chars=%d",
                        iid, chat.id, project.key, res.pushed, len(text))
            template = VOICE_QUEUED_MSG if res.pushed else VOICE_QUEUED_LOCAL_MSG
            await _answer(
                template.format(text=shown, label=label, istanza=html_mod.escape(iid)),
                "vocale-in-coda",
            )
            return
        logger.warning("coda di %s non scritta dall'host (%s): ripiego", iid, res.detail)
    # The legacy single mount still serves a device without the host bridge —
    # but only for its own instance: an idea of another instance never lands
    # in the wrong brain, it falls back to the node's queue below.
    instance = voice_inbox.instance_dir()
    if instance is not None and iid not in (None, _legacy_instance_id()):
        logger.warning("vocale di %s: la coda montata è di %s, non la uso",
                       iid, _legacy_instance_id())
        instance = None
    if instance is not None:
        path = voice_inbox.append_idea(instance, nodo, text, session, marker=marker)
        pushed = await asyncio.to_thread(voice_inbox.publish, instance, nodo)
        logger.info("voice queued chat=%s proj=%s path=%s pushed=%s chars=%d",
                    chat.id, project.key, path, pushed, len(text))
        template = VOICE_QUEUED_MSG if pushed else VOICE_QUEUED_LOCAL_MSG
        await _answer(
            template.format(text=shown, label=label,
                            istanza=html_mod.escape(voice_inbox.instance_name(instance))),
            "vocale-in-coda",
        )
        return
    if not _workspace_ready(project):
        logger.warning(
            "vocale non accodato proj=%s: nessuna istanza e %s non ha il repo qui",
            project.key, project.workspace,
        )
        await _answer(
            VOICE_MISSING_MSG.format(text=shown, label=label), "vocale-non-accodato"
        )
        return
    path = voice_inbox.append_idea(project.workspace, nodo, text, session, marker=marker)
    logger.warning("voice queued in the NODE (no instance) chat=%s proj=%s path=%s",
                   chat.id, project.key, path)
    await _answer(
        VOICE_QUEUED_NODE_MSG.format(text=shown, label=label), "vocale-in-coda-nodo"
    )


async def cmd_ricorda(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """``/ricorda [Nodo,] testo``: long-term memory by typing (Open Human, #80).

    The typed twin of a voice note: same queue, same node rule, same replies
    (with a note icon). Whatever lands there is reviewed by the digest before it
    becomes a brain note, so nothing is written to the brain unreviewed.
    """
    msg = update.effective_message
    chat = update.effective_chat
    if not msg or not chat:
        return
    profile = _profile_or_guest(update)
    if profile is None:
        return
    text = " ".join(context.args or []).strip()
    if not text:
        await msg.reply_text(
            "Uso: /ricorda testo da ricordare\nIn privato puoi aprire col nome del "
            "nodo: /ricorda Casa, compra le lampadine"
        )
        return
    private = getattr(chat, "type", "") == "private"
    project = None
    if not private:
        project, _agent = _topic_binding(chat.id, _topic_id(msg))
        if project is None:
            await msg.reply_text("Questa scheda non è legata a un progetto. Apri /progetti.")
            return
        if not can_access(profile, project.key):
            await msg.reply_text(NO_ACCESS_MSG)
            return
    await _queue_idea(msg, chat, profile, project, private, text,
                      icon="📝", suffix="-testo", marker=voice_inbox.TEXT_MARKER)


# --- Travelers raw-ingest ----------------------------------------------------
def _to_local(d: dt.datetime) -> dt.datetime:
    """Render a Telegram (UTC) message datetime in the configured local zone."""
    try:
        from zoneinfo import ZoneInfo

        return d.astimezone(ZoneInfo(TZ_NAME))
    except Exception:
        return d


def _sender_name(msg) -> str:
    u = getattr(msg, "from_user", None)
    if u is None:
        return "sconosciuto"
    return u.first_name or u.username or str(u.id)


def _raw_attachment_name(when: dt.datetime, msg_id: int, name: str) -> str:
    """Stable, filesystem-safe attachment name: ``<date>_<id>_<name>``."""
    safe = re.sub(r'[<>:"|?*\\/\s]+', "_", name).strip("_") or "allegato"
    return f"{when:%Y-%m-%d}_{msg_id}_{safe}"


def _group_message_prompt(
    author: str,
    tg_id: int,
    text: str,
    rel: str | None,
    download_failed: bool,
    reply_to: str | None = None,
    access: str | None = None,
) -> str:
    """Build the workspace-agent prompt for a free-text group message, carrying
    the sender's numeric Telegram id so the agent can act per-user. ``reply_to``
    (see chat_session.reply_to_line) is the quoted message it answers; ``access``
    the role Life granted to a non-owner sender (#83)."""
    parts = [f"[CMD:/group-message] mittente: {author} (tg_id: {tg_id})."]
    if access:
        parts.append(access)
    if reply_to:
        parts.append(reply_to)
    if text:
        parts.append(f"Testo: {text}")
    if rel:
        parts.append(f"Allegato (leggilo e interpretalo): {rel}")
    elif download_failed:
        parts.append("(c'era un allegato ma non e' stato scaricato)")
    return " ".join(parts)


DOWNLOAD_RETRY_WAITS = (5.0, 20.0, 60.0)
_DOWNLOAD_WARNED_AT: dict[str, float] = {}
DOWNLOAD_WARN_EVERY_SEC = 600.0


async def _download_with_retry(media, dest: str) -> None:
    """get_file + download, ritentati con attesa crescente: un arrivo in blocco (decine di allegati dopo
    un riavvio) mette in coda il Bot API locale e le prime richieste vanno in timeout senza colpa del file."""
    last: Exception | None = None
    for attempt, wait in enumerate((0.0, *DOWNLOAD_RETRY_WAITS)):
        if wait:
            await asyncio.sleep(wait)
        try:
            tg_file = await media.get_file(read_timeout=LESSON_GET_FILE_TIMEOUT_S)
            await tg_file.download_to_drive(dest)
            return
        except BadRequest:
            raise  # troppo grande & co.: riprovare non serve
        except Exception as exc:
            last = exc
            logger.warning("download attempt %d failed (%s): %r", attempt + 1, dest, exc)
    assert last is not None
    raise last


async def _get_file_with_retry(media):
    """``get_file`` con la stessa attesa crescente di ``_download_with_retry`` (a un riavvio arrivano in blocco)."""
    last: Exception | None = None
    for attempt, wait in enumerate((0.0, *DOWNLOAD_RETRY_WAITS)):
        if wait:
            await asyncio.sleep(wait)
        try:
            return await media.get_file(read_timeout=LESSON_GET_FILE_TIMEOUT_S)
        except BadRequest:
            raise
        except Exception as exc:
            last = exc
            logger.warning("get_file attempt %d failed: %r", attempt + 1, exc)
    assert last is not None
    raise last


async def _warn_owner_download_failed(context, project: Project, name: str) -> None:
    """Un solo avviso a the owner ogni 10 minuti, anche se i file falliti sono decine."""
    now = time.monotonic()
    last = _DOWNLOAD_WARNED_AT.get(project.key)
    if last is not None and now - last < DOWNLOAD_WARN_EVERY_SEC:
        return
    _DOWNLOAD_WARNED_AT[project.key] = now
    tg_bot = getattr(context, "bot", None)
    if tg_bot is not None:
        await _notify_owner(tg_bot, f"⚠️ {project.key}: un allegato di a member non si e' scaricato ({name}); "
                                    "metto la reazione 😢 e non rispondo. Altri nei prossimi 10 minuti non vengono segnalati.")


async def handle_project_group_message(
    update: Update, context: ContextTypes.DEFAULT_TYPE, *, project_key: str
) -> None:
    """Generic project-group ingest: a trusted group bound to a project.

    Mirrors the Travelers blueprint without hardcoding: the whole group is trusted
    (no per-user gate), media is downloaded into <workspace>/.claude/inbox/telegram,
    and the message is handed to the project's group agent as [CMD:/group-message].
    """
    msg = update.effective_message
    chat = update.effective_chat
    if not msg or not chat:
        return
    if not (msg.text or msg.caption or msg.photo or msg.document):
        return
    project = resolve_project(project_key)
    if project is None:
        logger.warning("group message: project %r not in registry; skip", project_key)
        return
    import manifest

    tg = manifest.telegram_of(project)
    agent = resolve_agent(project, tg.agent if tg else "generale") or projects.GENERALE

    # Created only when there is actually a file to put in it: a text-only
    # message must not leave an empty .claude/inbox/telegram behind in every
    # workspace it touches (the placeholder included).
    inbox = f"{project.workspace}/.claude/inbox/telegram"
    author = _sender_name(msg)
    text = (msg.text or msg.caption or "").strip()
    rel = None
    download_failed = False
    media, suggested = None, None
    if msg.photo:
        media, suggested = msg.photo[-1], "foto.jpg"
    elif msg.document:
        media, suggested = msg.document, (msg.document.file_name or "documento")
    if media is not None and not _workspace_ready(project):
        # Same rule as handle_photo: no repo here, no place to put the file.
        logger.warning(
            "allegato scartato proj=%s: %s non ha il repo su questo device",
            project.key, project.workspace,
        )
        media, download_failed = None, True
    if media is not None:
        local_dt = _to_local(msg.date)
        name = _raw_attachment_name(local_dt, msg.message_id, suggested)
        try:
            os.makedirs(inbox, exist_ok=True)
            await _download_with_retry(media, f"{inbox}/{name}")
            rel = f".claude/inbox/telegram/{name}"
        except Exception:
            logger.exception("group message: download failed msg=%s", msg.message_id)
            download_failed = True
    if download_failed and media is not None and _workspace_ready(project):
        # Un allegato che non si scarica non e' un argomento di conversazione: all'agente non si da' un
        # prompt senza file (il 25/10 ne usci' «e' la quinta volta, smetti di rimandarlo» a a member, 9 volte).
        # Solo la reazione di errore e un avviso a the owner; a member non deve fare niente di diverso.
        await _react(msg, REACT_ERR)
        await _warn_owner_download_failed(context, project, name)
        return

    # Intent routing on pure-text messages; media is treated as ingest so it
    # stays with the group's bound agent.
    if media is None and text:
        routed = resolve_agent_by_intent(project, text, agent)
        if routed.name != agent.name:
            logger.info(
                "intent-route group proj=%s %s->%s",
                project.key,
                agent.name,
                routed.name,
            )
            agent = routed
    tg_id = getattr(getattr(msg, "from_user", None), "id", 0)
    prompt = _group_message_prompt(
        author, tg_id, text, rel, download_failed,
        reply_to=chat_session.reply_to_line(getattr(msg, "reply_to_message", None)),
        access=_access_line(update),
    )
    await _invoke_and_reply(update, project, agent, prompt)


# --- Lesson recordings -> the group's agent (a member) ---------------------------
# A group whose manifest declares ``[telegram] audio_command`` (AcademicAssistant:
# "/lezione") turns every recording into a turn of its agent. Every branch
# answers: a lesson sent into silence is a lesson lost.
LESSON_AUDIO_RECEIVED_MSG = (
    "🎙 Ricevuta! La trascrivo e ti mando gli appunti appena pronti "
    "(per una lezione lunga ci vuole circa un'ora)."
)
LESSON_AUDIO_TOO_BIG_MSG = (
    "😕 Questo file è troppo grande per arrivarmi. Prova a dividere la "
    "registrazione in due parti e mandale una dopo l'altra."
)
LESSON_AUDIO_FAILED_MSG = (
    "⚠️ Non sono riuscito a scaricare la registrazione. Riprova a mandarla tra poco."
)
LESSON_AUDIO_TRANSCRIBE_FAILED_MSG = (
    "😕 Non sono riuscita a trascrivere la registrazione. "
    "the owner è stato avvisato: riprova a mandarla più tardi."
)
LESSON_AUDIO_AGENT_FAILED_MSG = (
    "😕 Ho la trascrizione ma non sono riuscita a preparare gli appunti. "
    "the owner è stato avvisato: la riprendo appena possibile."
)


def audio_command_for(chat_id: int) -> str | None:
    """The lesson-audio command of the project bound to this group, if any."""
    return manifest.audio_command_for(chat_id)


def _lesson_media(msg) -> tuple[object, str] | None:
    """(telegram media object, suggested file name) of a recording, or None."""
    if getattr(msg, "voice", None):
        return msg.voice, "vocale.ogg"
    if getattr(msg, "audio", None):
        return msg.audio, (getattr(msg.audio, "file_name", None) or "audio")
    if getattr(msg, "video", None):
        return msg.video, (getattr(msg.video, "file_name", None) or "video.mp4")
    if getattr(msg, "video_note", None):
        return msg.video_note, "videonota.mp4"
    doc = getattr(msg, "document", None)
    mime = (getattr(doc, "mime_type", None) or "") if doc else ""
    if doc and (mime.startswith("audio/") or mime.startswith("video/")):
        return doc, (getattr(doc, "file_name", None) or "registrazione")
    return None


def _is_too_big(exc: Exception) -> bool:
    return isinstance(exc, BadRequest) and "too big" in str(exc).lower()


async def handle_project_audio(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Hand a recorded lesson to the group's agent. True when it was ours.

    False (nothing done, nothing said) when the chat declares no
    ``audio_command`` or the message carries no recording: the caller keeps
    today's path for every other project.
    """
    msg = update.effective_message
    chat = update.effective_chat
    if not msg or not chat:
        return False
    binding = manifest.audio_binding(chat.id)
    if binding is None:
        return False
    found = _lesson_media(msg)
    if found is None:
        return False
    project_key, command = binding
    media, suggested = found
    if getattr(update, "edited_message", None) is not None:
        # An edit (a retouched caption) is the same recording: on 2026-10-16 it
        # queued a lesson already transcribed for another hour of CPU.
        logger.info("lesson audio chat=%s msg=%s edited: ignored", chat.id, msg.message_id)
        return True
    # Before any slow step: the album's caption must be known by the time the
    # caption-less siblings (handled concurrently) look it up.
    _remember_album_caption(msg)

    profile = _profile_or_guest(update)
    if profile is None:
        logger.warning(
            "blocked lesson audio user=%s",
            update.effective_user.id if update.effective_user else None,
        )
        return True
    if _is_stale(msg):
        return True
    project = resolve_project(project_key)
    if project is None:
        logger.warning("lesson audio: project %r not in registry", project_key)
        await msg.reply_text(LESSON_AUDIO_FAILED_MSG)
        return True
    if (owner := _served_by_another_bot(project, _current_bot_name(context))) is not None:
        logger.info("lesson audio chat=%s proj=%s -> bot %r owns it", chat.id, project.key, owner)
        return True
    if not can_access(profile, project.key):
        await msg.reply_text(NO_ACCESS_MSG)
        return True
    if not _workspace_ready(project):
        logger.warning(
            "lesson audio dropped proj=%s: %s has no checkout on this device",
            project.key, project.workspace,
        )
        await msg.reply_text(LESSON_AUDIO_FAILED_MSG)
        return True

    # get_file first: it is where Telegram refuses a file above the bot download
    # limit, so a too-big recording is told so instead of "Ricevuta" + silence.
    try:
        # With the local Bot API server getFile returns only once the server has
        # pulled the whole file from Telegram: ~100 MB lessons blew PTB's 5 s default.
        tg_file = await _get_file_with_retry(media)
    except Exception as exc:
        if _is_too_big(exc):
            logger.warning("lesson audio too big chat=%s msg=%s: %s",
                           chat.id, msg.message_id, exc)
            await msg.reply_text(LESSON_AUDIO_TOO_BIG_MSG)
        else:
            logger.exception("lesson audio: get_file failed msg=%s", msg.message_id)
            await msg.reply_text(LESSON_AUDIO_FAILED_MSG)
        return True

    tg = manifest.telegram_of(project)
    queue_dir = tg.audio_queue if tg else None
    if queue_dir:
        # Una lezione al giorno sono molti audio: niente risposta a ognuno, solo la reazione «preso in carico».
        # Il resto lo fa la pipeline (reazione «sto trascrivendo», poi il messaggio nell'argomento del corso).
        await _react(msg, REACT_WORK)
    else:
        await msg.reply_text(LESSON_AUDIO_RECEIVED_MSG)
    inbox = f"{project.workspace}/.claude/inbox/telegram"
    name = _raw_attachment_name(_to_local(msg.date), msg.message_id, suggested)
    try:
        os.makedirs(inbox, exist_ok=True)
        await tg_file.download_to_drive(f"{inbox}/{name}")
    except Exception as exc:
        if _is_too_big(exc):
            logger.warning("lesson audio too big chat=%s msg=%s: %s",
                           chat.id, msg.message_id, exc)
            await msg.reply_text(LESSON_AUDIO_TOO_BIG_MSG)
        else:
            logger.exception("lesson audio: download failed msg=%s", msg.message_id)
            await msg.reply_text(LESSON_AUDIO_FAILED_MSG)
        return True
    rel = f".claude/inbox/telegram/{name}"

    if queue_dir:
        try:
            _enqueue_lesson(update, project, queue_dir, rel, name)
            # «Gestito» solo a file salvato e voce in coda: prima di allora un replay deve poterlo riprendere.
            _mark_handled(msg, "lezione-ricevuta")
        except Exception as exc:
            logger.exception("lesson audio: queue write failed proj=%s file=%s", project.key, rel)
            await msg.reply_text(LESSON_AUDIO_FAILED_MSG)
            await _notify_owner_of_preprocess_failure(
                update, context, project, rel, f"{type(exc).__name__}: {exc}",
                title="Lezione non messa in coda")
        return True

    _mark_handled(msg, "lezione-ricevuta")  # taken in charge: a replay would transcribe it twice
    agent = resolve_agent(project, tg.agent if tg else "generale") or projects.GENERALE
    template = tg.audio_preprocess if tg else None
    if template:
        # Transcription can take an hour: run it in the background so neither
        # the update loop nor the group's turn lock waits for it.
        logger.info("lesson audio chat=%s proj=%s file=%s -> preprocess", chat.id, project.key, rel)
        _spawn_background(
            _preprocess_then_invoke(update, context, project, agent, command, template,
                                    f"{inbox}/{name}", rel),
            name=f"audio-preprocess:{project.key}:{msg.message_id}",
        )
        return True
    logger.info("lesson audio chat=%s proj=%s file=%s", chat.id, project.key, rel)
    await _invoke_and_reply(update, project, agent, _lesson_prompt(command, msg, rel))
    return True


def _enqueue_lesson(update: Update, project: Project, queue_dir: str, rel: str, name: str) -> str:
    """Write ``<workspace>/<queue_dir>/<stem>.json`` for the project's night job; returns its path.

    Atomic (temp file + replace): the job on the other machine may list the
    queue at any moment and must never read half an entry.
    """
    msg = update.effective_message
    _learn_topic_name_from(msg)
    entry = {
        "audio": rel,
        "argomento": _lesson_topic(update.effective_chat.id, msg),
        "ricevuto": dt.datetime.now(dt.UTC).astimezone().isoformat(timespec="seconds"),
        "chat_id": update.effective_chat.id,
        "message_id": msg.message_id,
        "thread_id": getattr(msg, "message_thread_id", None),
        "mittente": _sender_name(msg),
    }
    folder = os.path.join(project.workspace, queue_dir)
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, f"{os.path.splitext(name)[0]}.json")
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(entry, fh, ensure_ascii=False, indent=1)
    replace_with_retry(tmp, path)
    logger.info("lesson audio proj=%s file=%s -> queued %s", project.key, rel, path)
    return path


LESSON_NOW_FLAG = "elabora_ora"
LESSON_NOW_DATA = "lz:ora"
LESSON_NOW_OK_MSG = (
    "⚡ Richiesta inviata: l'elaborazione parte entro un minuto "
    "(per una lezione lunga ci vuole circa un'ora). Ti scrivo quando è pronta."
)
LESSON_NOW_EMPTY_MSG = (
    "Non c'è nessuna lezione in coda. Se hai appena mandato l'audio, "
    "aspetta che finisca di salvarsi e riprova tra un minuto."
)


def _request_lesson_run(project: Project, queue_dir: str) -> int:
    """Ask the night job to run now. Returns the lessons waiting; 0 = nothing asked.

    The flag sits next to the queue entries (not ``*.json``, so the job never reads it as a
    lesson); the job's poller on the other machine consumes it and starts the same unit as
    the night timer, so two runs never overlap.
    """
    folder = os.path.join(project.workspace, queue_dir)
    try:
        waiting = [n for n in os.listdir(folder) if n.endswith(".json")]
    except FileNotFoundError:
        return 0
    if not waiting:
        return 0
    flag = os.path.join(folder, LESSON_NOW_FLAG)
    tmp = f"{flag}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(dt.datetime.now(dt.UTC).astimezone().isoformat(timespec="seconds"))
    replace_with_retry(tmp, flag)
    logger.info("lesson run requested proj=%s waiting=%d", project.key, len(waiting))
    return len(waiting)


def _lesson_project_here(update: Update):
    """(project, queue_dir) of the chat's project when it has a lesson queue, else None."""
    chat = update.effective_chat
    if chat is None:
        return None
    bound = TOPIC_BINDING.get((chat.id, 0))
    grp = manifest.group_project_for_chat(chat.id)
    key = bound[0] if bound is not None else (grp[0] if grp is not None else None)
    project = resolve_project(key) if key else None
    tg = manifest.telegram_of(project) if project is not None else None
    if project is None or tg is None or not tg.audio_queue:
        return None
    return project, tg.audio_queue


async def _lesson_now(update: Update, reply) -> None:
    here = _lesson_project_here(update)
    if here is None:
        await reply("Qui non c'è nessuna coda di lezioni.")
        return
    try:
        waiting = _request_lesson_run(*here)
    except Exception:
        logger.exception("lesson run request failed")
        await reply("⚠️ Non sono riuscito a chiedere l'elaborazione. Riprova tra poco.")
        return
    await reply(LESSON_NOW_OK_MSG if waiting else LESSON_NOW_EMPTY_MSG)


async def cmd_elabora(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/elabora: start the lesson job now instead of waiting for midnight."""
    msg = update.effective_message
    if msg is None or _profile_or_guest(update) is None:
        return
    await _lesson_now(update, msg.reply_text)


async def on_lesson_now_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Tap on the "⚡ Elabora ora" button under the lesson-received message."""
    q = update.callback_query
    if q is None:
        return
    if _profile_or_guest(update) is None:
        await q.answer("Non autorizzato", show_alert=True)
        return
    await q.answer()
    await _lesson_now(update, update.effective_message.reply_text)


def _lesson_prompt(command: str, msg, rel: str, transcript_rel: str | None = None) -> str:
    """``[CMD:<command>] mittente: … Allegato: <rel> [Trascrizione: <txt>]``."""
    tg_id = getattr(getattr(msg, "from_user", None), "id", 0)
    parts = [f"[CMD:{command}] mittente: {_sender_name(msg)} (tg_id: {tg_id})."]
    caption = (getattr(msg, "caption", None) or "").strip()
    if caption:
        parts.append(f"Testo: {caption}")
    parts.append(f"Allegato: {rel}")
    if transcript_rel:
        parts.append(f"Trascrizione: {transcript_rel}")
    return " ".join(parts)


# Strong references to fire-and-forget tasks: the event loop only keeps weak
# ones, so an unreferenced task can be garbage-collected mid-run.
_BACKGROUND_TASKS: set[asyncio.Task] = set()


def _spawn_background(coro, *, name: str) -> asyncio.Task:
    task = asyncio.create_task(coro, name=name)
    _BACKGROUND_TASKS.add(task)

    def _done(t: asyncio.Task) -> None:
        _BACKGROUND_TASKS.discard(t)
        if not t.cancelled() and t.exception() is not None:
            logger.error("background task %s crashed", t.get_name(), exc_info=t.exception())

    task.add_done_callback(_done)
    return task


def _topic_thread_by_name(chat_id: int, name: str) -> int | None:
    """Forum topic of ``chat_id`` called ``name`` (case-insensitive), or None."""
    want = (name or "").strip().casefold()
    for (chat, thread), known in TOPIC_NAMES.items():
        if chat == chat_id and thread and known.strip().casefold() == want:
            return thread
    return None


LESSON_GET_FILE_TIMEOUT_S = float(os.environ.get("LESSON_GET_FILE_TIMEOUT_S", 900))
_ALBUM_CAPTIONS: dict[str, str] = {}
_ALBUM_CAPTIONS_MAX = 64


def _remember_album_caption(msg) -> None:
    """Keep an album's caption: Telegram puts it on one item only."""
    group = getattr(msg, "media_group_id", None)
    caption = (getattr(msg, "caption", None) or "").strip()
    if not group or not caption:
        return
    _ALBUM_CAPTIONS[str(group)] = caption
    while len(_ALBUM_CAPTIONS) > _ALBUM_CAPTIONS_MAX:
        _ALBUM_CAPTIONS.pop(next(iter(_ALBUM_CAPTIONS)))


def _lesson_topic(chat_id: int, msg) -> str:
    """The course a lesson recording belongs to, for the preprocess ``{topic}``.

    The forum topic name wins; a recording sent outside a course topic (the
    General one) falls back to its caption, or the caption of its album — that
    is how a lesson posted with "Stili del tatuaggio" as text was meant.
    """
    name = TOPIC_NAMES.get((chat_id, _topic_id(msg)))
    if name:
        return name
    caption = (getattr(msg, "caption", None) or "").strip()
    if not caption and (group := getattr(msg, "media_group_id", None)):
        caption = _ALBUM_CAPTIONS.get(str(group), "")
    # "/lezione Pittura" names the course after the command.
    return re.sub(r"^/\S+\s*", "", caption).strip()


async def _notify_owner_of_preprocess_failure(
    update: Update, context, project: Project, rel: str, error: str,
    title: str = "Trascrizione fallita",
) -> None:
    """Short technical DM to the owner profile; never raises."""
    text = (f"⚠️ {title} — progetto {project.key}, file {rel}\n"
            f"{(error or '').strip()[-500:]}")
    tg_bot = getattr(context, "bot", None) if context is not None else None
    if tg_bot is None:
        tg_bot = update.get_bot()
    await _notify_owner(tg_bot, text)


async def _preprocess_then_invoke(
    update: Update, context, project: Project, agent: Agent, command: str,
    template: str, audio_path: str, rel: str,
) -> None:
    """Run the project's ``audio_preprocess`` then hand the transcript to the agent.

    Any failure (non-zero exit, timeout, empty output, unexpected exception)
    is told in the group and to the owner: a lesson must never vanish silently.
    """
    msg = update.effective_message
    out_dir = os.path.join(project.workspace, "99_raw", "trascrizioni")
    stem = os.path.splitext(os.path.basename(audio_path))[0]
    out_path = os.path.join(out_dir, f"{stem}.txt")
    transcript_rel = f"99_raw/trascrizioni/{stem}.txt"
    try:
        os.makedirs(out_dir, exist_ok=True)
        _learn_topic_name_from(msg)
        topic = _lesson_topic(update.effective_chat.id, msg)
        cmd = audio_preprocess.build_command(template, Path(audio_path), Path(out_path), topic)
        ok, error = await audio_preprocess.run_preprocess(cmd, Path(project.workspace))
        if ok and not (os.path.isfile(out_path) and os.path.getsize(out_path) > 0):
            ok, error = False, f"transcript missing or empty: {transcript_rel}"
    except Exception as exc:
        logger.exception("audio preprocess crashed proj=%s file=%s", project.key, rel)
        ok, error = False, f"{type(exc).__name__}: {exc}"
    if not ok:
        logger.error("audio preprocess failed proj=%s file=%s: %s", project.key, rel, error)
        try:
            await msg.reply_text(LESSON_AUDIO_TRANSCRIBE_FAILED_MSG)
        except Exception:
            logger.exception("audio preprocess: group failure reply failed")
        await _notify_owner_of_preprocess_failure(update, context, project, rel, error)
        return
    logger.info("audio preprocess done proj=%s file=%s -> %s", project.key, rel, transcript_rel)
    try:
        await _invoke_and_reply(update, project, agent,
                                _lesson_prompt(command, msg, rel, transcript_rel))
    except Exception as exc:
        # The transcript is safe on disk; without this the group would only see a
        # reaction and nobody would know the notes were never written.
        logger.exception("lesson agent turn failed after transcription proj=%s file=%s",
                         project.key, rel)
        try:
            await msg.reply_text(LESSON_AUDIO_AGENT_FAILED_MSG)
        except Exception:
            logger.exception("audio preprocess: group agent-failure reply failed")
        await _notify_owner_of_preprocess_failure(
            update, context, project, rel,
            f"trascrizione pronta in {transcript_rel}; {type(exc).__name__}: {exc}",
            title="Appunti non preparati dopo la trascrizione",
        )


class _LessonAudioChat(filters.MessageFilter):
    """Matches only chats whose project declares ``audio_command``.

    Evaluated per message, so a group without it is never matched and its
    update flows on to the handlers registered after (group ingest, photos).
    """

    def __init__(self) -> None:
        super().__init__(name="LessonAudioChat")

    def filter(self, message) -> bool:
        chat_id = getattr(message, "chat_id", None)
        if chat_id is None:
            chat_id = getattr(getattr(message, "chat", None), "id", None)
        return chat_id is not None and audio_command_for(chat_id) is not None


LESSON_AUDIO_FILTER = _LessonAudioChat()
LESSON_MEDIA_FILTERS = (
    filters.AUDIO | filters.VIDEO | filters.VIDEO_NOTE
    | filters.Document.AUDIO | filters.Document.VIDEO
)


# --- Node-dedicated bots (multi-bot) -----------------------------------------
# An "extra" bot is a second Telegram surface bound to ONE Life node. It gets a
# deliberately small handler set — /start opens that node's menu, a tap either
# navigates deeper or opens a session in that node's workspace, free text goes
# to the session, and a bound group reuses the SAME generic every-message path
# the Coordinator already has (handle_project_group_message). Everything else
# (the dashboard, capabilities, hub, pipeline, notifications) stays exclusive to
# the default Coordinator bot.

_NODE_GRAPH_CACHE: dict[str | None, tuple[float, dict[str, dict]]] = {}
NODE_GRAPH_TTL = float(os.environ.get("NODE_GRAPH_TTL_SEC", "600"))


def _graph_cached(instance: str | None, force: bool) -> dict[str, dict]:
    """One graph, memoised per instance for NODE_GRAPH_TTL seconds."""
    now = time.time()
    cached = _NODE_GRAPH_CACHE.get(instance)
    if not force and cached is not None and (now - cached[0]) < NODE_GRAPH_TTL:
        return cached[1]
    try:
        nodes = life_registry_client.load_graph_nodes(instance=instance)
    except Exception:
        logger.exception("node graph load failed (instance=%r)", instance)
        nodes = {}
    if nodes or cached is None:
        _NODE_GRAPH_CACHE[instance] = (now, nodes)
        return nodes
    return cached[1]


def _node_graph(force: bool = False) -> dict[str, dict]:
    """Life's default node graph, memoised for NODE_GRAPH_TTL seconds.

    Delegates to ``life_registry_client.load_graph_nodes`` — the same live API
    + disk-cache chain the project registry already uses. Never raises: an
    unreachable Life backend degrades to ``{}`` and the menu says so.
    """
    return _graph_cached(None, force)


def _instance_graph(instance: str, force: bool = False) -> dict[str, dict]:
    """One Life instance's graph (#78), memoised like :func:`_node_graph`."""
    return _graph_cached(instance, force)


def _graph_for(conf: bot_registry.BotConf, force: bool = False) -> dict[str, dict]:
    """The graph a node bot must navigate: its instance's, or the device default.

    A bot with no declared instance keeps today's behaviour exactly (and stays
    stubable in tests); main() derives one from the graph at boot.
    """
    if conf.instance:
        return _instance_graph(conf.instance, force=force)
    return _node_graph(force=force)


def _node_menu_kb(menu: node_menu.Menu) -> InlineKeyboardMarkup:
    """One button per child, plus Back when the walk can go up."""
    rows = [
        [
            InlineKeyboardButton(
                f"{'📂' if e.is_cluster else '▶️'} {e.label}",
                callback_data=pack_cb("nm", "go", e.node_id),
            )
        ]
        for e in menu.entries
    ]
    if menu.parent_id:
        rows.append(
            [InlineKeyboardButton("⬅️ Indietro", callback_data=pack_cb("nm", "go", menu.parent_id))]
        )
    return InlineKeyboardMarkup(rows)


def _node_menu_text(menu: node_menu.Menu) -> str:
    trail = " › ".join(menu.breadcrumb) if menu.breadcrumb else menu.label
    return f"<b>{html_mod.escape(trail)}</b>\nScegli dove vuoi lavorare:"


async def _open_node_session(update: Update, node_id: str) -> None:
    """Bind this (chat, topic) to ``node_id`` so free text lands in that node.

    Reuses the existing binding mechanism verbatim: the session key stays
    ``(chat_id, topic_id)`` and the turn itself is served by handle_message,
    which already resolves the binding, applies the access gate and routes by
    intent. Nothing node-specific is forked here.
    """
    msg = update.effective_message
    chat = update.effective_chat
    if msg is None or chat is None:
        return
    project = resolve_project(node_id)
    if project is None:
        await msg.reply_text(
            f"Il nodo «{node_id}» non è registrato su questo device: "
            "aggiungilo a projects.local.json per aprirci una sessione."
        )
        return
    profile = _profile(update)
    if profile is not None and not can_access(profile, project.key):
        await msg.reply_text(NO_ACCESS_MSG)
        return
    tg = manifest.telegram_of(project)
    agent = resolve_agent(project, tg.agent if tg else "generale") or projects.GENERALE
    _bind_topic(chat.id, _topic_id(msg), project.key, agent.name)
    await msg.reply_text(
        f"{project.emoji} <b>{html_mod.escape(project.label)}</b> — sessione aperta "
        f"({html_mod.escape(agent.name)}).\nScrivi pure: ti rispondo da qui.",
        parse_mode=ParseMode.HTML,
    )


async def _show_node_menu(
    update: Update,
    node_id: str,
    root_id: str,
    *,
    edit: bool = False,
    conf: bot_registry.BotConf | None = None,
) -> None:
    """Render the menu at ``node_id``, or open a session when it is a leaf."""
    msg = update.effective_message
    if msg is None:
        return
    nodes = _graph_for(conf) if conf is not None else _node_graph()
    menu = node_menu.build_menu(node_id, nodes, root_id=root_id)
    if menu is None:
        if not nodes:
            await msg.reply_text(
                "Non riesco a leggere la mappa dei nodi (life_backend irraggiungibile "
                "e nessuna cache). Riprova più tardi."
            )
            return
        await msg.reply_text(f"Nodo «{node_id}» non trovato nella mappa.")
        return
    if menu.is_leaf:
        await _open_node_session(update, node_id)
        return
    text, kb = _node_menu_text(menu), _node_menu_kb(menu)
    if edit:
        try:
            await msg.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)
            return
        except Exception:
            logger.debug("node menu edit failed, sending a new message", exc_info=True)
    await msg.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)


def _make_node_start_handler(conf: bot_registry.BotConf):
    """/start (and /menu) on an extra bot: the bound node's menu."""

    async def _handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if _profile(update) is None:
            logger.warning(
                "blocked user=%s on bot=%s",
                update.effective_user.id if update.effective_user else None,
                conf.name,
            )
            return
        # A project group with its own [[menu]] gets its buttons, not the graph.
        if await _maybe_show_group_menu(update, context):
            return
        # Opens at the bot's ROOT, which may be a cluster — and the same value is
        # the ceiling of the walk, so Back can never escape above it into the
        # rest of the network.
        await _show_node_menu(update, conf.menu_root, conf.menu_root, conf=conf)

    _handler.__qualname__ = f"node_start[{conf.name}]"
    return _handler


def _make_node_menu_callback(conf: bot_registry.BotConf):
    """Taps inside an extra bot's menu: navigate deeper, back, or open a session."""

    async def _handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        q = update.callback_query
        if q is None:
            return
        if _profile(update) is None:
            await q.answer("Non autorizzato", show_alert=True)
            return
        await q.answer()
        parts = parse_cb(q.data or "")
        if len(parts) < 3 or parts[1] != "go":
            return
        target = parts[2]
        nodes = _graph_for(conf)
        if node_menu.resolve_target(target, nodes) == "session":
            await _open_node_session(update, target)
            return
        await _show_node_menu(update, target, conf.menu_root, edit=True, conf=conf)

    _handler.__qualname__ = f"node_menu_cb[{conf.name}]"
    return _handler


#: Messages parked while the person answers "which node did you mean?". Keyed by
#: (chat_id, topic_id); one slot per conversation, overwritten by the next
#: ambiguous message, dropped as soon as it is answered. Bounded on purpose: an
#: unanswered question is not worth remembering forever.
_PENDING_ROUTE: dict[tuple[int, int], Update] = {}
_PENDING_ROUTE_MAX = 200


def _park_pending(key: tuple[int, int], update: Update) -> None:
    if len(_PENDING_ROUTE) >= _PENDING_ROUTE_MAX:
        _PENDING_ROUTE.pop(next(iter(_PENDING_ROUTE)), None)
    _PENDING_ROUTE[key] = update


async def _bind_and_run(update, context, project_key: str, agent_name: str) -> None:
    """Bind this (chat, topic) to a node and let the normal pipeline take over.

    An ``agent`` node (Life#157 §3) is bound with its own agent ``<slug>`` and its
    first message gets a dry answer from Life's common runtime. A failed run is
    said in the room, then the message goes the usual way — a session in the
    node's folder, which for these nodes is closed to the manifest's tools.
    """
    msg, chat = update.effective_message, update.effective_chat
    project = resolve_project(project_key)
    node = agent_nodes.node_of(project.workspace) if project is not None else None
    if node is not None and resolve_agent(project, node.slug) is not None:
        agent_name = node.slug
    _bind_topic(chat.id, _topic_id(msg), project_key, agent_name)
    text = (msg.text or msg.caption or "").strip() if msg is not None else ""
    # Only for a known sender: anyone else meets handle_message's own refusal.
    if node is not None and text and _profile(update) is not None:
        ans = await agent_nodes.answer(node, text)
        if ans.ok:
            await msg.reply_text(ans.risposta)
            return
        await msg.reply_text(agent_nodes.failure_text(node, ans))
    await handle_message(update, context)


def _route_question_kb(decision: "node_routing.Decision", nodes: dict) -> InlineKeyboardMarkup:
    """The buttons that replace a guess."""
    rows = [
        [
            InlineKeyboardButton(
                (nodes.get(c.node_id) or {}).get("label") or c.node_id,
                callback_data=f"nr:{c.node_id}",
            )
        ]
        for c in decision.candidates
    ]
    return InlineKeyboardMarkup(rows)


def _make_node_route_callback(conf: bot_registry.BotConf):
    """Answer to "which node did you mean?": bind, then replay the parked message."""

    async def _handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        q = update.callback_query
        if q is None:
            return
        if _profile(update) is None:
            await q.answer("Non autorizzato", show_alert=True)
            return
        await q.answer()
        parts = (q.data or "").split(":", 1)
        if len(parts) != 2 or not parts[1]:
            return
        target = parts[1]
        project = resolve_project(target)
        if project is None:
            await q.edit_message_text(f"Il nodo «{target}» non è nel registro dei progetti.")
            return
        chat = update.effective_chat
        key = (chat.id, _topic_id(q.message)) if chat and q.message else None
        parked = _PENDING_ROUTE.pop(key, None) if key else None
        tg = manifest.telegram_of(project)
        agent = resolve_agent(project, tg.agent if tg else "") or projects.GENERALE
        label = (_graph_for(conf).get(target) or {}).get("label") or target
        if parked is None:
            # The question outlived its message (restart, or an old keyboard).
            # Bind anyway — the choice is still meaningful — and say plainly that
            # the original text is gone instead of pretending it was handled.
            _bind_topic(chat.id, _topic_id(q.message), project.key, agent.name)
            await q.edit_message_text(
                f"→ {label}. Il messaggio di prima non è più in memoria: riscrivilo pure."
            )
            return
        await q.edit_message_text(f"→ {label}")
        await _bind_and_run(parked, context, project.key, agent.name)

    _handler.__qualname__ = f"node_route_cb[{conf.name}]"
    return _handler


def _registra_indicazione(update, project, text: str) -> None:
    """Scrive il messaggio nel diario del nodo che lo riceve. Mai solleva.

    Il progetto arriva gia' risolto da chi chiama: e' quello a cui la scheda e'
    legata, cioe' lo stesso workspace in cui girera' il turno. Ricavarlo qui
    dentro (dal routing, dal binding, dal nodo del bot) significherebbe avere
    due opinioni su dove va a finire il lavoro, e archiviare l'indicazione dove
    non la cerchera' nessuno.
    """
    try:
        msg = update.effective_message
        chat = update.effective_chat
        if msg is None or project is None or not (text or "").strip():
            return
        autore = getattr(update.effective_user, "full_name", None) or "sconosciuto"
        indicazioni.registra(
            project.workspace,
            autore=autore,
            testo=text,
            stanza=getattr(chat, "title", "") or "",
            nodo=project.key,
        )
    except Exception:
        logger.exception("diario: indicazione non registrata")


def _make_node_text_handler(conf: bot_registry.BotConf):
    """Free text on an extra bot: a session in the right node of its subtree.

    Binds (chat, topic) on first contact and delegates to the untouched
    ``handle_message`` — same gate, same intent routing, same session mechanics
    as the Coordinator bot. What this adds is *which* node gets bound: a bot
    rooted at a cluster reads one room for a whole subtree, so the destination
    comes from the message itself (see ``node_routing``).

    Re-binding rule, and it is the interesting part: once a conversation is bound
    it stays bound, EXCEPT when a later message carries a strong signal — a link
    whose domain a sibling claims — pointing somewhere else. Without that
    exception, sending a rental link and then a product link in the same room
    would silently file the second one under the first one's node. With it, a
    mere change of topic in words is not enough to hijack an ongoing thread.
    """

    async def _handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        msg = update.effective_message
        chat = update.effective_chat
        if msg is None or chat is None:
            return
        tid = _topic_id(msg)
        bound = TOPIC_BINDING.get((chat.id, tid))
        text = msg.text or msg.caption or ""
        nodes = _graph_for(conf)
        decision = node_routing.route(text, conf.menu_root, nodes)

        if bound is not None:
            # An established thread is only redirected by hard evidence.
            strong = (
                decision.kind == "routed"
                and decision.candidates
                and decision.candidates[0].score >= node_routing.DOMAIN_WEIGHT
                and decision.node_id != bound[0]
            )
            if strong:
                project = resolve_project(decision.node_id)
                if project is not None:
                    tg = manifest.telegram_of(project)
                    agent = resolve_agent(project, tg.agent if tg else "") or projects.GENERALE
                    label = (nodes.get(decision.node_id) or {}).get("label") or decision.node_id
                    logger.info(
                        "bot %r: ri-instradato %s -> %s (dominio)",
                        conf.name,
                        bound[0],
                        decision.node_id,
                    )
                    await msg.reply_text(f"↪️ {label}")
                    await _bind_and_run(update, context, project.key, agent.name)
                    return
            await handle_message(update, context)
            return

        # First contact only: the department agent costs a model call, and an
        # established thread is never re-routed by words anyway.
        decision = await reparto_routing.route(text, conf.menu_root, nodes)

        if decision.needs_question:
            _park_pending((chat.id, tid), update)
            await msg.reply_text(
                "Non è chiaro a quale nodo serva: dimmelo tu.",
                reply_markup=_route_question_kb(decision, nodes),
            )
            return

        if decision.kind == "unbuilt":
            planned = (nodes.get(decision.planned) or {}).get("label") or decision.planned
            here = (nodes.get(decision.node_id) or {}).get("label") or decision.node_id
            await msg.reply_text(
                f"ℹ️ Questo sarebbe lavoro di «{planned}», che è previsto ma non ancora "
                f"costruito. Lo passo a «{here}»."
            )

        # Nothing matched (or the graph was unreadable): the message stays on the
        # bot's own project, which is exactly the pre-routing behaviour.
        target = decision.node_id if decision.kind != "root" else conf.node
        project = resolve_project(target)
        if project is None:
            # Silence here is the worst possible answer: to whoever wrote, a bot
            # that reads the message and says nothing is indistinguishable from a
            # bot that is down, and there is nothing to search for. Say it out
            # loud, in the room.
            logger.error(
                "bot %r: node %r non e' nel registro — messaggio scartato", conf.name, target
            )
            await msg.reply_text(
                f"⚠️ Sono configurato male: il nodo «{target}» non esiste nel registro "
                "dei progetti, quindi non so a chi girare questo messaggio. "
                "Il dettaglio e' nel log del Coordinator."
            )
            return
        tg = manifest.telegram_of(project)
        agent = resolve_agent(project, tg.agent if tg else "") or projects.GENERALE
        await _bind_and_run(update, context, project.key, agent.name)

    _handler.__qualname__ = f"node_text[{conf.name}]"
    return _handler


_NODE_BUILTIN_COMMANDS = ("start", "menu", "reset", "new", "argomenti")


def _node_capabilities(
    conf: bot_registry.BotConf,
) -> list[tuple[str, str, "manifest.Capability", int | None]]:
    """``(command, project_key, capability, group_chat_id)`` for this bot's nodes.

    A dedicated bot is its node's door, so everything the node can do has to open
    from it. Without this the Coordinator stays load-bearing in a group it no
    longer serves: capability commands are registered only in its own
    ``setup_handlers``, so a reassigned node's bot would know ``/start /menu
    /reset`` and nothing else, and ``/cena`` — advertised in that group's own
    pinned message — would die the moment the Coordinator leaves the room.

    Declaration order, first-wins: two nodes on one token could pick the same key,
    and PTB would happily run both handlers for one ``/cmd``. The built-ins are
    seeded into ``seen`` so a node can never shadow ``/reset``.
    """
    seen = set(_NODE_BUILTIN_COMMANDS)
    out: list[tuple[str, str, manifest.Capability, int | None]] = []
    for project_key in manifest.projects_for_bot(conf.name):
        project = resolve_project(project_key)
        if project is None:
            continue
        tg = manifest.telegram_of(project)
        chat_id = tg.group_chat_id if tg is not None else None
        for cap in manifest.capabilities_of(project):
            cmd = _tg_command(cap.key)
            if cmd in seen:
                logger.warning(
                    "bot %r: capability %r of %s maps to /%s, already taken — skipped",
                    conf.name,
                    cap.key,
                    project_key,
                    cmd,
                )
                continue
            seen.add(cmd)
            out.append((cmd, project_key, cap, chat_id))
    return out


def _node_commands(conf: bot_registry.BotConf) -> list[BotCommand]:
    """The ``/`` menu of an extra bot — per-token, so it shows this node only."""
    cmds = [
        BotCommand("start", "🏠 Menu del nodo"),
        BotCommand("menu", "🧭 Torna al menu"),
        BotCommand("reset", "🧹 Reset del filo di questa chat"),
    ]
    # Telegram caps the menu at 100 entries and a description at 256 chars.
    for cmd, _project_key, cap, _chat_id in _node_capabilities(conf)[:97]:
        cmds.append(BotCommand(cmd, (cap.label or cap.key)[:256]))
    return cmds


def _register_node_handlers(app: Application, conf: bot_registry.BotConf) -> None:
    """The whole surface of an extra bot. Deliberately small (voice and lesson audio included).

    Sei con l'archivio, che pero' non e' una superficie: sta in ``group=-1``,
    non risponde e non consuma l'update. Deve stare anche qui perche' un bot di
    nodo ha i suoi gruppi e la sua ``getUpdates``: senza, i messaggi di quei
    gruppi non li vedrebbe nessun archivio (vedi ``archive_message``).
    """
    from functools import partial

    app.add_handler(TypeHandler(Update, life_access_gate), group=-3)
    app.add_handler(MessageHandler(filters.ALL, archive_message), group=-1)
    app.add_handler(MessageHandler(TOPIC_SERVICE_FILTER, handle_topic_service), group=-2)
    app.add_handler(CommandHandler(["start", "menu"], _make_node_start_handler(conf)))
    app.add_handler(CommandHandler("reset", cmd_reset))
    app.add_handler(CommandHandler("new", cmd_new))
    app.add_handler(CommandHandler("argomenti", cmd_argomenti))
    app.add_handler(CallbackQueryHandler(on_drill_callback, pattern=r"^gx:"))
    app.add_handler(CallbackQueryHandler(_make_node_menu_callback(conf), pattern=r"^nm:"))
    app.add_handler(CallbackQueryHandler(_make_node_route_callback(conf), pattern=r"^nr:"))
    app.add_handler(CallbackQueryHandler(on_group_menu_callback, pattern=r"^gm:"))
    # Capability commands of this bot's own nodes. Registered AFTER the built-ins
    # (first registration wins, so /reset stays cmd_reset) and BEFORE the group
    # ingest, mirroring the Coordinator's order.
    #
    # Trust boundary — identical to the Coordinator's, deliberately: this widens
    # WHO answers, never WHERE. A group-scoped capability keeps its filters.Chat,
    # so it is reachable only from its own group; command-room/both register with
    # no chat filter and lean on the in-handler default-deny gate.
    for cmd, project_key, cap, chat_id in _node_capabilities(conf):
        handler = _make_capability_handler(project_key, cap.key)
        if cap.scope in ("group", "both") and chat_id is not None:
            app.add_handler(
                CommandHandler(cmd, handler, filters=filters.Chat(chat_id=chat_id))
            )
        if cap.scope in ("command-room", "both"):
            app.add_handler(CommandHandler(cmd, handler))
    # Voice notes (#56), before the group ingest that would swallow them.
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))
    # Lesson recordings: only chats that declare audio_command match, so every
    # other group's media flows on to the ingest below exactly as before.
    app.add_handler(
        MessageHandler(LESSON_MEDIA_FILTERS & LESSON_AUDIO_FILTER, handle_project_audio)
    )
    # Human groups: reuse the generic every-message ingest, never a fork of it.
    for chat_id, thread_id, project_key, _agent, bot_name in manifest.bot_bindings():
        # Forum topics share a chat_id and would steal each other's messages, so
        # they are never chat-wide — same rule the Coordinator bot follows.
        if thread_id or bot_name != conf.name:
            continue
        project = resolve_project(project_key)
        tg = manifest.telegram_of(project) if project is not None else None
        if tg is not None and not tg.wants_every_message:
            continue
        app.add_handler(
            MessageHandler(
                filters.Chat(chat_id=chat_id) & ~filters.COMMAND,
                partial(handle_project_group_message, project_key=project_key),
            )
        )
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, _make_node_text_handler(conf)))


def _make_node_post_init(conf: bot_registry.BotConf):
    """post_init of an extra bot: register itself + its own ``/`` command set.

    Per-token by construction — set_my_commands is a property of the token, so
    each bot advertises only what it can actually do.
    """

    async def _post_init(app: Application) -> None:
        _BOTS[conf.name] = app.bot
        _APPS[conf.name] = app
        try:
            await app.bot.set_my_commands(_node_commands(conf))
        except Exception:
            logger.exception("set_my_commands failed for bot %r", conf.name)
        # "ready" has to be earned, not announced. A bot whose node does not
        # resolve polls happily, greets nobody and drops every message on the
        # floor — which reads in the log exactly like a healthy bot. That is how
        # bot 'casa' sat bound to a node named "casa" (the real key is
        # "homeresearcher") from the day it was declared: one INFO line saying
        # ready, and a mute room. The bot is deliberately NOT refused — a door
        # that answers "I am misconfigured" beats a door that is not there — but
        # the log must say the true thing, at a level nobody scrolls past.
        if resolve_project(conf.node) is None:
            logger.error(
                "bot %r NON operativo | node=%r non e' nel registro dei progetti "
                "(chiavi valide: %s) — imposta %s%s",
                conf.name,
                conf.node,
                ", ".join(sorted(p.key for p in list_projects())),
                bot_registry.NODE_ENV_PREFIX,
                bot_registry.env_suffix(conf.name),
            )
            return
        # The menu root is a graph node, not a project, so it needs its own
        # check — and a three-state one. An unreachable graph means "I could not
        # look", which is NOT the same as "the root is missing": treating the
        # first as the second would raise a false alarm every time life_backend
        # blinks, and an alarm that cries wolf is as useless as silence.
        graph = _graph_for(conf)
        if not graph:
            logger.warning(
                "bot %r: radice %r non verificabile (mappa dei nodi non disponibile)",
                conf.name,
                conf.menu_root,
            )
        elif conf.menu_root not in graph:
            logger.error(
                "bot %r: radice %r assente dalla mappa dei nodi — /start mostrera' un "
                "menu vuoto; correggi %s%s",
                conf.name,
                conf.menu_root,
                bot_registry.ROOT_ENV_PREFIX,
                bot_registry.env_suffix(conf.name),
            )
        # A dedicated bot that no manifest hands a group to is legitimate (it
        # still serves its node in private chat), but it is worth naming: nine
        # times out of ten it means a `bot = "<name>"` line was never added.
        if not manifest.projects_for_bot(conf.name):
            logger.warning(
                "bot %r attivo solo in chat privata | nessun manifest lo dichiara "
                "con bot = %r, quindi non presidia alcun gruppo",
                conf.name,
                conf.name,
            )
        logger.info("bot %r ready | node=%s", conf.name, conf.node)

    return _post_init


async def _node_post_shutdown(app: Application) -> None:
    for name, b in list(_BOTS.items()):
        if b is app.bot and name != bot_registry.DEFAULT_BOT:
            _BOTS.pop(name, None)
            _APPS.pop(name, None)


def build_node_application(conf: bot_registry.BotConf) -> Application:
    """One PTB Application per extra bot — one token, one getUpdates loop."""
    app = (
        telegram_api.apply_api_server(Application.builder(), bot=conf.name)
        .token(conf.token)
        .concurrent_updates(True)
        .post_init(_make_node_post_init(conf))
        .post_shutdown(_node_post_shutdown)
        .build()
    )
    _register_node_handlers(app, conf)
    return app


# --- Startup / wiring --------------------------------------------------------
def _global_commands() -> list[BotCommand]:
    """The ``/`` menu: the dashboard + management commands."""
    return [
        BotCommand("progetti", "📂 Dashboard dei progetti"),
        BotCommand("nuovo", "➕ Nuova scheda: /nuovo <progetto> [agente] [nome]"),
        BotCommand("riprendi", "↩️ Riprendi una sessione: /riprendi <progetto>"),
        BotCommand("reset", "🧹 Reset del filo di questa scheda"),
        BotCommand("chiudi", "🗄 Archivia questa scheda"),
        BotCommand("ricorda", "📝 In coda per il brain: /ricorda [Nodo,] testo"),
        BotCommand("proponi", "💡 Proponi una modifica al nodo (contributor)"),
        BotCommand("notifiche", "🔔 Notifiche schedulate (admin)"),
        BotCommand("compara", "🆚 Confronta piu' modelli: /compara [modelli] su: <richiesta>"),
        BotCommand("opencodemodel", "🛠 Modello OpenCode di default (admin)"),
        BotCommand("dispatch", "🚀 Manda un task a un agente prod"),
        BotCommand("handoff", "🔁 Sposta l'ascolto Telegram su un device"),
        BotCommand("riorganizza", "🗂 Rivedi ora lo schedule (maggiordomo)"),
        BotCommand("prepara", "📋 Prepara ora i task in arrivo (maggiordomo)"),
        BotCommand("pulisci", "🧹 Cancella i messaggi vuoti o doppi del bot (admin)"),
        BotCommand("sospeso", "⏳ Cosa non ha ancora un esito (admin)"),
        BotCommand("stato", "🩺 Cosa sta degradando nella rete (admin)"),
        BotCommand("azzera", "⚠️ Svuota i gruppi dei progetti, con conferma (admin)"),
        BotCommand("help", "❓ Guida"),
    ]


def _group_capability_commands(man) -> list[BotCommand]:
    """Capabilities of one project reachable as a bare ``/cmd`` inside its own
    Telegram group (scope group/both) — the ones a chat-scoped menu should show
    alongside the global commands, mirroring ``_node_commands`` for extra bots."""
    return [
        BotCommand(_tg_command(cap.key), (cap.label or cap.key)[:256])
        for cap in man.capabilities
        if cap.scope in ("group", "both")
    ][:97]


# In-process cache of the last task.due event seen per scheduled_task_id, so a
# later "tk:open" tap (which only carries the st_id in its callback_data) can
# recover name/category/description/start_at for the seed prompt without a
# Scheduler refetch. Plain dict for v1 (unbounded — a TTL/LRU would be a
# sensible future improvement once task volume warrants it); lost on restart,
# which "tk:open" handles gracefully (see _on_task_cb below).
_TASK_DUE_CACHE: dict[int, dict] = {}

# The (chat_id, message_id) of the last live card sent per scheduled_task_id, so
# a new card (each task.due phase — pre/start/late — stacks another one) can
# supersede the previous one: at most ONE tappable card per task, so Fatto and
# Posticipa can't land on conflicting stale copies of the same reminder.
_TASK_DUE_MSG: dict[int, tuple[int, int]] = {}


async def _on_task_due(evt: dict) -> None:
    """Handler for inbound task.due events (M5, wired via hub.set_task_due_handler).

    Routing is per-profile (``profiles.card_target_for`` on the task owner):

    - ``dm`` -> the owner's DM (topic 0).
    - ``project_group`` -> the specialist node's own Telegram group topic.
    - ``coordinator_topic`` (default) -> a per-project forum topic in the shared
      Coordinator hub, auto-created on first use.

    Whatever the resolved (chat, thread), it is bound to the specialist so a
    later ``tk:open`` tap continues the session right there. Caches the event
    for the button taps. Must NEVER raise: hub.dispatch_webhook_event's
    task.due branch only marks the scheduled_task_id as delivered (dedup) after
    this returns successfully, so a raised exception here would make the bus
    keep retrying the same event — logging and swallowing is the correct
    fail-safe.
    """
    try:
        owner = evt["user_telegram_chat_id"]
        mode = profiles.card_target_for(owner)
        target = task_routing.resolve_card_target(evt, mode)
        if target is None:
            # coordinator_topic: resolve/auto-create the shared hub topic.
            project_key, agent_name = task_routing.resolve_specialist(
                evt.get("category")
            )
            project = resolve_project(project_key)
            agent = (
                resolve_agent(project, agent_name) or projects.GENERALE
                if project is not None
                else None
            )
            if project is not None and agent is not None:
                target = await _ensure_project_topic(_BOT, project, agent)
            if target is None:
                target = (owner, 0)
        chat, thread = target

        # Bind the landing (chat, thread) to the specialist so a later tk:open
        # continues the session there (step already bound for hub-created topics,
        # idempotent to redo).
        project_key, agent_name = task_routing.resolve_specialist(evt.get("category"))
        _bind_topic(chat, thread, project_key, agent_name)

        st_id = evt.get("scheduled_task_id")
        # Supersede the prior live card for this task (best-effort delete) so a
        # later phase never stacks a second tappable copy.
        if st_id is not None:
            prev = _TASK_DUE_MSG.get(st_id)
            if prev is not None:
                try:
                    await _BOT.delete_message(prev[0], prev[1])
                except Exception:
                    pass
        keyboard = task_card.render_keyboard(evt)
        # butler (M5 Slice C): if a prep artifact is already cached for this
        # task, offer a "Preparazione" button — rebuild the markup with an
        # extra row rather than touching task_card (keeps that module a pure
        # Scheduler-event renderer, no butler coupling).
        if st_id is not None and _PREP_CACHE.get(st_id):
            rows = list(keyboard.inline_keyboard) + [
                [
                    InlineKeyboardButton(
                        "📋 Preparazione",
                        callback_data=pack_cb("pr", "show", str(st_id)),
                    )
                ]
            ]
            keyboard = InlineKeyboardMarkup(rows)
        msg = await _BOT.send_message(
            chat,
            task_card.render_text(evt),
            reply_markup=keyboard,
            message_thread_id=thread or None,
        )
        if st_id is not None:
            _TASK_DUE_CACHE[st_id] = evt
            _TASK_DUE_MSG[st_id] = (chat, msg.message_id)
            _save_state()  # restart-durable supersede + tap context
    except Exception:
        logger.exception(
            "task.due: card send failed for %s", evt.get("scheduled_task_id")
        )


async def _on_task_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """``tk:open|done|pp:{st_id}`` — the reminder card's three taps.

    ``open`` spawns/resumes the resolved specialist's session in the card's own
    chat/topic (the node's group topic, or the DM if there's no group) with
    the M5 seed prompt; ``done``/``pp`` call the Scheduler lifecycle endpoints
    on the person's behalf (``origin="user"``) and update the card. Those
    lifecycle calls always use the task OWNER's telegram id (cached in the
    evt), never the chat where the tap happened — in a group the tap's chat
    id is the negative group id, which Scheduler's resolve_acting_user cannot
    use.
    """
    # NOTE: answerCallbackQuery may only be called ONCE per callback (a second
    # call errors as "query is too old / already answered"), so unlike the
    # cf:/pl: handlers above we do NOT blanket-answer up front — each branch
    # answers exactly once, with an alert on error or a plain ack on success.
    import scheduler_client as hw

    q = update.callback_query
    _, action, st_s = parse_cb(q.data)
    st_id = int(st_s)
    evt0 = _TASK_DUE_CACHE.get(st_id)
    if evt0 is not None and not _tap_allowed(update, evt0.get("user_telegram_chat_id")):
        await q.answer(DENIED_TAP, show_alert=True)
        return

    if action == "open":
        evt = _TASK_DUE_CACHE.get(st_id)
        if evt is None:
            await q.answer("Contesto scaduto, riapri da Scheduler", show_alert=True)
            return
        project_key, agent_name = task_routing.resolve_specialist(evt.get("category"))
        project = resolve_project(project_key)
        if project is None:
            logger.warning("tk:open: unknown project %s", project_key)
            await q.answer("Progetto specialista non trovato", show_alert=True)
            return
        await q.answer()
        agent = resolve_agent(project, agent_name) or projects.GENERALE
        # Resolve the same landing (chat, thread) the card was sent to, honoring
        # the task owner's per-profile card target (dm / project_group /
        # coordinator_topic hub topic), so the session continues right there.
        owner = evt["user_telegram_chat_id"]
        mode = profiles.card_target_for(owner)
        target = task_routing.resolve_card_target(evt, mode)
        if target is None:
            target = await _ensure_project_topic(context.bot, project, agent)
            if target is None:
                target = (owner, 0)
        chat_id, thread_id = target
        _bind_topic(chat_id, thread_id, project.key, agent.name)
        await _invoke_into_topic(
            context, chat_id, thread_id, project, agent, task_routing.build_seed(evt)
        )
        return

    if action == "done":
        evt = _TASK_DUE_CACHE.get(st_id)
        if evt is None:
            await q.answer("Contesto scaduto, riapri da Scheduler", show_alert=True)
            return
        owner_chat_id = evt["user_telegram_chat_id"]
        try:
            await hw.client_from_env(origin="user").complete_task(st_id, owner_chat_id)
        except hw.SchedulerError as exc:
            logger.warning("tk:done failed for %s: %s", st_id, exc)
            await q.answer("⚠️ Errore verso Scheduler", show_alert=True)
            return
        await q.answer()
        # Terminal: drop the live-card tracking so no later send deletes this
        # "✅ Completato" message (a completed task gets no more reminders).
        _TASK_DUE_MSG.pop(st_id, None)
        _TASK_DUE_CACHE.pop(st_id, None)
        _PREP_CACHE.pop(st_id, None)
        _save_state()
        try:
            await q.edit_message_text("✅ Completato")
        except Exception:
            pass
        return

    if action == "pp":
        evt = _TASK_DUE_CACHE.get(st_id)
        if evt is None:
            await q.answer("Contesto scaduto, riapri da Scheduler", show_alert=True)
            return
        owner_chat_id = evt["user_telegram_chat_id"]
        try:
            await hw.client_from_env(origin="user").postpone_task(st_id, owner_chat_id)
        except hw.SchedulerError as exc:
            logger.warning("tk:pp failed for %s: %s", st_id, exc)
            await q.answer("⚠️ Errore verso Scheduler", show_alert=True)
            return
        _PREP_CACHE.pop(st_id, None)
        _save_state()
        await q.answer()
        try:
            await q.edit_message_text("⏰ Posticipato")
        except Exception:
            pass
        return


# --- taskgen (M5 Slice B) wiring ---------------------------------------------
# Injected callables bound to real bot functions and handed to
# scheduler.register_taskgen_jobs() at startup (see on_startup below) so
# taskgen.py/scheduler.py never import bot.py (acyclic import graph, same
# discipline as providers.py's run_claude injection).


def _taskgen_resolve_workspace(node_key: str) -> str:
    """``resolve_workspace`` for taskgen: a watcher's ``node_key`` -> container
    cwd for ``claude -p``. Falls back to the raw node_key (unresolved) so a
    misconfigured watcher fails loudly downstream (claude_runner) rather than
    silently here."""
    project = resolve_project(node_key)
    return project.workspace if project is not None else node_key


def _taskgen_chat_id_of(profile_key: str) -> int | None:
    """``chat_id_of`` for taskgen: a watcher's ``recipient_profile`` -> the
    Telegram user id to notify/act for. Looks up ``profiles.user_ids_for``
    first; if the profile key has no mapped user (e.g. the seed config's
    ``"owner"``, which isn't a catalog key in profiles.py — the catalog's
    admin profile is ``"owner"``), falls back to the first admin user id so
    the seed watcher still has somewhere to land. Returns None (watcher tick
    skipped, logged by the caller) only if there is truly no admin mapped."""
    ids = profiles.user_ids_for(profile_key)
    if ids:
        return ids[0]
    admins = profiles.admin_user_ids()
    if admins:
        return admins[0]
    return None


def _taskgen_when_str(when_iso: str | None) -> str:
    try:
        if when_iso and "T" in when_iso:
            hhmm = when_iso.split("T", 1)[1][:5]
            if len(hhmm) == 5:
                return f" per le {hhmm}"
    except Exception:
        pass
    return ""


async def _taskgen_send_notice(
    *,
    kind: str,
    suggestion,
    telegram_chat_id: int,
    st_id: int | None = None,
    proposal_id: int | None = None,
) -> None:
    """``send_notice`` for taskgen (injected into ``run_watcher``).

    ``kind="scheduled"`` (trusted branch): act-then-notify — the ScheduledTask
    already exists on Scheduler by the time this runs, so the message tells
    the person what was done and offers an undo (``[❌ Annulla]`` ->
    ``tg:cancel:{st_id}``, see ``_on_taskgen_cancel_cb``). ``kind="proposed"``
    (propose branch): a lighter heads-up — the actionable approve/reject card
    itself is Scheduler's own #17 proposal flow, not this notice.

    Sent straight to ``telegram_chat_id`` (the watcher's resolved recipient,
    always the person's own chat — taskgen has no group/hub routing), so the
    later cancel tap happens in that same chat and needs no cached owner
    lookup (unlike ``_on_task_cb``'s tk:done/pp, which run across group taps).
    Never raises: a failed Telegram send is logged, not propagated into
    ``run_watcher``'s per-suggestion try/except.
    """
    if _BOT is None:
        logger.warning("taskgen send_notice: bot not started yet, dropping notice")
        return
    when_str = _taskgen_when_str(getattr(suggestion, "when_iso", None))
    title = getattr(suggestion, "title", "")
    try:
        if kind == "scheduled":
            text = f"🍳 Ho pianificato «{title}»{when_str}."
            markup = None
            if st_id is not None:
                markup = InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton(
                                "❌ Annulla",
                                callback_data=pack_cb("tg", "cancel", str(st_id)),
                            )
                        ]
                    ]
                )
            await _BOT.send_message(telegram_chat_id, text, reply_markup=markup)
        else:
            text = f"💡 Proposta: «{title}»{when_str} — approvala da Scheduler."
            await _BOT.send_message(telegram_chat_id, text)
    except Exception:
        logger.exception(
            "taskgen send_notice (%s) failed for chat %s", kind, telegram_chat_id
        )


async def _on_taskgen_cancel_cb(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """``tg:cancel:{st_id}`` — the undo button on a taskgen act-then-notify card.

    Deletes the one-off ScheduledTask on Scheduler (``origin="user"``) then
    edits the card in place. The tap always happens in the same chat the
    notice was sent to (taskgen's ``telegram_chat_id`` is always the person's
    own chat, no group routing), so ``q.message.chat_id`` is a valid owner id
    here — unlike ``_on_task_cb``'s done/pp, which must use a cached owner id
    because their cards can land in a shared group topic.
    """
    import scheduler_client as hw

    q = update.callback_query
    _, _action, st_s = parse_cb(q.data)
    st_id = int(st_s)
    chat_id = q.message.chat_id if q.message else None
    if chat_id is None:
        await q.answer("Contesto mancante", show_alert=True)
        return
    if not _tap_allowed(update, chat_id):
        await q.answer(DENIED_TAP, show_alert=True)
        return
    try:
        await hw.client_from_env(origin="user").delete_scheduled_task(
            st_id=st_id, telegram_chat_id=chat_id
        )
    except hw.SchedulerError as exc:
        logger.warning("tg:cancel failed for %s: %s", st_id, exc)
        await q.answer("⚠️ Errore verso Scheduler", show_alert=True)
        return
    await q.answer()
    try:
        await q.edit_message_text("Annullato")
    except Exception:
        pass


# --- butler (M5 maggiordomo proattivo) wiring --------------------------------
# Injected callables bound to real bot functions and handed to
# scheduler.register_butler_jobs() at startup (see on_startup below) so
# butler_reorg.py/butler_prep.py/scheduler.py never import bot.py (acyclic
# import graph, same discipline as taskgen's injection above).


async def _butler_reschedule_pairs(
    items, telegram_chat_id, *, log_tag: str
) -> list:
    """Reschedule ``(st_id, start_at_iso)`` pairs on the person's behalf.

    The single place that talks to Scheduler for a reorganization: used by the
    ``[✅ Applica]`` tap, by the ``auto_apply_trusted`` mode (#19) and by the
    ``[↩️ Annulla]`` undo (which passes each task's ORIGINAL start). Every pair
    is isolated in its own try/except so one failure never blocks the others;
    returns the list of st_ids that failed (empty on full success).
    """
    import scheduler_client as hw

    failed_ids: list = []
    client = hw.client_from_env(origin="user")
    for st_id, start_at in items:
        try:
            await client.reschedule_task(
                st_id=int(st_id),
                telegram_chat_id=telegram_chat_id,
                start_at=start_at,
            )
        except Exception as exc:
            logger.warning("%s st_id=%s failed: %s", log_tag, st_id, exc)
            failed_ids.append(st_id)
    return failed_ids


async def _butler_apply_reorg(st_ids, new_start_iso: str, telegram_chat_id: int) -> list:
    """``apply_reorg`` for butler_reorg's ``auto_apply_trusted`` mode (#19).

    Same reschedule path as the manual ``[✅ Applica]`` tap — the butler acting
    on its own must be indistinguishable, for Scheduler, from the person
    tapping. Returns the st_ids that failed.
    """
    return await _butler_reschedule_pairs(
        [(st_id, new_start_iso) for st_id in (st_ids or [])],
        telegram_chat_id,
        log_tag="butler auto-apply reorg",
    )


async def _butler_send_reorg_card(proposal: dict, chat_id: int) -> None:
    """``send_reorg_card`` for butler_reorg (injected into ``run_reorg_watcher``).

    Generates a short collision-safe token (``secrets.token_urlsafe`` — the
    proposal itself is far too big for Telegram's 64-byte callback_data cap),
    caches the full proposal under it (``proposal`` carries the OWNER's
    ``telegram_chat_id`` — the reschedule on tap must use that, never the tap's
    own chat), composes the card text + apply/ignore keyboard, sends it, then
    persists state so a bot restart doesn't lose the token->proposal mapping.
    Never raises: any failure is logged and swallowed, mirroring
    ``_taskgen_send_notice``.
    """
    try:
        token = secrets.token_urlsafe(6)
        while token in _REORG_CACHE:  # collision-safe, astronomically unlikely
            token = secrets.token_urlsafe(6)
        _REORG_CACHE[token] = proposal

        kind = proposal.get("kind") or "?"
        st_ids = proposal.get("st_ids") or []
        new_start = proposal.get("new_start_iso") or "?"
        rationale = proposal.get("rationale") or ""
        ids_txt = ", ".join(str(i) for i in st_ids)
        verb = "accorpo" if kind == "accorpa" else "sposto"
        if proposal.get("applied"):
            # act-then-notify (#19): the reorganization already happened, the
            # only thing left to offer is a way back.
            text = f"✅ Riorganizzato: {verb} {ids_txt} → {new_start}."
            if rationale:
                text += f" Motivo: {rationale}"
            failed_ids = proposal.get("failed_ids") or []
            if failed_ids:
                failed_txt = ", ".join(str(i) for i in failed_ids)
                text += f"\n⚠️ Non applicato a: {failed_txt}"
            markup = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "↩️ Annulla", callback_data=pack_cb("rg", "undo", token)
                        )
                    ]
                ]
            )
        else:
            text = f"🗂 Riorganizzo? {kind}: {verb} {ids_txt} → {new_start}."
            if rationale:
                text += f" Motivo: {rationale}"
            markup = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "✅ Applica", callback_data=pack_cb("rg", "apply", token)
                        ),
                        InlineKeyboardButton(
                            "❌ Ignora", callback_data=pack_cb("rg", "no", token)
                        ),
                    ]
                ]
            )
        if _BOT is None:
            logger.warning("butler send_reorg_card: bot not started yet, dropping card")
            return
        await _BOT.send_message(chat_id, text, reply_markup=markup)
        _save_state()
    except Exception:
        logger.exception("butler send_reorg_card failed for chat %s", chat_id)


async def _butler_store_artifact(st_id: int, text: str) -> None:
    """``store_artifact`` for butler_prep (injected into ``run_prep_watcher``).

    Caches the prepared artifact so a later ``task.due`` card (``_on_task_due``)
    can offer a "Preparazione" button, and a tap on it (``_on_prep_cb``) can
    show the text. Never raises: logged and swallowed.
    """
    try:
        _PREP_CACHE[int(st_id)] = text
        _save_state()
    except Exception:
        logger.exception("butler store_artifact failed for st_id %s", st_id)


def _butler_audit(kind: str, st_id, outcome: str, **extra) -> None:
    """Append one record to the butler action audit trail (#20).

    Every step of an action's life — proposed, confirmed, discarded, executed
    with its result — leaves a trace here AND in the log. The list is capped at
    ``_BUTLER_AUDIT_MAX`` records so the persisted state stays bounded; the
    caller owns ``_save_state()``. Never raises.
    """
    try:
        record = {
            "ts": dt.datetime.now(dt.timezone.utc).isoformat(),
            "st_id": st_id,
            "kind": kind,
            "outcome": outcome,
        }
        record.update(extra)
        _BUTLER_AUDIT.append(record)
        del _BUTLER_AUDIT[:-_BUTLER_AUDIT_MAX]
        logger.info("butler audit: %s", record)
    except Exception:
        logger.exception("butler audit record failed (kind=%s, st_id=%s)", kind, st_id)


async def _butler_propose_action(action: dict, st: dict, telegram_chat_id: int) -> None:
    """``propose_action`` for butler_prep's ``side_effects`` mode (#20).

    Sends a confirmation card that spells out, in clear, WHAT would be done —
    recipient, subject, a truncated excerpt of the body — and does NOTHING else:
    the side effect happens exclusively in ``_on_prep_action_cb`` after the
    person taps ``[✅ Conferma]``. No ``parse_mode``: the fields are free text
    written by an LLM, so plain text is the only form that cannot inject markup
    (same reasoning as the remote cards of #45).

    Never raises: any failure is logged and swallowed, like the reorg card.
    """
    try:
        kind = action.get("kind") or "?"
        st_id = st.get("id") if isinstance(st, dict) else None

        token = secrets.token_urlsafe(6)
        while token in _PREP_ACTION_CACHE:  # collision-safe, astronomically unlikely
            token = secrets.token_urlsafe(6)
        entry = dict(action)
        entry["st_id"] = st_id
        entry["st_name"] = st.get("name") if isinstance(st, dict) else None
        entry["telegram_chat_id"] = telegram_chat_id
        _PREP_ACTION_CACHE[token] = entry

        body = str(action.get("body") or "")
        excerpt = body if len(body) <= 500 else body[:500] + "…(troncato)"
        text = (
            "✉️ Preparo una bozza e-mail?\n"
            f"Task: {entry.get('st_name') or st_id}\n"
            f"A: {action.get('to') or '?'}\n"
            f"Oggetto: {action.get('subject') or '?'}\n"
        )
        rationale = action.get("rationale")
        if rationale:
            text += f"Motivo: {rationale}\n"
        text += f"\n{excerpt}\n\nNulla verrà fatto senza la tua conferma."
        markup = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "✅ Conferma", callback_data=pack_cb("pa", "ok", token)
                    ),
                    InlineKeyboardButton(
                        "❌ Scarta", callback_data=pack_cb("pa", "no", token)
                    ),
                ]
            ]
        )
        if _BOT is None:
            logger.warning("butler propose_action: bot not started yet, dropping card")
            _PREP_ACTION_CACHE.pop(token, None)
            return
        await _BOT.send_message(telegram_chat_id, text, reply_markup=markup)
        _butler_audit(kind, st_id, "proposed", token=token, to=action.get("to"))
        _save_state()
    except Exception:
        logger.exception("butler propose_action failed for chat %s", telegram_chat_id)


def _butler_execute_action(action: dict) -> tuple[bool, str]:
    """Actually perform a confirmed action. Called ONLY from the confirm tap.

    MVP: ``email_draft`` creates a DRAFT through ``graph_mailer``. The mailer
    only exposes ``send`` today, so unless a ``create_draft`` method exists the
    action fails cleanly with an explicit message — falling back to an actual
    send would turn a preparation into an irreversible outward act, which is
    exactly what this feature must never do.

    Returns ``(ok, human_message)``; never raises.
    """
    kind = action.get("kind")
    if kind != "email_draft":
        return False, f"azione non supportata: {kind}"
    try:
        import graph_mailer

        create_draft = getattr(graph_mailer.GraphMailer, "create_draft", None)
        if create_draft is None:
            return False, "bozze non supportate dal mailer"
        mailer = graph_mailer.GraphMailer(graph_mailer.load_config())
        ok = mailer.create_draft(
            to=str(action.get("to") or ""),
            subject=str(action.get("subject") or ""),
            body_html=str(action.get("body") or ""),
        )
        return bool(ok), "bozza creata" if ok else "creazione bozza fallita"
    except Exception as exc:
        logger.exception("butler execute_action failed for %r", kind)
        return False, f"errore: {exc}"


async def _on_prep_action_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """``pa:ok|no:{token}`` — the ONLY place a #20 preparation action is executed.

    ``no`` discards the cached action without touching anything outside the bot.
    ``ok`` runs ``_butler_execute_action`` and edits the card with the outcome.
    Either way the token is popped (one-shot), the step is audited and the state
    persisted. An unknown/expired token answers with an alert, like ``rg:``.
    """
    q = update.callback_query
    _, action_cb, token = parse_cb(q.data)

    action = _PREP_ACTION_CACHE.get(token)
    if action is None:
        await q.answer("Azione scaduta", show_alert=True)
        try:
            await q.edit_message_text("⌛ Azione scaduta")
        except Exception:
            pass
        return

    if not _tap_allowed(update, action.get("telegram_chat_id")):
        await q.answer(DENIED_TAP, show_alert=True)
        return
    kind = action.get("kind") or "?"
    st_id = action.get("st_id")

    if action_cb == "no":
        _PREP_ACTION_CACHE.pop(token, None)
        _butler_audit(kind, st_id, "discarded", token=token)
        _save_state()
        await q.answer()
        try:
            await q.edit_message_text("Scartata")
        except Exception:
            pass
        return

    if action_cb == "ok":
        _butler_audit(kind, st_id, "confirmed", token=token)
        ok, detail = _butler_execute_action(action)
        _PREP_ACTION_CACHE.pop(token, None)
        _butler_audit(
            kind, st_id, "executed" if ok else "failed", token=token, detail=detail
        )
        _save_state()
        await q.answer()
        try:
            await q.edit_message_text(
                f"✅ {detail}" if ok else f"⚠️ Non eseguita: {detail}"
            )
        except Exception:
            pass
        return

    await q.answer()


def _butler_deps() -> dict:
    """The butler dependency bundle, built ONCE for both trigger paths.

    The autonomous jobs (``scheduler.register_butler_jobs``) and the on-demand
    commands (#21) must inject exactly the same callables, otherwise a manual
    tick would behave differently from the scheduled one. ``hw_client`` is NOT
    in here: the caller owns the Scheduler client (startup builds one for
    taskgen too, a command builds its own).
    """
    return {
        "run_claude": run_claude_html,
        "resolve_workspace": _taskgen_resolve_workspace,
        "resolve_specialist": task_routing.resolve_specialist,
        "dedup": _BUTLER_DEDUP,
        "send_reorg_card": _butler_send_reorg_card,
        "store_artifact": _butler_store_artifact,
        "apply_reorg": _butler_apply_reorg,
        "propose_action": _butler_propose_action,
    }


async def _butler_manual_tick(kind: str, chat_id: int) -> dict | None:
    """Run ONE butler tick on demand for the first watcher of ``kind``.

    Same pipeline as the APScheduler job, with two differences: the watcher is
    used even when ``enabled`` is False (a manual command is an explicit
    request), and ``telegram_chat_id`` is the chat the command came from, not
    ``chat_id_of(recipient_profile)`` — the person asking is the recipient.

    Returns the watcher's counters dict, or None if no watcher of that kind is
    configured. Raises nothing of its own; pipeline errors propagate to the
    command handler, which reports them.
    """
    import butler_config
    import butler_prep
    import butler_reorg
    import scheduler_client as hw

    cfg = scheduler._load_butler_config()
    watcher = butler_config.watcher_for_kind(cfg, kind)
    if watcher is None:
        return None
    policy = butler_config.load_butler_policy(cfg)
    trust = butler_config.load_butler_trust(cfg)
    action_trust = butler_config.load_butler_action_trust(cfg)
    deps = _butler_deps()
    hw_client = hw.client_from_env()
    try:
        if kind == "reorg":
            return await butler_reorg.run_reorg_watcher(
                watcher,
                run_claude=deps["run_claude"],
                hw_client=hw_client,
                dedup=deps["dedup"],
                resolve_workspace=deps["resolve_workspace"],
                send_reorg_card=deps["send_reorg_card"],
                telegram_chat_id=chat_id,
                now=time.time(),
                policy=policy,
                trust=trust,
                apply_reorg=deps["apply_reorg"],
            )
        return await butler_prep.run_prep_watcher(
            watcher,
            run_claude=deps["run_claude"],
            hw_client=hw_client,
            dedup=deps["dedup"],
            resolve_workspace=deps["resolve_workspace"],
            resolve_specialist=deps["resolve_specialist"],
            store_artifact=deps["store_artifact"],
            telegram_chat_id=chat_id,
            now=time.time(),
            policy=policy,
            action_trust=action_trust,
            propose_action=deps["propose_action"],
        )
    finally:
        # Same discipline as the scheduled tick: dedup + caches must survive a
        # restart whether the run succeeded or blew up.
        try:
            _save_state()
        except Exception:
            logger.exception("butler manual tick %s: save_state failed", kind)


async def cmd_pulisci(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """``/pulisci`` — propone di cancellare i messaggi del bot in questa chat che il
    controllo di ``message_guard`` non farebbe piu' partire (vuoti, segnaposto,
    doppi). Solo quelli del registro degli invii, entro 48h; cancella dopo conferma."""
    msg = update.effective_message
    chat = update.effective_chat
    if msg is None or chat is None:
        return
    if not _is_admin(update):
        await msg.reply_text(NO_ACCESS_MSG)
        return
    bad = message_guard.find_bad(chat.id)
    if not bad:
        await msg.reply_text("🧹 Niente da cancellare: nessun messaggio vuoto o doppio nelle ultime 48 ore.")
        return
    token = message_guard.stash_pending(chat.id, [r["id"] for r in bad])
    righe = [f"• {html_mod.escape((r['text'] or '∅')[:50])!s} <i>({r['reason']})</i>" for r in bad[:8]]
    if len(bad) > len(righe):
        righe.append(f"… e altri {len(bad) - len(righe)}")
    await msg.reply_text(
        f"🧹 <b>{len(bad)}</b> messaggi da cancellare:\n" + "\n".join(righe),
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton(f"Cancella {len(bad)}", callback_data=pack_cb("pu", "ok", token)),
            InlineKeyboardButton("Annulla", callback_data=pack_cb("pu", "no", token)),
        ]]),
    )


async def _on_pulisci_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """``pu:ok|no:<token>`` — conferma o annulla la pulizia proposta da /pulisci."""
    q = update.callback_query
    if q is None or q.message is None:
        return
    if not _is_admin(update):
        await q.answer("Solo admin", show_alert=True)
        return
    _, action, token = parse_cb(q.data)
    pending = message_guard.take_pending(token)
    if pending is None:
        await q.answer("Scaduta: rilancia /pulisci", show_alert=True)
        return
    await q.answer()
    if action != "ok":
        await q.edit_message_text("Annullato.")
        return
    chat_id, ids = pending
    deleted: list[int] = []
    for mid in ids:
        try:
            await q.get_bot().delete_message(chat_id=chat_id, message_id=mid)
            deleted.append(mid)
        except Exception as exc:  # gia' cancellato, troppo vecchio, niente permesso
            logger.warning("pulisci: delete %s/%s fallita: %s", chat_id, mid, exc)
    message_guard.forget(chat_id, ids)  # anche quelli non cancellabili: non ripropongo all'infinito
    await q.edit_message_text(f"🧹 Cancellati {len(deleted)} su {len(ids)}.")


WIPE_KEEP_PROJECTS = {
    k.strip().lower()
    for k in os.environ.get("AZZERA_KEEP_PROJECTS", "comunity").split(",")
    if k.strip()
}
_WIPE_TASKS: set = set()


def _wipe_targets() -> tuple[list[tuple[int, list[str]]], list[str]]:
    """(gruppi da svuotare come ``(chat_id, [progetti])``, righe di cio' che si salta)."""
    import manifest as _manifest

    by_chat: dict[int, list[str]] = {}
    skipped: list[str] = []
    keep_chats: set[int] = set()
    for p in list_projects():
        tg = _manifest.telegram_of(p)
        if tg is None or not tg.group_chat_id:
            continue
        if p.key.lower() in WIPE_KEEP_PROJECTS:
            keep_chats.add(tg.group_chat_id)
            continue
        if tg.bot != bot_registry.DEFAULT_BOT:
            skipped.append(f"{p.key} (bot dedicato {tg.bot})")
            continue
        by_chat.setdefault(tg.group_chat_id, []).append(p.key)
    for chat_id in keep_chats & by_chat.keys():
        skipped.append(f"{', '.join(by_chat.pop(chat_id))} (condivide il gruppo con un progetto da tenere)")
    return sorted(by_chat.items()), skipped


async def cmd_azzera(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """``/azzera`` — propone di cancellare TUTTI i messaggi dei gruppi dei progetti,
    tranne quelli in ``AZZERA_KEEP_PROJECTS`` (default: comunity). Irreversibile:
    elenca i gruppi e chiede una conferma esplicita."""
    msg = update.effective_message
    chat = update.effective_chat
    if msg is None or chat is None:
        return
    if not _is_admin(update):
        await msg.reply_text(NO_ACCESS_MSG)
        return
    if chat.type != "private":
        await msg.reply_text("Lancialo in privato con me, non in un gruppo.")
        return
    targets, skipped = _wipe_targets()
    if not targets:
        await msg.reply_text("Nessun gruppo da svuotare.")
        return
    token = message_guard.stash_pending(0, [cid for cid, _ in targets])
    righe = [f"• {', '.join(keys)} <code>{cid}</code>" for cid, keys in targets]
    testo = (
        f"⚠️ <b>Cancello TUTTI i messaggi</b> di {len(targets)} gruppi. Non si torna indietro "
        "(piani di Health compresi).\n" + "\n".join(righe)
    )
    if skipped:
        testo += "\n\nSaltati:\n" + "\n".join(f"• {html_mod.escape(s)}" for s in skipped)
    await msg.reply_text(
        testo,
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("Sì, cancella tutto", callback_data=pack_cb("az", "ok", token)),
            InlineKeyboardButton("Annulla", callback_data=pack_cb("az", "no", token)),
        ]]),
    )


async def _on_azzera_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """``az:ok|no:<token>`` — conferma o annulla l'azzeramento. Lavora in background."""
    q = update.callback_query
    if q is None or q.message is None:
        return
    if not _is_admin(update):
        await q.answer("Solo admin", show_alert=True)
        return
    _, action, token = parse_cb(q.data)
    pending = message_guard.take_pending(token)
    if pending is None:
        await q.answer("Scaduta: rilancia /azzera", show_alert=True)
        return
    await q.answer()
    if action != "ok":
        await q.edit_message_text("Annullato.")
        return
    chat_ids = pending[1]
    bot = q.get_bot()
    status = q.message
    await q.edit_message_text(f"🧹 Azzero {len(chat_ids)} gruppi…")

    async def _run() -> None:
        import chat_wipe

        righe = []
        for cid in chat_ids:
            async def _progress(r, cid=cid) -> None:
                try:
                    await status.edit_text(f"🧹 {cid}: blocchi {r.batches} (ultimo id {r.last_id})")
                except Exception:
                    pass

            r = await chat_wipe.wipe_chat(bot, cid, on_progress=_progress)
            stato = f"errore: {r.error}" if r.error else f"ok, {r.batches} blocchi"
            if r.failed_batches and not r.error:
                stato += f" ({r.failed_batches} falliti)"
            righe.append(f"• <code>{cid}</code> {html_mod.escape(stato)}")
        await bot.send_message(
            status.chat_id, "🧹 Fatto.\n" + "\n".join(righe), parse_mode=ParseMode.HTML
        )

    task = asyncio.create_task(_run())
    _WIPE_TASKS.add(task)
    task.add_done_callback(_WIPE_TASKS.discard)


async def cmd_riorganizza(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """``/riorganizza`` — run the reorg watcher (#21) on demand, here and now."""
    if not _allowed(update):
        return
    msg = update.effective_message
    chat = update.effective_chat
    if msg is None or chat is None:
        return
    try:
        counters = await _butler_manual_tick("reorg", chat.id)
    except Exception:
        logger.exception("/riorganizza failed")
        await msg.reply_text("⚠️ Riorganizzazione fallita, guarda i log.")
        return
    if counters is None:
        await msg.reply_text("Nessun watcher `reorg` configurato.")
        return
    applied = counters.get("applied", 0)
    applied_txt = f"{applied} applicate, " if applied else ""
    await msg.reply_text(
        "🗂 Riorganizzazione: "
        f"{counters.get('proposed', 0)} proposte, "
        f"{applied_txt}"
        f"{counters.get('skipped', 0)} già viste, "
        f"{counters.get('failed', 0)} errori."
    )


async def cmd_prepara(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """``/prepara`` — run the prep watcher (#21) on demand, here and now.

    Takes no arguments: the prep pipeline works on the watcher's schedule
    window (the next ``horizon_hours``), not on a single task, so a per-task
    filter has nothing to bind to. Arguments are refused explicitly rather
    than silently ignored.
    """
    if not _allowed(update):
        return
    msg = update.effective_message
    chat = update.effective_chat
    if msg is None or chat is None:
        return
    if context.args:
        await msg.reply_text(
            "Il filtro per singolo task non è supportato: /prepara prepara "
            "la finestra corrente dello schedule."
        )
        return
    try:
        counters = await _butler_manual_tick("prep", chat.id)
    except Exception:
        logger.exception("/prepara failed")
        await msg.reply_text("⚠️ Preparazione fallita, guarda i log.")
        return
    if counters is None:
        await msg.reply_text("Nessun watcher `prep` configurato.")
        return
    await msg.reply_text(
        "📋 Preparazione: "
        f"{counters.get('prepared', 0)} preparate, "
        f"{counters.get('skipped', 0)} già viste, "
        f"{counters.get('failed', 0)} errori."
    )


async def _on_reorg_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """``rg:apply|no|undo:{token}`` — the reorg card's taps.

    ``apply`` reschedules every ``st_id`` in the cached proposal on the
    person's behalf (``origin="user"``), using the OWNER's telegram id from
    the cached proposal — NEVER the chat where the tap happened (a reorg card
    can, in principle, land in a shared/group context). Each ``st_id`` is
    isolated in its own try/except so one failure doesn't block the others;
    the card is edited to reflect full success / partial failure. ``no``
    just dismisses the proposal. ``undo`` (#19) is the mirror of ``apply`` on
    an already-applied reorganization: it restores each task's original start
    from ``proposal["prev_starts"]``. Either way the token is popped from
    ``_REORG_CACHE`` (one-shot) and state is persisted.
    """
    q = update.callback_query
    _, action, token = parse_cb(q.data)

    proposal = _REORG_CACHE.get(token)
    if proposal is None:
        await q.answer("Proposta scaduta", show_alert=True)
        try:
            await q.edit_message_text("⌛ Proposta scaduta")
        except Exception:
            pass
        return
    if not _tap_allowed(update, proposal.get("telegram_chat_id")):
        await q.answer(DENIED_TAP, show_alert=True)
        return

    if action == "no":
        _REORG_CACHE.pop(token, None)
        _save_state()
        await q.answer()
        try:
            await q.edit_message_text("Ignorato")
        except Exception:
            pass
        return

    if action == "undo":
        # act-then-notify undo (#19): put every task back at its ORIGINAL start,
        # read from the snapshot the watcher took before applying. A task with
        # no recorded original start is skipped rather than guessed at.
        owner_chat_id = proposal.get("telegram_chat_id")
        prev_starts = proposal.get("prev_starts") or {}
        st_ids = proposal.get("st_ids") or []
        pairs = [
            (st_id, prev_starts[str(st_id)])
            for st_id in st_ids
            if str(st_id) in prev_starts
        ]
        undone: list = []
        if pairs:
            failed_ids = await _butler_reschedule_pairs(
                pairs, owner_chat_id, log_tag="rg:undo"
            )
            undone = [st_id for st_id, _ in pairs if st_id not in failed_ids]

        _REORG_CACHE.pop(token, None)
        _save_state()
        await q.answer()
        if not pairs:
            result_text = "⚠️ Impossibile annullare: orari originali non disponibili"
        elif not undone:
            result_text = "⚠️ Errore: annullamento non riuscito"
        elif len(undone) == len(st_ids):
            result_text = "↩️ Annullato"
        else:
            ids_txt = ", ".join(str(i) for i in undone)
            result_text = f"↩️ Annullato parzialmente: {ids_txt}"
        try:
            await q.edit_message_text(result_text)
        except Exception:
            pass
        return

    if action == "apply":
        owner_chat_id = proposal.get("telegram_chat_id")
        new_start_iso = proposal.get("new_start_iso")
        st_ids = proposal.get("st_ids") or []
        failed_ids = await _butler_apply_reorg(st_ids, new_start_iso, owner_chat_id)

        _REORG_CACHE.pop(token, None)
        _save_state()
        await q.answer()
        if not failed_ids:
            result_text = "✅ Riorganizzato"
        elif len(failed_ids) == len(st_ids):
            result_text = "⚠️ Errore: nessuna riorganizzazione applicata"
        else:
            ids_txt = ", ".join(str(i) for i in failed_ids)
            result_text = f"⚠️ Parziale: falliti {ids_txt}"
        try:
            await q.edit_message_text(result_text)
        except Exception:
            pass
        return

    await q.answer()


async def _on_prep_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """``pr:show:{st_id}`` — the task.due card's optional "Preparazione" tap.

    Shows the cached artifact (``butler_prep``'s pre-executed deliverable) in
    place. Missing/expired cache (bot restart wiped it) answers with an alert
    instead of crashing. Long artifacts are truncated to Telegram's practical
    message-length comfort zone (well under the 4096-char hard cap).
    """
    q = update.callback_query
    _, _action, st_s = parse_cb(q.data)
    try:
        st_id = int(st_s)
    except ValueError:
        await q.answer("Riferimento non valido", show_alert=True)
        return

    artifact = _PREP_CACHE.get(st_id)
    if not artifact:
        await q.answer("Preparazione non disponibile", show_alert=True)
        return

    await q.answer()
    text = artifact if len(artifact) <= 3900 else artifact[:3900] + "\n…(troncato)"
    try:
        if q.message is not None:
            await q.message.reply_text(f"📋 Preparazione:\n\n{text}")
    except Exception:
        logger.exception("pr:show failed for st_id %s", st_id)


# --- Generic remote card protocol (Coordinator#45) --------------------------
# The bot side of the injection: remote_cards.py never imports bot.py, so it
# calls back through remote_cards._S, filled here at startup, next to where
# scheduler._S is filled (see on_startup below). ``_remote_card_target``
# resolves a plain project key to its Telegram topic.


def _remote_card_target(project_key: str) -> tuple[int, int] | None:
    """Where a generic remote card lands: the project's Telegram topic, else
    the first admin. ``project_key == ""`` (e.g. no project mapped)
    goes straight to the admin fallback, same as an unresolved project."""
    project = resolve_project(project_key) if project_key else None
    tg = manifest.telegram_of(project) if project is not None else None
    if tg is not None and tg.group_chat_id:
        return tg.group_chat_id, tg.group_thread_id
    admins = profiles.admin_user_ids()
    return (admins[0], 0) if admins else None


async def _remote_send_card(chat_id: int, thread_id, text: str, buttons: list) -> int:
    """``remote_cards._S["send_card"]``: plain text, buttons already carry a
    resolved Telegram ``callback_data`` (built by ``remote_cards.callback_data``
    before this is called). Returns the Telegram message id."""
    markup = None
    if buttons:
        markup = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(btn["label"], callback_data=btn["callback_data"])
                    for btn in row
                ]
                for row in buttons
            ]
        )
    msg = await _BOT.send_message(
        chat_id, text, message_thread_id=thread_id or None, reply_markup=markup
    )
    return msg.message_id


def _remote_card_markup(buttons: list):
    if not buttons:
        return None
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(btn["label"], callback_data=btn["callback_data"]) for btn in row]
            for row in buttons
        ]
    )


async def _remote_edit_card(chat_id: int, message_id: int, text: str, buttons: list) -> None:
    """``remote_cards._S["edit_card"]``: rewrite a card digest in place."""
    await _BOT.edit_message_text(
        text, chat_id=chat_id, message_id=message_id, reply_markup=_remote_card_markup(buttons)
    )


async def _remote_delete_card(chat_id: int, message_id: int) -> None:
    """``remote_cards._S["delete_card"]``: drop a superseded or empty digest."""
    await _BOT.delete_message(chat_id=chat_id, message_id=message_id)


async def _remote_send_attachment(chat_id: int, thread_id, att: dict) -> None:
    """``remote_cards._S["send_attachment"]``: one card attachment (TicketManager#15)
    as its own message in the digest's chat/topic. Plain text, no parse_mode."""
    thread = thread_id or None
    if att["type"] == "text":
        await _BOT.send_message(chat_id, att["text"], message_thread_id=thread)
        return
    file = InputFile(io.BytesIO(att["data"]), filename=att["name"])
    if att["type"] == "photo":
        await _BOT.send_photo(chat_id, file, caption=att.get("caption"), message_thread_id=thread)
    else:
        await _BOT.send_document(
            chat_id, file, caption=att.get("caption"), message_thread_id=thread
        )


# --- Generic remote card protocol (Coordinator#45): the tap side -----------
# This handler owns nothing per card — every tap is forwarded straight to the node that posted
# the card (remote_cards.forward_action, spec §4.2), and whatever the node's
# answer says decides what happens to the card. Coordinator never re-derives
# card content on its own.

async def _on_remote_card_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """``cx:<source>:<card_id>:<action>`` — a generic remote card's buttons.

    Contract (spec §4.2): the node's answer is one of
    ``{"text","buttons"}`` (edit the card; ``buttons: []`` closes it with no
    markup), ``{"toast"}`` (``q.answer(toast)`` only, card untouched), or both
    together (edit the card AND show the toast). Anything else (not a dict,
    or neither ``text`` nor ``toast``) gets a generic "risposta non valida"
    toast and leaves the card untouched.

    Ordering is deliberate (round-1 review finding): the edit happens FIRST,
    the answer LAST, and both are wrapped so neither can stop the other from
    running. Telegram expires a callback query ~15s after the tap, and this
    handler has already spent up to ``remote_cards._FORWARD_TIMEOUT_SEC``
    (12s) waiting on the node by the time it gets here — an expired query
    makes ``q.answer(...)`` raise, and if that raise happened first (as it did
    before this fix) the edit was skipped even though the node had already
    executed the action, leaving the operator staring at a stale card for a
    change that DID happen. Swallowing the ``q.answer`` failure (logged, never
    re-raised) is what keeps that from being a lie: the card itself — edited
    first — is still the source of truth for what happened.
    """
    q = update.callback_query
    parsed = remote_cards.parse_callback(q.data)
    if parsed is None:
        await _remote_card_answer(q, "Card non valida.")
        return
    source, card_id, action = parsed
    # The node acts on the tap: only who may drive a session presses it (#89).
    if SESSION_GATE and not await _session_full(update):
        await _remote_card_answer(q, "🔒 Solo un admin può usare questa card.")
        return

    result = await remote_cards.forward_action(source, card_id, action)
    if not isinstance(result, dict):
        logger.warning("cx: malformed answer from %s for %s/%s: %r",
                        source, card_id, action, result)
        result = {}

    text = result.get("text")
    toast = result.get("toast")
    if text is None and not toast:
        # Neither a card nor a message: a silent answer() would look to the
        # operator as if the tap had done something.
        toast = f"⚠️ Risposta non valida da {source}."

    # A card living in a digest (one message for all the destination's cards)
    # is updated there; the node's text, when the card closes, becomes the toast.
    try:
        in_digest = await remote_cards.apply_answer(source, card_id, result)
    except Exception:
        logger.exception("cx: digest update failed for %s/%s/%s", source, card_id, action)
        in_digest = False
    if in_digest:
        if not toast and isinstance(text, str) and text:
            toast = text[:190]
        await _remote_card_answer(q, toast or None)
        return

    if text is not None:
        try:
            buttons = result.get("buttons") or []
            markup = (
                InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton(
                                btn["label"],
                                callback_data=remote_cards.callback_data(
                                    source, card_id, btn["action"]
                                ),
                            )
                            for btn in row
                        ]
                        for row in buttons
                    ]
                )
                if buttons
                else None
            )
            await q.edit_message_text(text, reply_markup=markup)
        except Exception:
            # Includes the benign "message is not modified" Telegram error, not
            # just genuine failures — either way q.answer below must still run.
            logger.exception("cx: edit failed for %s/%s/%s", source, card_id, action)

    await _remote_card_answer(q, toast or None)


async def _remote_card_answer(q, toast: str | None) -> None:
    """``q.answer(toast)`` (or a bare ``q.answer()``), never letting a raise
    (typically an expired callback query, ~15s TTL) escape — see
    ``_on_remote_card_cb`` for why this must not take the handler down."""
    try:
        if toast:
            await q.answer(toast)
        else:
            await q.answer()
    except Exception:
        logger.warning("cx: q.answer failed (expired callback query?): %s",
                        toast or "<no toast>")


# --- Replay of messages left without a good answer ----------------------------
# the owner's rule: whenever the bot dropped out or answered badly, it answers
# when it comes back. Runs once at startup (single-device mode) and every time
# this instance acquires Telegram polling. Selection logic: replay_unanswered.
REPLAY_STATE_PATH = os.environ.get("REPLAY_STATE_PATH", "/app/data/replayed.json")
REPLAY_ENABLED = os.environ.get("REPLAY_UNANSWERED", "1") != "0"
# Let the backlog Telegram hands over on (re)start go through the normal path
# first: a fresh message must be answered by its own handler, not twice.
REPLAY_DELAY_SEC = int(os.environ.get("REPLAY_DELAY_SEC", "45"))
REPLAY_FAILED_MSG = (
    "😕 Non sono riuscito a rispondere a questa richiesta, nemmeno riprovando "
    "dopo il riavvio. Riscrivila quando vuoi."
)
REPLAY_LOST_MSG = (
    "😕 Questo messaggio mi era arrivato mentre ero offline, ma il file non "
    "l'ho piu': me lo rimandi?"
)
REPLAY_NOTE = "⏳ recupero un messaggio rimasto senza risposta…"
REPLAY_LATE_LINE = "↩️ <i>Arrivato mentre ero offline: rispondo ora.</i>"
_REPLAY_LOCK: asyncio.Lock | None = None


def _replay_window(chat_id: int) -> int:
    try:
        window = manifest.catch_up_seconds(chat_id)
    except Exception:
        window = None
    return window if window is not None else replay_unanswered.DEFAULT_WINDOW_SEC


def _replay_device() -> str:
    """Name of this instance in the replay state (see replay_unanswered.cutover)."""
    cfg = _OWNERSHIP.get("config")
    name = getattr(cfg, "device", None) or os.environ.get("OWNERSHIP_DEVICE")
    if name:
        return str(name)
    import socket

    return socket.gethostname()


def _polls_bot(name: str) -> bool:
    """Does THIS instance currently poll ``name``? Only that one may replay.

    A bot with an ownership ladder of its own (OWNERSHIP_DOMAINS) answers for
    itself; every other bot polls together with the Coordinator.
    """
    domains = _OWNERSHIP.get("domains") or {}
    mgr = domains.get(name)
    if mgr is not None:
        return bool(getattr(mgr, "is_polling", False))
    return _sono_il_volto()


def _replay_binding(chat_id, topic) -> tuple[Project | None, Agent | None]:
    """Come il live: l'argomento senza binding proprio ricade su quello del gruppo (topic 0).

    Senza il ripiego il replay ignorava ogni messaggio scritto in un argomento non legato di persona
    (il 25/10: 9 dispense di a member negli argomenti dei corsi, mai riprese).
    """
    found = _topic_binding(chat_id, topic or 0)
    return found if found[0] is not None else _topic_binding(chat_id, 0)


def _replay_bound(chat_id, topic) -> bool:
    return _replay_binding(chat_id, topic)[0] is not None


def _replay_route(rec: dict) -> str | None:
    """Name of the bot whose handlers must see this message again, or None.

    None means the bot would not answer it live either: unknown sender in a
    private chat, a card with no project, a sender without access. A private
    chat belongs to the bot it was written to; a group card to the bot that
    owns its project (the other one would stand down in silence).
    """
    chat, topic = rec.get("chat"), rec.get("topic") or 0
    uid = rec.get("da_id")
    profile = profile_for(uid) if uid is not None else None
    if rec.get("chat_tipo") == "private":
        if profile is None:
            return None
        name = rec.get("bot") or bot_registry.DEFAULT_BOT
        return name if name in _APPS else None
    project, _agent = _replay_binding(chat, topic)
    if project is None:
        return None
    if profile is not None:
        if not can_access(profile, project.key):
            return None
    elif project.key not in OPEN_GROUP_PROJECTS:
        return None
    name = _served_by_another_bot(project) or bot_registry.DEFAULT_BOT
    return name if name in _APPS else None


async def _replay_unit(app, unit: list[dict]) -> str | None:
    """Push one message (or one album) back through ``app``'s handlers.

    The very handlers that serve live updates do the work -- a voice note is
    transcribed and queued, a photo saved and OCR'd, a private message goes the
    DM path -- with ``_REPLAY`` set so they skip the stale check, tell the agent
    the message is late, and report how it went. Returns "ok", "failed", or
    None when no handler answered (the bot would have stayed silent live too).
    """
    from telegram import Update

    holder: dict = {"rec": unit[0], "outcome": None}
    token = _REPLAY.set(holder)
    try:
        for rec in unit:
            raw = replay_unanswered.build_raw(rec)
            if raw is None:
                continue
            update = Update.de_json({"update_id": 0, "message": raw}, app.bot)
            await app.process_update(update)
        mgid = unit[0].get("media_group_id")
        if mgid:
            # An album is answered by a debounced flush (see _album_buffer):
            # wait for it, or the outcome would be read before the turn ran.
            acc = _album_buffer.get((unit[0].get("chat"), mgid))
            task = acc.get("task") if acc else None
            if task is not None:
                try:
                    await task
                except asyncio.CancelledError:
                    pass
    finally:
        _REPLAY.reset(token)
    return holder.get("outcome")


async def _send_replay_failure(bot, rec: dict, text: str = REPLAY_FAILED_MSG) -> None:
    from telegram import ReplyParameters

    await bot.send_message(
        rec["chat"],
        text,
        message_thread_id=(rec.get("topic") or None),
        reply_parameters=ReplyParameters(
            message_id=rec["msg_id"], allow_sending_without_reply=True
        ),
    )


def _replay_candidates(now: dt.datetime) -> tuple[list[list[dict]], dict]:
    """(units to replay, replay state) -- the read-only half of a scan."""
    since = now - dt.timedelta(seconds=replay_unanswered.MEDIA_WINDOW_SEC)
    messages = replay_unanswered.read_jsonl(MSG_LOG_PATH, since=since)
    turns = replay_unanswered.read_jsonl(CONV_LOG_PATH, since=since)
    handled = replay_unanswered.read_handled(HANDLED_LOG_PATH, since=since)
    state = replay_unanswered.load_state(REPLAY_STATE_PATH)
    device = _replay_device()
    first_time = device not in (state.get("since_all") or {})
    all_since = replay_unanswered.cutover(state, device, now=now)
    if first_time:
        # Persist the cutover at once: kept only in memory it would move forward
        # on every scan, and nothing past it would ever be judged.
        replay_unanswered.save_state(REPLAY_STATE_PATH, state, now=now)
    bot_ids = {getattr(b, "id", None) for b in _BOTS.values()} - {None}
    todo = replay_unanswered.select_unanswered(
        messages,
        turns,
        now=now,
        window_for=_replay_window,
        is_bound=_replay_bound,
        state=state,
        bot_ids=bot_ids,
        handled=handled,
        all_since=all_since,
    )
    return replay_unanswered.group_albums(todo), state


async def _replay_unanswered() -> int:
    """Find and re-dispatch unanswered messages. Returns how many were replayed."""
    global _REPLAY_LOCK
    if not REPLAY_ENABLED or _BOT is None:
        return 0
    if _REPLAY_LOCK is None:
        _REPLAY_LOCK = asyncio.Lock()
    if _REPLAY_LOCK.locked():
        return 0  # a scan is already running (ownership flapped mid-replay)
    async with _REPLAY_LOCK:
        now = dt.datetime.now(dt.UTC)
        units, state = await asyncio.to_thread(_replay_candidates, now)
        logger.info("replay unanswered: %d candidate(s)", len(units))
        done = 0

        def _mark_unit(recs, status, attempts=None):
            for r in recs:
                replay_unanswered.mark(
                    state, replay_unanswered.msg_key(r["chat"], r["msg_id"]),
                    status=status, now=dt.datetime.now(dt.UTC), attempts=attempts,
                )
            replay_unanswered.save_state(REPLAY_STATE_PATH, state, now=now)

        for unit in units:
            rec = unit[0]
            key = replay_unanswered.msg_key(rec["chat"], rec["msg_id"])
            name = _replay_route(rec)
            if name is None:
                logger.info("replay unanswered: skip %s (no route/access)", key)
                continue
            if not _polls_bot(name):
                logger.info("replay unanswered: skip %s (bot %r polled elsewhere)", key, name)
                continue
            app = _APPS[name]
            lost = [r for r in unit if not replay_unanswered.is_recoverable(r)]
            if lost:
                # Archived before the file_id was kept: say so in the thread
                # and ask for it again, instead of leaving it in silence.
                for r in lost:
                    logger.warning("replay unanswered %s: %s without file, asking to resend",
                                   replay_unanswered.msg_key(r["chat"], r["msg_id"]),
                                   r.get("tipo"))
                    try:
                        await _send_replay_failure(app.bot, r, REPLAY_LOST_MSG)
                    except Exception:
                        logger.exception("replay unanswered: lost note not sent")
                _mark_unit(lost, replay_unanswered.LOST)
                unit = [r for r in unit if r not in lost]
                if not unit:
                    continue
                rec = unit[0]
                key = replay_unanswered.msg_key(rec["chat"], rec["msg_id"])
            if _turn_lock(rec["chat"], rec.get("topic") or 0).locked():
                logger.info("replay unanswered: skip %s (card busy)", key)
                continue
            entry = state.get("items", {}).get(key, {})
            attempts = int(entry.get("attempts", 0))
            outcome = "failed"
            while attempts < replay_unanswered.MAX_ATTEMPTS:
                attempts += 1
                # Persist the attempt BEFORE running it: a crash mid-turn must
                # count, or a message that kills the bot would loop forever.
                _mark_unit(unit, replay_unanswered.PENDING, attempts)
                logger.info(
                    "replay unanswered %s bot=%s tipo=%s chat_tipo=%s attempt=%d text=%r",
                    key, name, rec.get("tipo"), rec.get("chat_tipo"), attempts,
                    (rec.get("testo") or "")[:80],
                )
                try:
                    outcome = await _replay_unit(app, unit)
                except Exception:
                    logger.exception("replay unanswered %s: attempt failed", key)
                    outcome = "failed"
                if outcome != "failed":
                    break
            if outcome == "ok":
                done += 1
                status = replay_unanswered.DONE
            elif outcome is None:
                # No handler answered: exactly what the bot would have done live.
                logger.info("replay unanswered %s: no handler answered, skipped", key)
                status = replay_unanswered.SKIPPED
            else:
                status = replay_unanswered.FAILED
                logger.warning("replay unanswered %s: giving up", key)
                try:
                    await _send_replay_failure(app.bot, rec)
                except Exception:
                    logger.exception("replay unanswered %s: failure note not sent", key)
            _mark_unit(unit, status, attempts)
        logger.info("replay unanswered: %d answered", done)
        return done


def _schedule_replay(delay: int = REPLAY_DELAY_SEC) -> None:
    async def _later():
        try:
            await asyncio.sleep(delay)
            await _replay_unanswered()
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("replay unanswered: scan failed")

    _spawn_background(_later(), name="replay-unanswered")


# A turn can fail while the bot stays up ("o non rispondi correttamente"): the
# startup/acquire scan alone would leave it unanswered until the next restart.
REPLAY_INTERVAL_MIN = int(os.environ.get("REPLAY_INTERVAL_MIN", "10"))
REPLAY_JOB_ID = "replay-unanswered"


async def _replay_tick() -> int:
    """Periodic scan; only the instance holding Telegram polling replays."""
    domains = _OWNERSHIP.get("domains") or {}
    polling_any = _sono_il_volto() or any(
        getattr(m, "is_polling", False) for m in domains.values()
    )
    if not polling_any:
        logger.debug("replay unanswered: tick skipped (not polling here)")
        return 0
    logger.info("replay unanswered: periodic tick")
    try:
        return await _replay_unanswered()
    except Exception:
        logger.exception("replay unanswered: periodic scan failed")
        return 0


def _register_replay_job() -> None:
    """Every REPLAY_INTERVAL_MIN minutes, in the existing APScheduler when it
    runs on this device, else in a plain background loop."""
    if not REPLAY_ENABLED or REPLAY_INTERVAL_MIN <= 0:
        return
    sched = scheduler._S.get("sched")
    if sched is not None:
        from apscheduler.triggers.interval import IntervalTrigger

        sched.add_job(
            _replay_tick,
            IntervalTrigger(minutes=REPLAY_INTERVAL_MIN),
            id=REPLAY_JOB_ID,
            max_instances=1,
            coalesce=True,
            replace_existing=True,
        )
        logger.info("replay unanswered: job every %d min", REPLAY_INTERVAL_MIN)
        return

    async def _loop():
        while True:
            await asyncio.sleep(REPLAY_INTERVAL_MIN * 60)
            await _replay_tick()

    _spawn_background(_loop(), name="replay-unanswered-loop")
    logger.info("replay unanswered: loop every %d min (no scheduler)", REPLAY_INTERVAL_MIN)


NOISE_SWEEP_MIN = 30


async def _sweep_noise() -> int:
    """Cancella i messaggi di servizio (errori, avvisi, «sto pensando») dopo 24h, in tutti i gruppi."""
    domains = _OWNERSHIP.get("domains") or {}
    if not (_sono_il_volto() or any(getattr(m, "is_polling", False) for m in domains.values())):
        return 0
    bots = {str(getattr(b, "token", "")).split(":")[0]: b for b in _BOTS.values()}
    gone: dict[int, list[int]] = {}
    for r in message_guard.due_noise():
        bot = bots.get(str(r.get("bot"))) or _BOT
        try:
            await bot.delete_message(chat_id=r["chat"], message_id=r["id"])
        except Exception as exc:  # gia' cancellato, oltre le 48h, niente permesso: non riprovare all'infinito
            logger.info("noise sweep: %s/%s non cancellabile: %s", r["chat"], r["id"], exc)
        gone.setdefault(r["chat"], []).append(r["id"])
    for chat, ids in gone.items():
        message_guard.forget(chat, ids)
    n = sum(len(v) for v in gone.values())
    if n:
        logger.info("noise sweep: %d messaggi di servizio tolti", n)
    return n


def _register_noise_sweep() -> None:
    sched = scheduler._S.get("sched")
    if sched is not None:
        from apscheduler.triggers.interval import IntervalTrigger

        sched.add_job(_sweep_noise, IntervalTrigger(minutes=NOISE_SWEEP_MIN), id="noise-sweep",
                      max_instances=1, coalesce=True, replace_existing=True)
        return

    async def _loop():
        while True:
            await asyncio.sleep(NOISE_SWEEP_MIN * 60)
            try:
                await _sweep_noise()
            except Exception:
                logger.exception("noise sweep failed")

    _spawn_background(_loop(), name="noise-sweep-loop")


async def cmd_sospeso(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/sospeso (admin): tutto cio' che e' scritto su Telegram e non ha ancora un esito."""
    msg = update.effective_message
    if msg is None or not _is_admin(update):
        return
    now = dt.datetime.now(dt.UTC)
    lines: list[str] = []
    try:
        units, state = _replay_candidates(now)
    except Exception:
        logger.exception("sospeso: scan failed")
        await msg.reply_text("⚠️ Non riesco a leggere il registro dei messaggi.")
        return
    for unit in units:
        r = unit[0]
        what = r.get("tipo") or "testo"
        label = (r.get("testo") or (r.get("media") or {}).get("nome") or "").strip()[:50]
        lines.append(f"⏳ {r.get('chat_nome') or r['chat']} · {what} {label} (msg {r['msg_id']})")
    for key, e in (state.get("items") or {}).items():
        if e.get("status") in (replay_unanswered.FAILED, replay_unanswered.LOST):
            lines.append(f"❌ {key}: {e.get('status')}")
    waiting = 0
    for p in projects.list_projects():
        tg = manifest.telegram_of(p)
        if tg and tg.audio_queue:
            try:
                folder = os.path.join(p.workspace, tg.audio_queue)
                waiting += len([n for n in os.listdir(folder) if n.endswith(".json")])
            except OSError:
                pass
    if waiting:
        lines.append(f"🎧 {waiting} lezioni in coda per l'elaborazione")
    head = "Ancora in sospeso:" if lines else "✅ Niente in sospeso: ogni messaggio ha il suo esito."
    extra = f"\n… e altri {len(lines) - 30}" if len(lines) > 30 else ""
    await msg.reply_text("\n".join([head, *lines[:30]]) + extra)


async def cmd_stato(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/stato (admin): cosa sta degradando adesso nella rete (spec Life P4)."""
    msg = update.effective_message
    if msg is None or not _is_admin(update):
        return
    import stato
    try:
        data = await asyncio.to_thread(stato.fetch)
    except RuntimeError as exc:
        logger.warning("stato: Life non risponde (%s)", exc)
        await msg.reply_text(stato.render_failure(str(exc)), parse_mode="HTML")
        return
    await msg.reply_text(stato.render(data), parse_mode="HTML")


async def on_startup(app: Application) -> None:
    global _BOT
    _BOT = app.bot
    # Same object, reachable by name: extra bots register themselves the same
    # way in their own post_init, so bot_for() is uniform across all of them.
    _BOTS[bot_registry.DEFAULT_BOT] = app.bot
    _APPS[bot_registry.DEFAULT_BOT] = app
    try:
        await _check_contested_chats(app.bot)
    except Exception:
        logger.exception("contested-chat check failed")
    try:
        _log_instance_bindings()
    except Exception:
        logger.exception("instance bindings check failed")
    commands = _global_commands()
    try:
        await app.bot.set_my_commands(commands)
        logger.info("registered %d bot commands", len(commands))
    except Exception:
        logger.exception("set_my_commands failed")
    # Per-chat menu: set_my_commands above is the bot-wide default scope, which
    # Telegram shows in a chat only where no more specific scope is set. A
    # project's own group/both capabilities (e.g. ComUnity's /invito) never
    # appeared in the "/" autocomplete because nothing told Telegram about them
    # for that chat — the CommandHandler answering the command is unrelated to
    # what the menu displays. One BotCommandScopeChat call per project group.
    import manifest as _manifest

    for p in list_projects():
        man = _manifest.load(p)
        tg = man.telegram
        if not tg or tg.group_chat_id is None:
            continue
        # A group declared for a dedicated bot is not ours to decorate:
        # set_my_commands is per token, and a token that is not in the chat gets
        # "Chat not found" (Scheduler's room, served by @scheduler_life_ai_bot).
        # This holds even when that bot is NOT configured on this device — which
        # is the usual case, since its token is polled by Scheduler's own bot and
        # a second poller would 409. Message routing falls back to us there
        # (_served_by_another_bot), but the menu of a room we are not in cannot.
        # Only a positively absent owner (_ABSENT_OWNERS) hands the room back.
        if (
            tg.bot != bot_registry.DEFAULT_BOT
            and (tg.bot, tg.group_chat_id) not in _ABSENT_OWNERS
        ):
            continue
        extra = _group_capability_commands(man)
        if not extra:
            continue
        try:
            await app.bot.set_my_commands(
                commands + extra, scope=BotCommandScopeChat(chat_id=tg.group_chat_id)
            )
            logger.info(
                "registered %d group commands for %s", len(extra), p.key
            )
        except Exception:
            logger.exception("set_my_commands (group scope) failed for %s", p.key)

    async def _send_to(uid: int, text: str) -> None:
        await _send_chunks_to(app.bot, uid, text)

    async def _send_target(chat_id: int, thread_id, text: str) -> None:
        await _send_chunks_thread(app.bot, chat_id, thread_id, text)

    if SCHEDULER_ENABLED:
        try:
            scheduler.start(
                run_claude=run_claude_html,
                send_to=_send_to,
                send_target=_send_target,
                tz=TZ_NAME,
                topic_thread=_topic_thread_by_name,
            )
        except Exception:
            logger.exception("scheduler start failed")
        # taskgen (M5 Slice B): needs its own Scheduler-writing client (unlike
        # the notification scheduler above); AGENT_API_SECRET/SCHEDULER_API_URL
        # missing is a normal "not configured on this device" state (spec §5),
        # not a startup failure — log and skip watcher registration.
        try:
            import scheduler_client as hw

            hw_client = hw.client_from_env()
            scheduler.register_taskgen_jobs(
                scheduler._S["sched"],
                run_claude=run_claude_html,
                hw_client=hw_client,
                send_notice=_taskgen_send_notice,
                resolve_workspace=_taskgen_resolve_workspace,
                dedup=_TASKGEN_DEDUP,
                chat_id_of=_taskgen_chat_id_of,
                save_state=_save_state,
            )
            # butler (M5 maggiordomo proattivo): same Scheduler client + the
            # not-configured degrade as taskgen above (same try/except).
            scheduler.register_butler_jobs(
                scheduler._S["sched"],
                hw_client=hw_client,
                chat_id_of=_taskgen_chat_id_of,
                save_state=_save_state,
                **_butler_deps(),
            )
        except hw.SchedulerNotConfigured:
            logger.info("taskgen/butler disabled: Scheduler agent API not configured")
        except Exception:
            logger.exception("taskgen/butler job registration failed")
    else:
        logger.info("scheduler disabled on this device (SCHEDULER_ENABLED=0)")
    _register_replay_job()
    _register_noise_sweep()

    # Generic remote card protocol (Coordinator#45): inject unconditionally —
    # /api/cards is a plain HTTP route, not an APScheduler job, so it does not depend on SCHEDULER_ENABLED. A
    # card posted before this line gets 503 (remote_cards._S["send_card"] is
    # still None), never a crash.
    remote_cards._S["send_card"] = _remote_send_card
    remote_cards._S["resolve_target"] = _remote_card_target
    remote_cards._S["edit_card"] = _remote_edit_card
    remote_cards._S["delete_card"] = _remote_delete_card
    remote_cards._S["send_attachment"] = _remote_send_attachment

    try:
        import notif_sync

        notif_sync.sync_from_manifests()
    except Exception:
        logger.exception("manifest notification sync failed")

    try:
        hub.start(
            bot=app.bot,
            run_claude=run_claude_html,
            send_to=_send_to,
            config=hub_config.load_config(),
            # Listeners are split: the LOCAL agent listeners must run on every
            # node always (they wake the agents this box hosts), so we start
            # them unconditionally below. Only the OWNER listeners (telegram
            # face + snapshot/resume) follow the Telegram polling owner, and
            # are toggled by the supervisor in _run_with_ownership. We therefore
            # never auto-spawn here; we wire deps then start the groups
            # explicitly based on the ownership mode.
            spawn_listeners=False,
        )
        hub.set_task_due_handler(_on_task_due)
        # Local listeners: always on, regardless of ownership.
        hub.start_local_listeners()
        # Single-node (ownership disabled) has no _acquire to arm the owner
        # listeners, so start them here too. With ownership enabled the
        # supervisor starts/stops them on acquire/release instead.
        if not ownership.load_config().enabled:
            hub.start_owner_listeners()
        # send_thread used by hub for topic posts
        hub._S["send_thread"] = _send_chunks_thread
        pipeline.configure(
            client=hub._client,
            open_topic=hub._open_pipeline_topic,
            send_thread=_send_chunks_thread,
            post_gate=_post_gate,
            bot=app.bot,
            config=hub._S["config"],
        )
        migrate.configure(
            client=hub._client,
            open_topic=hub._open_pipeline_topic,
            send_thread=_send_chunks_thread,
            bot=app.bot,
            config=hub._S["config"],
        )
        restructure.configure(
            client=hub._client,
            open_topic=hub._open_pipeline_topic,
            send_thread=_send_chunks_thread,
            bot=app.bot,
            config=hub._S["config"],
        )
    except Exception:
        logger.exception("hub start failed")

    # Web API (LAN, no auth) nello stesso event loop: condivide lo scheduler
    # vivo, quindi le modifiche da web si applicano all'istante come da Telegram.
    # Porta 8018 (8013 è riservata all'ownership status endpoint).
    try:
        import uvicorn

        import api  # FastAPI app; reaches run_claude/send_target via scheduler._S

        # Default = the node's registry `back` port (9999 + index 2). The old
        # default 8018 predates the Life standardization and, since compose did
        # not pass NOTIF_API_PORT, is what the API actually bound — behind a
        # publication that pointed elsewhere. Never default to a legacy port.
        _api_port = int(os.environ.get("NOTIF_API_PORT", "10001"))
        _api_cfg = uvicorn.Config(
            api.app, host="0.0.0.0", port=_api_port, log_level="info", loop="asyncio"
        )
        asyncio.create_task(uvicorn.Server(_api_cfg).serve())
        logger.info("notifications web API listening on :%d", _api_port)
        # A2A push webhook mode: the inbox configs are registered by the
        # ownership ACQUIRE, not here — a push config is delivered on top of the
        # SSE fan-out, so a standby peer holding one for a shared identity
        # (telegram-owner is the same agent on both devices) would double every
        # escalation. On a single-device deployment ownership is disabled and
        # _run_with_ownership never runs, so register here instead: that path
        # polls unconditionally, i.e. it is always the owner.
        _hub_cfg = hub._S.get("config")
        if (
            _hub_cfg
            and _hub_cfg.a2a_webhook_enabled
            and not ownership.load_config().enabled
        ):
            asyncio.create_task(hub.register_push_webhooks())
    except Exception:
        logger.exception("web API start failed")

    if CLAUDE_LIVE_SESSIONS:
        global _evict_task
        _evict_task = asyncio.create_task(_live_evict_loop())
        logger.info(
            "live sessions ON (max=%d, idle_ttl=%ds)",
            CLAUDE_MAX_LIVE_SESSIONS,
            SESSION_TTL_SEC,
        )
    elif CLAUDE_LIVE_SESSIONS_REQUESTED:
        logger.warning(
            "CLAUDE_LIVE_SESSIONS=1 IGNORED: the warm path only speaks claude's "
            "stream-json protocol, and this deployment runs AI_BACKEND=%s. Every "
            "turn goes through the cold run_claude, which honours the backend. "
            "Unset CLAUDE_LIVE_SESSIONS or switch AI_BACKEND to claude.",
            claude_runner.AI_BACKEND,
        )

    global _projects_refresh_task
    _projects_refresh_task = asyncio.create_task(_projects_refresh_loop(app.bot))
    # Single device: no _acquire will ever run, so startup is the only "came back".
    if not ownership.load_config().enabled:
        _schedule_replay()
    logger.info(
        "projects refresh loop ON (interval=%ds)", PROJECTS_REFRESH_INTERVAL_SEC
    )


async def on_shutdown(app: Application) -> None:
    """Tear down warm live sessions and stop the eviction/refresh loops on shutdown."""
    global _evict_task, _projects_refresh_task
    if _evict_task is not None:
        _evict_task.cancel()
        try:
            await _evict_task
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("evict task shutdown")
        _evict_task = None
    if _projects_refresh_task is not None:
        _projects_refresh_task.cancel()
        try:
            await _projects_refresh_task
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("projects refresh task shutdown")
        _projects_refresh_task = None
    if CLAUDE_LIVE_SESSIONS:
        try:
            await _live_manager().close()
        except Exception:
            logger.exception("live close failed")


# ---------------------------------------------------------------------------
# Manifest capability helpers
# ---------------------------------------------------------------------------


def commands_for_context(capabilities, context: str):
    """Capabilities visible in a context: 'group' or 'command-room'.

    A capability with scope 'both' shows in either; otherwise scope must match.
    """
    return [c for c in capabilities if c.scope == "both" or c.scope == context]


def _cap_prompt(capability, args: str) -> str:
    """Bridge a bot command to the project's claude session via the [CMD:/key] convention."""
    args = (args or "").strip()
    return f"[CMD:/{capability.key}] {args}".strip()


def _tg_command(cap_key: str) -> str:
    """Telegram bot command names accept only [a-z0-9_] (no hyphens). Capability
    keys are stable manifest ids and may contain hyphens (e.g. ``open-preset``);
    those keys stay intact for in-handler dispatch (``c.key == cap_key``) — only
    the registered COMMAND string is normalized here. Without this, a hyphenated
    key makes python-telegram-bot raise ``Command ... is not a valid bot command``
    and crash the whole bot at startup. User types the underscore form
    (``/open_preset``); the handler still resolves the original capability.
    """
    return re.sub(r"[^a-z0-9_]", "_", cap_key.lower())


async def _handle_direct_cli_capability(update: Update, project, cap, args: str) -> None:
    """Dispatch an ``action=direct_cli`` capability: run ``cap.cli`` as a
    subprocess in the project's workspace, no LLM in the loop. This is what
    ``action="session"`` (the default) never was — ``[CMD:/key]`` there only
    hands a prompt to the project's agent, which must itself read the
    manifest and decide to run the cli; live on ComUnity's /invito that meant
    a 15-20s round trip through a full agent session for what should be one
    instant shell command (issue: "sicuro che non chiama llm").
    Args are shell-quoted and appended after ``cap.cli``, never interpolated
    into it — free text typed by a Telegram user must never reach a shell as
    raw syntax."""
    msg = update.effective_message
    if not msg:
        return
    args = (args or "").strip()
    cmd = f"{cap.cli} {shlex.quote(args)}" if args else cap.cli
    try:
        proc = await asyncio.create_subprocess_shell(
            cmd,
            cwd=project.workspace,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=60)
    except TimeoutError:
        await msg.reply_text("⏱ Timeout: il comando non ha risposto entro 60s.")
        return
    except Exception:
        logger.exception("direct_cli capability %r failed to spawn", cap.key)
        await msg.reply_text("⚠️ Errore nell'esecuzione del comando.")
        return
    text = (out or b"").decode("utf-8", "replace").strip() or "(nessun output)"
    if proc.returncode != 0:
        body = f"⚠️ Comando terminato con errore ({proc.returncode}):\n{_h(text)}"
        await _send_html_chunks(msg, body)
        return
    # Solo il codice in <code> (tap-to-copy), il resto della riga testo normale —
    # non l'intera riga/blocco in <pre>: quel modo racchiudeva anche "[valido]
    # scade: ..." nel tap-to-copy e su Telegram non risultava copiabile affatto.
    # L'action="session" precedente ottenendo lo stesso effetto perche' l'agente
    # scriveva SOLO il codice tra backtick in mezzo a una frase; qui replichiamo
    # lo stesso risultato riga per riga, senza passare da nessun agente.
    text, markup = group_menu.extract_buttons(text)
    if markup is not None and not text:
        text = "👇"
    body = "\n".join(_code_first_token(line) for line in text.split("\n"))
    await _send_html_chunks(msg, body, reply_markup=markup)


def _code_first_token(line: str) -> str:
    """Wrap the leading token of ``line`` in ``<code>`` (Telegram
    tap-to-copy) when it is followed by a double space, leaving the rest as
    plain escaped text. Matches the shape ``CODE  [stato]  altro`` that
    ``invite.py``'s ``_fmt`` prints (double space is its column separator) —
    a single-space sentence like ``(nessun output)`` falls through unwrapped."""
    head, sep, rest = line.partition("  ")
    if not sep:
        return _h(line)
    return f"<code>{_h(head)}</code>{sep}{_h(rest)}"


def _caption_capability(update: Update, project):
    """The capability a photo's caption asks for, if any.

    Telegram delivers a command written as a photo caption in ``caption`` and
    never through ``CommandHandler``: ``/segnala <testo>`` with an attached
    photo would otherwise be ingested as a plain photo. Same command names as
    the text surface, including the ``/name@bot`` form.
    """
    msg = update.effective_message
    text = (getattr(msg, "caption", None) or "").strip() if msg is not None else ""
    if not text.startswith("/"):
        return None
    name = text.split(None, 1)[0][1:].split("@", 1)[0].lower()
    if not name:
        return None
    for cap in manifest.capabilities_of(project):
        if _tg_command(cap.key).lower() == name:
            return cap
    return None


def _make_capability_handler(project_key: str, cap_key: str):
    async def _handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await _run_capability(update, project_key, cap_key)

    return _handler


async def _run_capability(
    update: Update, project_key: str, cap_key: str, args: str | None = None
) -> None:
    """Body of a capability command. ``args=None`` reads them from the typed
    message; a group-menu tap passes them explicitly, because there the message
    is the bot's own menu, not the person's ask."""
    msg = update.effective_message
    if not msg:
        return
    project = resolve_project(project_key)
    if project is None:
        return
    import manifest

    cap = next(
        (c for c in manifest.capabilities_of(project) if c.key == cap_key), None
    )
    if cap is None:
        return
    # Group context is trusted; command-room context still needs profile access.
    chat = update.effective_chat
    tg = manifest.telegram_of(project)
    in_group = bool(tg and chat and chat.id == tg.group_chat_id)
    if not in_group:
        profile = _profile(update)
        if profile is None or not can_access(profile, project.key):
            await msg.reply_text(NO_ACCESS_MSG)
            return
    if args is None:
        # A photo arrives with the command in the caption, not in msg.text.
        args = (msg.text or msg.caption or "").partition(" ")[2]
    team = manifest.team_of(project)
    grant = None
    if cap.requires or getattr(cap, "action", "session") == "team":
        grant = await _life_grant(update, project, team)
        level = cap.requires.split(":", 1)[1] if ":" in cap.requires else "use"
        if not _grant_allows(grant, level):
            await msg.reply_text(LIFE_ACCESS_REFUSALS[life_access.DENIED])
            return
    if getattr(cap, "action", "session") == "team":
        if team is None:
            await msg.reply_text("Comandi del team non configurati per questo progetto.")
            return
        iid = _team_instance(team, update)
        if not iid:
            await msg.reply_text("Istanza non risolta per questa chat.")
            return
        await team_commands.handle(update, project, cap, args, grant=grant, team=team,
                                   instance_id=iid, agent_name=_topic_agent(update, project))
        return
    if getattr(cap, "action", "session") == "scheduler_proposal":
        await _handle_scheduler_capability(update, cap, args)
        return
    if getattr(cap, "action", "session") == "direct_cli":
        await _handle_direct_cli_capability(update, project, cap, args)
        return
    agent = (
        resolve_agent(project, (tg.agent if tg else "generale"))
        or projects.GENERALE
    )
    await _invoke_and_reply(update, project, agent, _cap_prompt(cap, args))


async def _handle_scheduler_capability(update: Update, cap, args: str) -> None:
    """Dispatch an ``action=scheduler_proposal`` capability: build the payload
    from the command args and create a node->person Proposal on Scheduler. The
    person confirms it on their side (Telegram DM / web) — we never confirm for
    them (contract §5/§6). Target = the commanding user (Telegram ids are global,
    so their private chat_id equals their user id)."""
    import scheduler_client as hw

    msg = update.effective_message
    if not msg:
        return
    try:
        summary, payload = hw.build_proposal(cap.kind, args.split())
    except ValueError as exc:
        hint = f"\nUso: {cap.hint}" if cap.hint else ""
        await msg.reply_text(f"⚠️ {exc}{hint}")
        return

    target = update.effective_user.id if update.effective_user else None
    if target is None:
        await msg.reply_text("⚠️ Impossibile identificare la persona destinataria.")
        return
    try:
        client = hw.client_from_env()
        res = await client.create_proposal(
            kind=cap.kind,
            summary=summary,
            payload=payload,
            telegram_chat_id=target,
        )
    except hw.SchedulerNotConfigured:
        await msg.reply_text(
            "⚠️ Scheduler non configurato (manca SCHEDULER_API_URL o AGENT_API_SECRET)."
        )
        return
    except hw.SchedulerForbidden as exc:
        await msg.reply_text(f"⛔ Scheduler ha rifiutato la proposta: {exc}")
        return
    except hw.SchedulerNotFound:
        await msg.reply_text(
            "❓ Persona non trovata su Scheduler (collega prima il tuo Telegram)."
        )
        return
    except hw.SchedulerConflict:
        await msg.reply_text("↩️ Proposta già gestita.")
        return
    except hw.SchedulerAuthError:
        await msg.reply_text("🔑 Segreto Agent API errato: verifica AGENT_API_SECRET.")
        return
    except hw.SchedulerError as exc:
        logger.warning("scheduler proposal failed: %s", exc)
        await msg.reply_text(f"⚠️ Errore verso Scheduler: {exc}")
        return

    pid = res.get("id")
    await msg.reply_text(
        f"✅ Proposta #{pid} inviata a Scheduler: «{summary}».\n"
        "Confermala (o rifiutala) dal DM di Scheduler."
    )


async def _start_extra_apps(extra_apps: list[Application]) -> None:
    """Bring node-dedicated bots up to "started", WITHOUT polling yet.

    Polling is toggled separately (see _acquire/_release) because who may call
    getUpdates is an ownership question, not a lifecycle one.
    """
    for app in extra_apps:
        await app.initialize()
        if app.post_init:
            await app.post_init(app)
        await app.start()


async def _stop_updater(app: Application) -> None:
    if app.updater is not None and app.updater.running:
        await app.updater.stop()


async def _teardown_app(app: Application, what: str) -> None:
    """Best-effort teardown: a failing step must never skip the ones after it."""

    async def _post_shutdown() -> None:
        if app.post_shutdown:
            await app.post_shutdown(app)

    for name, step in (
        ("updater.stop", lambda: _stop_updater(app)),
        ("stop", app.stop),
        ("post_shutdown", _post_shutdown),
        ("shutdown", app.shutdown),
    ):
        try:
            await step()
        except Exception:
            logger.exception("%s: shutdown step %s failed", what, name)


async def _stop_extra_apps(extra_apps: list[Application]) -> None:
    """One bot failing to stop must not block the others."""
    for app in extra_apps:
        await _teardown_app(app, "extra bot")


async def _run_multi(app: Application, extra_apps: list[Application]) -> None:
    """No-ownership path with extra bots: N pollers, N tokens, one process.

    Mirrors what run_polling() does for the single app (initialize -> post_init
    -> start -> poll -> idle -> teardown) but drives it by hand, because
    run_polling owns the event loop and cannot host a second Application.
    """
    import signal

    await _start_extra_apps(extra_apps)
    await app.initialize()
    if app.post_init:
        await app.post_init(app)
    await app.start()
    await app.updater.start_polling(allowed_updates=Update.ALL_TYPES)
    for extra in extra_apps:
        await extra.updater.start_polling(allowed_updates=Update.ALL_TYPES)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass  # non-Linux host: docker stop won't be graceful, acceptable
    try:
        await stop.wait()
    finally:
        await _stop_extra_apps(extra_apps)
        await _teardown_app(app, "coordinator")


async def _run_with_ownership(
    app: Application,
    own_cfg: ownership.OwnershipConfig,
    extra_apps: list[Application] | None = None,
    domain_apps: dict[str, Application] | None = None,
) -> None:
    """Manual PTB lifecycle: the ownership supervisor starts/stops the updater.

    ``domain_apps`` are bots with their own ownership ladder (OWNERSHIP_DOMAINS):
    each gets a manager of its own, so a device can be first for one bot and
    last for another. The main manager never starts or stops them.

    run_polling() owns initialize/post_init/start/idle/shutdown; here we drive
    those steps ourselves so polling can start and stop while the process (and
    the scheduler + status endpoint) stays alive. PTB invokes post_init/
    post_shutdown only from its run_* helpers, so we call them explicitly.
    """
    import signal

    from telegram.error import Conflict

    extra_apps = list(extra_apps or [])
    domain_apps = dict(domain_apps or {})
    domain_managers: dict[str, ownership.OwnershipManager] = {}
    conflict_tasks: set[asyncio.Task] = set()

    def _poll_error_for(get_manager):
        def _poll_error(exc) -> None:
            # PTB v21 invokes error_callback inside the asyncio network loop
            # (telegram/ext/_utils/networkloop.py), NOT in a thread, so
            # create_task is safe here. Keep a reference so the task can't be
            # garbage-collected mid-flight and its exception gets logged.
            if isinstance(exc, Conflict):
                t = asyncio.create_task(get_manager().on_conflict())
                conflict_tasks.add(t)
                t.add_done_callback(conflict_tasks.discard)

        return _poll_error

    _poll_error = _poll_error_for(lambda: manager)

    async def _acquire() -> None:
        await app.updater.start_polling(
            allowed_updates=Update.ALL_TYPES, error_callback=_poll_error
        )
        # Extra bots poll only while THIS device is the Telegram face: each has
        # its own token, but "who talks to Telegram" is one decision per device
        # — a peer polling the same extra token would 409 exactly like the
        # default one does.
        for extra in extra_apps:
            await extra.updater.start_polling(
                allowed_updates=Update.ALL_TYPES, error_callback=_poll_error
            )
        # Owner-only: telegram face + snapshot/resume. The local agent
        # listeners were already started at on_startup and stay up regardless
        # of ownership, so we do NOT touch them here.
        hub.start_owner_listeners()
        # Owner-only too, and for the same reason: Network delivers a push
        # config IN ADDITION to the SSE stream, so a config held by the standby
        # peer would hand it a second copy of every escalation on the shared
        # telegram-owner identity.
        await hub.register_push_webhooks()
        # Back as the Telegram face: answer what was left unanswered meanwhile.
        _schedule_replay()

    async def _release() -> None:
        if app.updater.running:
            await app.updater.stop()
        for extra in extra_apps:
            try:
                if extra.updater is not None and extra.updater.running:
                    await extra.updater.stop()
            except Exception:
                logger.exception("extra bot updater stop failed")
        # Stop ONLY the owner listeners; the local agent listeners keep running
        # so the agents this node hosts stay reachable while the peer is owner.
        await hub.stop_owner_listeners()
        # Hand the push path back with the polling: a config left behind keeps
        # Network posting our way after we stopped being the Telegram face.
        await hub.unregister_push_webhooks()

    manager = ownership.OwnershipManager(own_cfg, acquire=_acquire, release=_release)
    _OWNERSHIP["manager"] = manager
    _OWNERSHIP["config"] = own_cfg

    def _domain_pair(name: str, dapp: Application):
        async def _acq() -> None:
            await dapp.updater.start_polling(
                allowed_updates=Update.ALL_TYPES,
                error_callback=_poll_error_for(lambda: domain_managers[name]),
            )
            _schedule_replay()

        async def _rel() -> None:
            if dapp.updater is not None and dapp.updater.running:
                await dapp.updater.stop()

        return _acq, _rel

    for name, dapp in domain_apps.items():
        acq, rel = _domain_pair(name, dapp)
        domain_managers[name] = ownership.OwnershipManager(
            ownership.load_config(name), acquire=acq, release=rel
        )
    _OWNERSHIP["domains"] = domain_managers

    await _start_extra_apps(extra_apps + list(domain_apps.values()))
    await app.initialize()
    if app.post_init:
        await app.post_init(app)
    # We boot in STANDBY, so any push config bearing our name is a leftover from
    # a previous life (a crashed owner never gets to release). Clearing it once
    # makes the invariant "a config exists only while polling" self-healing
    # instead of merely well-intentioned. AFTER post_init: that is on_startup,
    # which builds hub's config — before it, this call finds none and no-ops.
    await hub.unregister_push_webhooks()
    server = await ownership.serve_status(manager, domains=domain_managers)
    await app.start()

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass  # non-Linux host: docker stop won't be graceful, acceptable

    logger.info(
        "ownership mode: device=%s priority=%d peer=%s",
        own_cfg.device,
        own_cfg.priority,
        own_cfg.peer_url,
    )
    for name, dmgr in domain_managers.items():
        logger.info(
            "ownership domain %r: priority=%d peers=%s",
            name,
            dmgr.cfg.priority,
            dmgr.cfg.peer_url,
        )
    supervisor = asyncio.create_task(manager.run())
    domain_supervisors = [asyncio.create_task(m.run()) for m in domain_managers.values()]
    try:
        await stop.wait()
    finally:
        # Each step guarded: a failing release must not skip app.shutdown().
        supervisor.cancel()
        for t in domain_supervisors:
            t.cancel()
        server.close()
        await server.wait_closed()
        await _stop_extra_apps(extra_apps + list(domain_apps.values()))
        for dmgr in domain_managers.values():
            for step in (dmgr._release, dmgr.aclose):
                try:
                    await step()
                except Exception:
                    logger.exception("domain shutdown step %s failed", step)
        # _release stops the owner listeners; hub.stop_listeners then cancels
        # the always-on local listeners too (full process shutdown).
        for step in (_release, hub.stop_listeners, manager.aclose, app.stop):
            try:
                await step()
            except Exception:
                logger.exception("shutdown step %s failed", step)
        if app.post_shutdown:
            try:
                await app.post_shutdown(app)
            except Exception:
                logger.exception("post_shutdown failed")
        await app.shutdown()


def build_default_application(reassigned: frozenset[str] = frozenset()) -> Application:
    """The Coordinator bot: the full handler set, exactly as it has always been.

    Extracted out of main() unchanged so it can be built (and asserted on) in a
    test without running a poller.
    """
    # concurrent_updates -> different topics can be processed in parallel, so a
    # long task in one tab doesn't block the others.
    app = (
        telegram_api.apply_api_server(Application.builder())
        .token(TOKEN)
        .concurrent_updates(True)
        .post_init(on_startup)
        .post_shutdown(on_shutdown)
        .build()
    )
    _register_default_handlers(app, reassigned)
    return app


def _register_default_handlers(app: Application, reassigned: frozenset[str] = frozenset()) -> None:
    """Register the Coordinator bot's handlers.

    ``reassigned`` holds project keys whose manifest declares a *configured*
    extra bot; their group surface is registered on that bot instead, so the
    two bots can't both answer in the same chat. It is empty whenever no extra
    bot is configured, which makes this function byte-identical to the code it
    was extracted from — that is the zero-regression contract.
    """
    # Per-instance access through Life (#83): first, before any other group.
    app.add_handler(TypeHandler(Update, life_access_gate), group=-3)
    app.add_handler(CommandHandler("start", cmd_help))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler(["progetti", "menu", "dashboard"], cmd_progetti))
    app.add_handler(CommandHandler("nuovo", cmd_nuovo))
    app.add_handler(CommandHandler("riprendi", cmd_riprendi))
    app.add_handler(CommandHandler(["modello", "model"], cmd_modello))
    app.add_handler(CommandHandler(["compara", "compare"], cmd_compara))
    app.add_handler(CommandHandler("opencodemodel", cmd_opencodemodel))
    app.add_handler(CommandHandler("chiudi", cmd_chiudi))
    app.add_handler(CommandHandler("reset", cmd_reset))
    app.add_handler(CommandHandler("new", cmd_new))
    app.add_handler(CommandHandler("argomenti", cmd_argomenti))
    app.add_handler(CommandHandler("elabora", cmd_elabora))
    app.add_handler(CallbackQueryHandler(on_lesson_now_callback, pattern=rf"^{LESSON_NOW_DATA}$"))
    app.add_handler(CallbackQueryHandler(on_drill_callback, pattern=r"^gx:"))
    app.add_handler(MessageHandler(TOPIC_SERVICE_FILTER, handle_topic_service), group=-2)
    app.add_handler(CommandHandler(["notifiche", "notify"], cmd_notifiche))
    app.add_handler(CommandHandler("pin", cmd_pin))
    app.add_handler(CommandHandler("ricorda", cmd_ricorda))
    app.add_handler(CommandHandler("proponi", cmd_proponi))
    app.add_handler(CommandHandler("hub", hub.cmd_hub))
    app.add_handler(CommandHandler("dispatch", hub.cmd_dispatch))
    app.add_handler(CommandHandler("handoff", cmd_handoff))
    app.add_handler(CommandHandler("pipeline", cmd_pipeline))
    app.add_handler(CommandHandler("migrate", cmd_migrate))
    app.add_handler(CommandHandler("restructure", cmd_restructure))
    app.add_handler(CommandHandler("riorganizza", cmd_riorganizza))
    app.add_handler(CommandHandler("prepara", cmd_prepara))
    app.add_handler(CommandHandler("pulisci", cmd_pulisci))
    app.add_handler(CommandHandler("sospeso", cmd_sospeso))
    app.add_handler(CommandHandler("stato", cmd_stato))
    app.add_handler(CallbackQueryHandler(_on_pulisci_cb, pattern=r"^pu:"))
    app.add_handler(CommandHandler("azzera", cmd_azzera))
    app.add_handler(CallbackQueryHandler(_on_azzera_cb, pattern=r"^az:"))
    app.add_handler(CallbackQueryHandler(on_notif_callback, pattern=r"^notif:"))
    app.add_handler(CallbackQueryHandler(on_pipeline_callback, pattern=r"^pl:"))
    app.add_handler(CallbackQueryHandler(on_confirm_callback, pattern=r"^cf:"))
    app.add_handler(CallbackQueryHandler(_on_task_cb, pattern=r"^tk:"))
    app.add_handler(CallbackQueryHandler(_on_taskgen_cancel_cb, pattern=r"^tg:"))
    app.add_handler(CallbackQueryHandler(_on_reorg_cb, pattern=r"^rg:"))
    app.add_handler(CallbackQueryHandler(_on_prep_cb, pattern=r"^pr:"))
    app.add_handler(CallbackQueryHandler(_on_prep_action_cb, pattern=r"^pa:"))
    app.add_handler(CallbackQueryHandler(_on_remote_card_cb, pattern=r"^cx:"))
    app.add_handler(CallbackQueryHandler(on_rating_callback, pattern=r"^rt:"))
    app.add_handler(CommandHandler("lega", cmd_lega))
    app.add_handler(CallbackQueryHandler(on_group_bind_cb, pattern=r"^gb:"))
    app.add_handler(CallbackQueryHandler(on_dm_route_cb, pattern=r"^dmr:"))
    # Project-group button menus ([[menu]]); before the catch-all on_callback.
    app.add_handler(CallbackQueryHandler(on_group_menu_callback, pattern=r"^gm:"))
    app.add_handler(
        ChatMemberHandler(on_my_chat_member, ChatMemberHandler.MY_CHAT_MEMBER)
    )
    app.add_handler(CallbackQueryHandler(on_callback))
    import manifest

    # Rooms whose whole surface moved to a dedicated bot. Computed HERE, before
    # the capability loop, because those commands must stand down in them too —
    # the group MessageHandler loop below recomputes nothing, it just reuses this.
    moved_chats: set[int] = {
        chat_id
        for chat_id, thread_id, project_key, _agent in manifest.group_bindings()
        if not thread_id and project_key in reassigned
    }

    # --- Capability commands (scope: group and/or command-room) ---
    # Registered BEFORE the Task-5 group MessageHandler loop so a /cmd in a
    # group reliably hits the CommandHandler (the loop's ~filters.COMMAND
    # already excludes commands, but first-registration wins for CommandHandlers).
    for p in list_projects():
        man = manifest.load(p)
        tg = man.telegram
        for cap in man.capabilities:
            handler = _make_capability_handler(p.key, cap.key)
            cmd = _tg_command(cap.key)
            if (
                cap.scope in ("group", "both")
                and tg
                and tg.group_chat_id is not None
                and p.key not in reassigned
            ):
                app.add_handler(
                    CommandHandler(
                        cmd,
                        handler,
                        filters=filters.Chat(chat_id=tg.group_chat_id),
                    )
                )
            # Trust boundary: group-scoped caps are reachable ONLY from the
            # project's group (filters.Chat above). command-room/both caps
            # register globally (no chat filter) and rely on the in-handler
            # _profile/can_access gate (default-deny, same as handle_message)
            # to block unauthorized senders — do not remove that gate.
            #
            # One exception, and it is the whole point of `reassigned`: inside a
            # room a dedicated bot now owns, "no chat filter" means BOTH bots
            # answer the same /cmd — the dedicated bot registers it unfiltered
            # too. Seen live on /contatta in "Casa 🕌": two bozze for one ask.
            # So the handler narrows instead of disappearing, exactly like the
            # catch-alls below: deaf in the moved rooms, unchanged everywhere
            # else, so the command room keeps working.
            if cap.scope in ("command-room", "both"):
                deaf = (
                    ~filters.Chat(chat_id=moved_chats)
                    if moved_chats and p.key in reassigned
                    else None
                )
                app.add_handler(CommandHandler(cmd, handler, filters=deaf))
    # --- Voice notes -> brain queue (#56) ---
    # BEFORE the group loop: its chat-wide handler matches every non-command
    # message, voice included, and handle_project_group_message drops what it
    # cannot read — a note sent in a dedicated group would vanish in silence.
    voice = filters.VOICE & ~filters.Chat(chat_id=moved_chats) if moved_chats else filters.VOICE
    app.add_handler(MessageHandler(voice, handle_voice))
    # Lesson recordings (audio/video/video note/audio-video documents) of groups
    # declaring audio_command. Same position and reasoning as the voice handler;
    # the filter matches nothing elsewhere, so other projects are untouched.
    lesson = LESSON_MEDIA_FILTERS & LESSON_AUDIO_FILTER
    if moved_chats:
        lesson = lesson & ~filters.Chat(chat_id=moved_chats)
    app.add_handler(MessageHandler(lesson, handle_project_audio))
    # --- Task-5 group MessageHandler loop ---
    # Only DEDICATED groups (thread 0) get a chat-wide handler: there the whole
    # group maps to one project. Forum-topic projects (thread != 0) share one
    # chat_id, so a chat-wide filter would let topics steal each other's messages;
    # those route via the generic handle_message + TOPIC_BINDING[chat:topic].
    from functools import partial

    for chat_id, thread_id, project_key, _agent in manifest.group_bindings():
        if thread_id or project_key in reassigned:
            continue
        app.add_handler(
            MessageHandler(
                filters.Chat(chat_id=chat_id) & ~filters.COMMAND,
                partial(handle_project_group_message, project_key=project_key),
            )
        )
    # Reassignment must silence the catch-alls in that chat too, not just move the
    # dedicated handler. The Coordinator's token has privacy mode OFF, so while it
    # remains a member Telegram keeps delivering it every message; handle_message
    # would then find the (chat, 0) binding seed_group_bindings() wrote — that seed
    # is bot-agnostic — and run a SECOND claude session on the same node for the
    # same message. One group, one bot, whoever is still in the room.
    # Registered BEFORE the catch-alls: a migration is a service message, and it
    # is delivered exactly once. See handle_chat_migration for why it matters.
    # Archivio completo: group=-1 gira prima di tutto e non consuma l'update,
    # quindi registra anche cio' che gli handler a valle scartano (comandi,
    # topic senza binding, messaggi fra persone). Vedi archive_message.
    app.add_handler(MessageHandler(filters.ALL, archive_message), group=-1)
    app.add_handler(MessageHandler(filters.StatusUpdate.MIGRATE, handle_chat_migration))
    deaf = ~filters.Chat(chat_id=moved_chats) if moved_chats else None
    photo = filters.PHOTO & deaf if deaf else filters.PHOTO
    text = filters.TEXT & ~filters.COMMAND
    app.add_handler(MessageHandler(photo, handle_photo))
    app.add_handler(MessageHandler(text & deaf if deaf else text, handle_message))


async def handle_chat_migration(update, context) -> None:
    """Follow a group when Telegram promotes it to a supergroup.

    Promotion changes the chat id (``-5480477264`` -> ``-100…``) and happens on
    its own: someone enables topics, makes the group public, or the member count
    grows. Every notification aimed at the old id then fails forever, and the
    symptom is a report that stops arriving — silence, which is indistinguishable
    from "nothing to report". That is the whole failure mode this codebase keeps
    running into, so it is worth following the chat instead of waiting to notice.

    Telegram delivers this service message ONCE. Miss it and the old->new pairing
    is not recoverable from anywhere.
    """
    msg = update.effective_message
    new_id = getattr(msg, "migrate_to_chat_id", None) if msg else None
    if not new_id:
        return                      # the twin event, seen from the new chat
    old_id = update.effective_chat.id
    moved = notif_store.remap_chat(old_id, new_id)
    logger.info("chat %s promossa a supergruppo -> %s; notifiche riportate: %s",
                old_id, new_id, moved or "nessuna")
    # A manifest is a versioned file this process must not rewrite; naming it is
    # the most it can honestly do.
    stale = [p.key for p in projects.list_projects()
             if (tg := manifest.telegram_of(p)) is not None
             and tg.group_chat_id == old_id]
    if stale:
        logger.warning("coordinator.toml da aggiornare a mano (group_chat_id=%s -> %s): %s",
                       old_id, new_id, ", ".join(stale))


def _resolve_bot_instances(
    confs: list[bot_registry.BotConf],
) -> list[bot_registry.BotConf]:
    """Give every node bot the Life instance whose graph it must navigate (#78).

    A node bot is a door to one subtree, and an operator runs several instances
    whose graphs are different. The explicit ``COORDINATOR_BOT_INSTANCE_<NAME>``
    wins; otherwise the instance is derived from the menu root (then the node)
    through Life's own composition — never a hand-written table. An ambiguous
    or unknown node keeps today's behaviour: the device default graph.
    """
    import bus_agents

    out: list[bot_registry.BotConf] = []
    for conf in confs:
        if conf.instance:
            out.append(conf)
            continue
        try:
            iid = bus_agents.instance_for_node(conf.menu_root) or bus_agents.instance_for_node(
                conf.node
            )
        except Exception:
            logger.exception("bot %r: instance derivation failed", conf.name)
            iid = None
        if iid:
            logger.info("bot %r: menu rooted on instance %r", conf.name, iid)
            out.append(dataclasses.replace(conf, instance=iid))
        else:
            out.append(conf)
    return out


def _reassigned_projects(extra: list[bot_registry.BotConf]) -> frozenset[str]:
    """Project keys whose group surface moves off the default bot.

    Only manifests naming a bot that ACTUALLY loaded count: a manifest that
    declares ``bot = "chef"`` while no chef token is configured keeps its
    current Coordinator surface instead of silently going dark.
    """
    if not extra:
        return frozenset()
    names = {c.name for c in extra}
    keys: set[str] = set()
    for name in names:
        keys.update(manifest.projects_for_bot(name))
    return frozenset(keys)


def workspace_audit() -> list[tuple[str, str, str | None]]:
    """``(project_key, declared_workspace, case_variant)`` for unreachable workspaces.

    A workspace that isn't there fails in total silence: Docker creates the mount
    point whatever you name it, so the container starts clean, and the only
    symptom is that ``.claude/coordinator.toml`` "doesn't exist" — the project
    keeps working with an empty manifest, so no group, no pin, no capabilities.
    A node once lived like that: the registry declared its workspace in
    camelCase, the compose mounted it lowercase, and on Linux those are two
    different directories. On Windows it would have worked by accident.

    Deliberately a runtime check and not a test: only the device running the
    container can know what is actually mounted. The case variant is reported
    separately because "not mounted" sends you hunting for a missing line when
    the line is right there, spelled differently.
    """
    from pathlib import Path

    out: list[tuple[str, str, str | None]] = []
    for p in list_projects():
        ws = Path(p.workspace)
        if ws.is_dir():
            continue
        variant = None
        if ws.parent.is_dir():
            variant = next(
                (d.name for d in ws.parent.iterdir() if d.name.lower() == ws.name.lower()),
                None,
            )
        out.append((p.key, str(ws), variant))
    return out


#: ``(bot name, chat id)`` pairs startup positively confirmed are NOT a
#: membership. Read by :func:`_served_by_another_bot`; empty until
#: :func:`ownership_audit` runs, and empty is the safe value (it just means
#: every declaration is honoured, which is today's behaviour).
_ABSENT_OWNERS: set[tuple[str, int]] = set()


def ownership_audit(
    *, timeout: float = 10.0
) -> list[tuple[str, str, int, str]]:
    """``(project_key, bot, chat_id, verdict)`` for every declared handover.

    A manifest that says ``bot = "casa"`` is a handover: it tells the Coordinator
    to stay quiet in that chat because a dedicated bot is answering. That is only
    true while the named bot is actually *in* the chat. When it is not, the
    declaration stops being a handover and becomes a mute button — the room goes
    silent and nothing in the logs says why, because "not replying" is exactly
    what the code was told to do.

    ``getChat`` with the owner's own token answers the question directly: a bot
    that is not a member cannot read the chat and Telegram refuses the call.

    Verdicts are three, not two, and the third one matters: ``"absent"`` is a
    positive answer from Telegram and is recorded in ``_ABSENT_OWNERS`` so we
    keep the room; ``"unknown"`` means the check itself failed (no network, rate
    limit) and is deliberately NOT recorded, so a hiccup at boot cannot hand
    every dedicated room back to the Coordinator and double every reply.
    """
    import json as _json
    import urllib.error
    import urllib.request

    out: list[tuple[str, str, int, str]] = []
    _ABSENT_OWNERS.clear()
    tokens = {c.name: c.token for c in bot_registry.load_extra_bots()}
    for p in list_projects():
        tg = manifest.telegram_of(p)
        if tg is None or tg.group_chat_id is None:
            continue
        if tg.bot == bot_registry.DEFAULT_BOT or tg.bot not in tokens:
            continue
        url = f"{telegram_api.api_root(bot=tg.bot)}/bot{tokens[tg.bot]}/getChat?chat_id={tg.group_chat_id}"
        try:
            with urllib.request.urlopen(url, timeout=timeout) as resp:
                _json.load(resp)
            verdict = "member"
        except urllib.error.HTTPError:
            # Telegram answered, and the answer is "that bot cannot see this chat".
            verdict = "absent"
            _ABSENT_OWNERS.add((tg.bot, tg.group_chat_id))
        except Exception:  # noqa: BLE001 - network/DNS/timeout: we simply do not know
            verdict = "unknown"
        out.append((p.key, tg.bot, tg.group_chat_id, verdict))
    return out


def main() -> None:
    if not TOKEN:
        raise SystemExit("TELEGRAM_BOT_TOKEN is required")
    if not ALLOWED_USER_IDS:
        logger.warning("ALLOWED_USER_IDS empty: the bot will ignore everyone")
    _load_state()
    for key, ws, variant in workspace_audit():
        logger.warning(
            "workspace missing: %s -> %s%s",
            key,
            ws,
            f" (mounted as {variant!r} — case mismatch)" if variant else "",
        )
    for key, owner, chat_id, verdict in ownership_audit():
        if verdict == "member":
            logger.info("handover ok: %s -> bot %r in chat %s", key, owner, chat_id)
        elif verdict == "absent":
            logger.error(
                "handover impossibile: %s dichiara il bot %r, che NON e' nel gruppo %s. "
                "Rispondo io: senza questa eccezione la stanza resterebbe muta.",
                key,
                owner,
                chat_id,
            )
        else:
            logger.warning(
                "handover non verificabile: %s -> bot %r in chat %s; la dichiarazione "
                "resta valida (un errore transitorio non deve cambiare il routing)",
                key,
                owner,
                chat_id,
            )
    seed_group_bindings()

    # Extra, node-dedicated bots. Empty (the default) => everything below is the
    # single-bot code path this file has always run.
    extra_confs = _resolve_bot_instances(bot_registry.load_extra_bots())
    reassigned = _reassigned_projects(extra_confs)

    app = build_default_application(reassigned)

    extra_apps: list[Application] = []
    domain_apps: dict[str, Application] = {}
    own_domains = {
        d.strip().lower()
        for d in os.environ.get("OWNERSHIP_DOMAINS", "").split(",")
        if d.strip()
    }
    for conf in extra_confs:
        try:
            built = build_node_application(conf)
        except Exception:
            # One bad bot must never take the Coordinator down with it.
            logger.exception("failed to build bot %r; skipping it", conf.name)
            continue
        if conf.name in own_domains:
            domain_apps[conf.name] = built
        else:
            extra_apps.append(built)

    logger.info(
        "starting coordinator | registry_source=%s projects=%s allowed_users=%s",
        projects.registry_source(),
        [p.key for p in list_projects()],
        ALLOWED_USER_IDS,
    )
    if extra_apps or domain_apps:
        logger.info(
            "extra telegram bots: %s | groups moved off the default bot: %s",
            [c.name for c in extra_confs],
            sorted(reassigned),
        )
    drift = projects.life_graph_drift()
    if drift and drift["missing_in_coordinator"]:
        # Name the source ACTUALLY in use, not a file that may well not exist
        # here: the old wording always said "projects.local.json", so when the
        # live registry failed to load (issue #35) everyone went looking in a
        # file the compose deliberately does not mount, and the real cause -
        # a registry that never loaded - stayed hidden for a month.
        logger.warning(
            "life_graph_drift: %s active Life node(s) not in the registry "
            "currently in use (%s): %s",
            len(drift["missing_in_coordinator"]),
            projects.registry_source(),
            drift["missing_in_coordinator"],
        )
    own_cfg = ownership.load_config()
    if not own_cfg.enabled:
        # No peers: nobody to share a ladder with, so domain bots simply poll
        # along with the rest.
        extra_apps = extra_apps + list(domain_apps.values())
        if not extra_apps:
            # Single-device deployment: today's blocking path, unchanged.
            app.run_polling(allowed_updates=Update.ALL_TYPES)
            return
        asyncio.run(_run_multi(app, extra_apps))
        return
    asyncio.run(_run_with_ownership(app, own_cfg, extra_apps, domain_apps))


if __name__ == "__main__":
    main()
