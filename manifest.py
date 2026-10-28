# manifest.py
"""Per-project Coordinator manifest: <workspace>/.claude/coordinator.toml.

Declares the project's Telegram group, its CLI-backed capabilities (with scope)
and its scheduled notifications. Pure logic — no Telegram imports — discovered
generically like functionalities.py and cached on the file mtime.
"""
from __future__ import annotations

import logging
import os
import re
import shlex
import tomllib
from dataclasses import dataclass, field

import projects
from pipeline_stages import STAGE_TYPES, PipelineStageDecl
from projects import Project

logger = logging.getLogger("coordinator.manifest")

_SCOPES = ("command-room", "group", "both")
_SCHEDULER_KINDS = ("schedulable", "fixed_event", "preference")
_ACTIONS = ("session", "direct_cli", "scheduler_proposal", "team")
# Telegram surface modes a project can declare. Absent => derived from the
# legacy fields, so every manifest written before this existed keeps its
# behaviour exactly (see TelegramConf.effective_mode).
TELEGRAM_MODES = ("dm", "group", "topic")
DEFAULT_BOT = "default"


@dataclass(frozen=True)
class TelegramConf:
    group_chat_id: int | None = None
    group_thread_id: int = 0  # forum topic to bind (0 = dedicated group's General)
    agent: str = "generale"
    pin: str | None = None
    # Where incoming photos land, relative to the workspace root. None keeps the
    # historical "raw/telegram"; a project whose convention differs (e.g. a vault
    # node collecting originals under vault/raw) overrides it in its manifest.
    photo_dir: str | None = None
    # WHICH bot serves this project. "default" is the historical single
    # Coordinator bot (TELEGRAM_BOT_TOKEN); any other name must exist in the
    # bot registry (COORDINATOR_BOTS + TELEGRAM_BOT_TOKEN_<NAME>) or the
    # declaration is ignored and the project stays on the default bot.
    bot: str = DEFAULT_BOT
    # The surface this project exposes. Left empty in the manifest it is DERIVED
    # from the legacy fields, which is what every existing manifest relies on.
    mode: str = ""
    # "Human group" flag: every message in the bound group is fed to the agent
    # (handle_project_group_message), not just commands. None = derive: today
    # only dedicated groups (mode="group") get that chat-wide handler.
    every_message: bool | None = None
    # Project-level model default (#65): a /modello catalog id ("deepseek-pro")
    # or a plain model token ("opus"), plus an optional effort. It sits between
    # the card's sticky /modello and the deployment env default, so a node can
    # be born on a stronger model without anyone typing /modello in each card.
    # Kept as raw strings: the catalog lives in bot.py, not here.
    model: str | None = None
    effort: str | None = None
    # How old an incoming message may be and still get an answer, in hours.
    # None keeps the deployment-wide STALE_MAX_AGE_SEC (10 minutes), which
    # drops whatever piled up while the bot was down. A group that knows the
    # bot sleeps at night (ComUnity: minipc off 23-8) sets it so the morning
    # boot answers the night's messages instead of discarding them silently.
    catch_up_hours: float | None = None
    # Lesson recordings: when set (e.g. "/lezione"), every voice note, audio,
    # video, video note or audio/video document sent to the bound group is
    # saved in the inbox and handed to the group's agent as [CMD:<this>].
    # None keeps today's behaviour (voice -> brain queue, other media ignored).
    audio_command: str | None = None
    # Optional pre-processing of those recordings, run by the bot as a
    # background subprocess BEFORE the agent turn (a 2-hour lesson takes about
    # an hour to transcribe: inside a turn the watchdog would kill it). An argv
    # template with "{file}" (the downloaded recording) and "{out}" (the
    # transcript path under 99_raw/trascrizioni/). None = agent gets the audio.
    audio_preprocess: str | None = None
    # Lesson recordings processed OFF the box (AcademicAssistant #34: a night job
    # on another machine transcribes, corrects and analyses). A directory
    # relative to the workspace: the bot saves the recording as usual, writes
    # ``<dir>/<recording stem>.json`` ({"audio", "argomento", ...}) and stops
    # there: no transcription, no agent turn. Wins over audio_preprocess.
    audio_queue: str | None = None
    # Opt-out of the device NOTIFICATION_DEST_OVERRIDE for this project's
    # `to = "group"` notifications: they go to its own group_chat_id instead.
    notify_in_group: bool = False
    # "day": a chat's session is valid only within the same local day
    # (TZ, default Europe/Rome); the first turn of a new day starts fresh. The
    # idle TTL still applies. None keeps the idle TTL alone.
    session_scope: str | None = None
    # A tap on a group-menu button whose key differs from the last tapped one
    # in that chat starts a new session (switching activity = new context).
    menu_resets_session: bool = False
    # After a menu tap, delete the menu message so the chat stays clean
    # (/menu shows it again).
    menu_autohide: bool = False
    # Relative dir under the workspace where every completed turn of the
    # project's group / bound DM is appended as <dir>/YYYY-MM-DD.md.
    chat_log: str | None = None
    # Forum topic names /argomenti creates when called with no arguments.
    topics: tuple[str, ...] = ()

    @property
    def effective_mode(self) -> str:
        """dm | group | topic — declared if valid, else derived from the legacy fields.

        Derivation reproduces exactly how ``main()`` has always read these
        fields: no group id at all is a DM-only project; a non-zero thread id
        means the project lives as one topic inside a shared forum; anything
        else is a dedicated group.
        """
        if self.mode in TELEGRAM_MODES:
            return self.mode
        if self.group_chat_id is None:
            return "dm"
        return "topic" if self.group_thread_id else "group"

    @property
    def wants_every_message(self) -> bool:
        """Whether the bound chat gets the every-message ingest handler.

        Default matches today's rule in ``main()``: only dedicated groups
        (thread 0) get a chat-wide MessageHandler; forum topics share a chat_id
        and would steal each other's messages.
        """
        if self.every_message is not None:
            return bool(self.every_message)
        return self.effective_mode == "group"


@dataclass(frozen=True)
class Capability:
    key: str
    label: str
    cli: str
    scope: str = "command-room"      # command-room | group | both
    hint: str = ""
    action: str = "session"          # session | scheduler_proposal | direct_cli | team
    kind: str = ""                   # scheduler proposal kind / team command kind
    # "life:use" / "life:propose" / "life:edit" / "life:publish": the Life level
    # this command needs on the chat's instance. Empty = the group's own gate is
    # enough (the historical behaviour).
    requires: str = ""


@dataclass(frozen=True)
class TeamConf:
    """Settings of the Telegram team commands of one instance (#Task 14).

    ``github_repo`` is where ``/segnala`` opens the issue; ``instance`` names the
    Life instance whose config/CLI the wrappers use (default: the chat's
    instance). ``sector_areas`` maps a topic's agent name to the ClickUp areas
    that agent owns, so ``/incarichi`` in "Dev" lists Product and Tech without
    the user typing either slug.
    """

    github_repo: str = ""
    instance: str = ""
    sector_areas: dict[str, tuple[str, ...]] = field(default_factory=dict)


@dataclass(frozen=True)
class NotificationDecl:
    key: str
    title: str
    provider: str                    # agent | static | growbox
    schedule: str
    to: str = "dm"                   # dm | group | chat:<id>[:<thread>]
    prompt: str = ""
    text: str = ""
    project: str = ""                # for to="group"/provider="agent"; default filled by caller
    enabled: bool = True
    gate: str = ""                   # optional shell command; non-zero exit -> skip this run
    recipients: tuple[str, ...] = () # profile keys whose DMs receive a to="dm" notification
    command: str = ""                # provider="command"/"agent": shell run in the node workspace
    timeout_sec: float | None = None # with `command`: kill after N s (default 300)
    params: dict = field(default_factory=dict)  # provider="report": path, max_age_h (``[notification.params]``)


@dataclass(frozen=True)
class MenuItem:
    """One button of a project group's menu (``[[menu]]`` in the manifest).

    ``cmd`` alone runs that command as if typed; ``hint`` answers with a short
    prompt (and, with a ``cmd``, reminds the typed form); at least one is set.
    """
    key: str
    label: str
    cmd: str = ""
    hint: str = ""
    # Drill-down source: a shell command template with "{path}", run in the
    # workspace; it prints the JSON of one menu level (see group_menu.py).
    source: str = ""
    # Section name: consecutive items of the same section share rows, and a
    # change of section starts a new row (e.g. Studio / Calendario / Altro).
    section: str = ""


@dataclass(frozen=True)
class RoutingDecl:
    """What makes a message *about* this node, declared by the node itself.

    Two signals of deliberately unequal weight. ``domains`` is the strong one: a
    link to a rental portal is not ambiguous, and one match is enough to decide.
    ``keywords`` is the weak one — words are shared between siblings ("casa"
    belongs to every node of the cluster), so they can rank candidates but must
    never settle a tie on their own.

    The vocabulary lives in the node that owns it, not in a central table in the
    Coordinator: a table nobody owns is a table nobody updates.
    """

    domains: tuple[str, ...] = ()
    keywords: tuple[str, ...] = ()

    @property
    def is_empty(self) -> bool:
        return not self.domains and not self.keywords


@dataclass(frozen=True)
class DbTargetDecl:
    agent: str
    host: str = ""


@dataclass(frozen=True)
class Manifest:
    telegram: TelegramConf | None = None
    capabilities: list[Capability] = field(default_factory=list)
    team: TeamConf | None = None
    notifications: list[NotificationDecl] = field(default_factory=list)
    pipeline_stages: list[PipelineStageDecl] = field(default_factory=list)
    db_targets: list[DbTargetDecl] = field(default_factory=list)
    routing: RoutingDecl = field(default_factory=RoutingDecl)
    menu: list[MenuItem] = field(default_factory=list)


# cache: workspace -> (toml_mtime, Manifest)
_CACHE: dict[str, tuple[float, Manifest]] = {}


def _toml_path(project: Project) -> str:
    return os.path.join(project.workspace, ".claude", "coordinator.toml")


def toml_path(project: Project) -> str:
    """Dove vive il manifesto di ``project``.

    Pubblica perche' da quando il bot sa legare un gruppo da se' non e' piu' solo
    questo modulo a leggerlo: ci scrive anche ``group_register``. Una seconda
    copia di questo join altrove significherebbe due verita' sul dove.
    """
    return _toml_path(project)


def _parse_telegram(data: dict) -> TelegramConf | None:
    tg = data.get("telegram")
    if not isinstance(tg, dict):
        return None
    gid = tg.get("group_chat_id")
    try:
        gid = int(gid) if gid is not None else None
    except (TypeError, ValueError):
        gid = None
    # Telegram chat ids for groups are negative; 0 is the "not created yet"
    # placeholder that manifests ship with (Launcher's does). Read as a real id
    # it bound a group that does not exist — mode "group" and a
    # set_my_commands at every boot answered with "Chat not found".
    if gid == 0:
        gid = None
    try:
        thread = int(tg.get("group_thread_id", 0) or 0)
    except (TypeError, ValueError):
        thread = 0
    agent = str(tg.get("agent", "generale")).strip() or "generale"
    pin = tg.get("pin")
    pin = str(pin) if pin is not None and str(pin).strip() else None
    return TelegramConf(
        group_chat_id=gid,
        group_thread_id=thread,
        agent=agent,
        pin=pin,
        photo_dir=_parse_photo_dir(tg.get("photo_dir")),
        bot=_parse_bot_name(tg.get("bot")),
        mode=_parse_mode(tg.get("mode")),
        every_message=_parse_every_message(tg.get("every_message")),
        model=_parse_opt_str(tg.get("model")),
        effort=_parse_opt_str(tg.get("effort")),
        catch_up_hours=_parse_catch_up_hours(tg.get("catch_up_hours")),
        audio_command=_parse_audio_command(tg.get("audio_command")),
        audio_preprocess=_parse_audio_preprocess(tg.get("audio_preprocess")),
        audio_queue=_parse_audio_queue(tg.get("audio_queue")),
        notify_in_group=_parse_every_message(tg.get("notify_in_group")) is True,
        session_scope=_parse_session_scope(tg.get("session_scope")),
        menu_resets_session=_parse_every_message(tg.get("menu_resets_session")) is True,
        menu_autohide=_parse_every_message(tg.get("menu_autohide")) is True,
        chat_log=_parse_rel_dir(tg.get("chat_log"), "chat_log"),
        topics=_parse_topics(tg.get("topics")),
    )


SESSION_SCOPES = ("day",)


def _parse_session_scope(raw) -> str | None:
    """"day" or None; anything else is ignored with a warning."""
    txt = _parse_opt_str(raw)
    if txt is None:
        return None
    if txt.lower() not in SESSION_SCOPES:
        logger.warning("coordinator.toml: ignoring invalid telegram.session_scope %r", raw)
        return None
    return txt.lower()


def _parse_rel_dir(raw, key: str) -> str | None:
    """A relative dir inside the workspace (no absolute path, no ``..``)."""
    txt = _parse_opt_str(raw)
    if txt is None:
        return None
    norm = txt.replace("\\", "/").strip("/")
    if (not norm or txt.startswith(("/", "\\")) or re.match(r"^[A-Za-z]:", txt)
            or any(part == ".." for part in norm.split("/"))):
        logger.warning("coordinator.toml: ignoring invalid telegram.%s %r", key, raw)
        return None
    return norm


def _parse_topics(raw) -> tuple[str, ...]:
    """Distinct non-empty topic names (max 128 chars each, Telegram's limit)."""
    if not isinstance(raw, list):
        return ()
    out: list[str] = []
    for item in raw:
        name = str(item).strip()[:128] if item is not None else ""
        if name and name not in out:
            out.append(name)
    return tuple(out)


def _parse_audio_command(raw) -> str | None:
    """A slash command like "/lezione", or None when unset/malformed."""
    txt = _parse_opt_str(raw)
    if txt is None:
        return None
    if not re.fullmatch(r"/[A-Za-z0-9_-]+", txt):
        logger.warning("coordinator.toml: ignoring invalid telegram.audio_command %r", raw)
        return None
    return txt


def _parse_audio_queue(raw) -> str | None:
    """A directory relative to the workspace (no "..", not absolute), or None."""
    txt = _parse_opt_str(raw) if isinstance(raw, str) else None
    if txt is None:
        if raw is not None:
            logger.warning("coordinator.toml: ignoring invalid telegram.audio_queue %r", raw)
        return None
    parts = txt.replace("\\", "/").split("/")
    if txt.startswith(("/", "\\")) or ":" in txt or ".." in parts or not all(parts):
        logger.warning("coordinator.toml: ignoring invalid telegram.audio_queue %r", raw)
        return None
    return txt


def _parse_audio_preprocess(raw) -> str | None:
    """A command template using both {file} and {out}, or None when unset/malformed."""
    if raw is None:
        return None
    txt = raw.strip() if isinstance(raw, str) else ""
    if not txt or "{file}" not in txt or "{out}" not in txt:
        logger.warning("coordinator.toml: ignoring invalid telegram.audio_preprocess %r", raw)
        return None
    try:
        shlex.split(txt)
    except ValueError:
        logger.warning("coordinator.toml: ignoring unparsable telegram.audio_preprocess %r", raw)
        return None
    return txt


# Telegram keeps undelivered updates for 24 hours: a longer window promises
# messages that no longer exist.
CATCH_UP_MAX_HOURS = 24.0


def _parse_catch_up_hours(raw) -> float | None:
    """Positive hours, capped at what Telegram retains; anything else is None."""
    if raw is None or isinstance(raw, bool):
        return None
    try:
        hours = float(raw)
    except (TypeError, ValueError):
        return None
    if hours <= 0:
        return None
    return min(hours, CATCH_UP_MAX_HOURS)


def _parse_opt_str(raw) -> str | None:
    """A trimmed non-empty string, or None."""
    if raw is None:
        return None
    txt = str(raw).strip()
    return txt or None


def _parse_bot_name(raw) -> str:
    """Which bot serves this project; unset/blank keeps the historical default."""
    if raw is None:
        return DEFAULT_BOT
    name = str(raw).strip().lower()
    if not name:
        return DEFAULT_BOT
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", name):
        logger.warning("coordinator.toml: ignoring invalid telegram.bot %r", raw)
        return DEFAULT_BOT
    return name


def _parse_mode(raw) -> str:
    """dm | group | topic, or "" to derive from the legacy fields."""
    if raw is None:
        return ""
    mode = str(raw).strip().lower()
    if not mode:
        return ""
    if mode not in TELEGRAM_MODES:
        logger.warning(
            "coordinator.toml: ignoring unknown telegram.mode %r (expected %s)",
            raw,
            "/".join(TELEGRAM_MODES),
        )
        return ""
    return mode


def _parse_every_message(raw) -> bool | None:
    """Tri-state: True/False when declared, None to derive from the mode."""
    if raw is None:
        return None
    if isinstance(raw, bool):
        return raw
    txt = str(raw).strip().lower()
    if txt in ("1", "true", "yes", "on"):
        return True
    if txt in ("0", "false", "no", "off"):
        return False
    logger.warning("coordinator.toml: ignoring non-boolean telegram.every_message %r", raw)
    return None


def _parse_photo_dir(raw) -> str | None:
    """A workspace-relative subdir, or None when unset/unsafe.

    The manifest lives inside the project, but it is still config the bot writes
    files from: an absolute path or a ``..`` segment would let a manifest push
    downloads outside the workspace it belongs to. Those are dropped (falling
    back to the default) rather than honoured.
    """
    if raw is None:
        return None
    txt = str(raw).strip()
    if not txt:
        return None
    # Deliberately not os.path.isabs: the bot runs on Linux, the tests on
    # Windows, and "what counts as absolute" must not differ between them.
    absolute = txt[0] in "/\\" or (len(txt) > 1 and txt[1] == ":")
    val = txt.replace("\\", "/").strip("/")
    if absolute or not val or ".." in val.split("/"):
        logger.warning("coordinator.toml: ignoring unsafe photo_dir %r", raw)
        return None
    return val


def _parse_capabilities(data: dict, project_key: str) -> list[Capability]:
    out: list[Capability] = []
    for i, e in enumerate(data.get("capability", [])):
        key = str(e.get("key", "")).strip()
        label = str(e.get("label", "")).strip()
        cli = str(e.get("cli", "")).strip()
        action = str(e.get("action", "session")).strip().lower() or "session"
        kind = str(e.get("kind", "")).strip()
        if not (key and label):
            logger.warning("capability #%d in %s missing key/label — skipped",
                           i, project_key)
            continue
        # cli is required only for the default session action (it is the string
        # forwarded to the project's claude session). Action capabilities dispatch
        # in-process instead, so they validate their own required fields.
        if action in ("session", "direct_cli") and not cli:
            logger.warning("capability %r in %s missing cli — skipped", key, project_key)
            continue
        if action not in _ACTIONS:
            logger.warning("capability %r in %s: bad action %r — skipped",
                           key, project_key, action)
            continue
        if action == "scheduler_proposal" and kind not in _SCHEDULER_KINDS:
            logger.warning("capability %r in %s: bad scheduler kind %r — skipped",
                           key, project_key, kind)
            continue
        scope = str(e.get("scope", "command-room")).strip().lower()
        if scope not in _SCOPES:
            scope = "command-room"
        hint = str(e.get("hint", ""))
        requires = str(e.get("requires") or "").strip()
        out.append(Capability(key, label, cli, scope, hint, action, kind, requires))
    return out


def _parse_notifications(data: dict, project: Project) -> list[NotificationDecl]:
    out: list[NotificationDecl] = []
    for i, e in enumerate(data.get("notification", [])):
        key = str(e.get("key", "")).strip()
        provider = str(e.get("provider", "")).strip()
        schedule = str(e.get("schedule", "")).strip()
        if not (key and provider and schedule):
            logger.warning("notification #%d in %s missing key/provider/schedule — skipped",
                           i, project.key)
            continue
        out.append(NotificationDecl(
            key=key,
            title=str(e.get("title", key)).strip() or key,
            provider=provider,
            schedule=schedule,
            to=str(e.get("to", "dm")).strip() or "dm",
            prompt=str(e.get("prompt", "")),
            text=str(e.get("text", "")),
            project=str(e.get("project", "")).strip() or project.key,
            enabled=bool(e.get("enabled", True)),
            gate=str(e.get("gate", "")).strip(),
            recipients=_parse_recipients(e.get("recipients")),
            command=str(e.get("command", "")).strip(),
            timeout_sec=_parse_timeout(e.get("timeout_sec"), key),
            params=_parse_params(e.get("params")),
        ))
    return out


def _parse_params(raw) -> dict:
    """The ``[notification.params]`` table as a plain dict (anything else: empty)."""
    return dict(raw) if isinstance(raw, dict) else {}


def _parse_timeout(raw, key: str) -> float | None:
    """``timeout_sec`` as a number, or None; the range is checked by validate()."""
    if raw is None or isinstance(raw, bool):
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        logger.warning("notification %s: timeout_sec %r is not a number — ignored", key, raw)
        return None


def _parse_recipients(raw) -> tuple[str, ...]:
    """Profile keys from ``recipients = [...]``; blanks dropped, order kept."""
    if not isinstance(raw, list):
        return ()
    keys = (str(r).strip() for r in raw)
    return tuple(dict.fromkeys(k for k in keys if k))


_MENU_KEY_RE = re.compile(r"^[A-Za-z0-9_-]{1,40}$")  # fits the 64-byte callback_data
_MENU_CMD_RE = re.compile(r"^/[A-Za-z0-9_]+$")


def _parse_menu(data: dict, project_key: str) -> list[MenuItem]:
    out: list[MenuItem] = []
    seen: set[str] = set()
    for i, e in enumerate(data.get("menu", [])):
        if not isinstance(e, dict):
            continue
        key = str(e.get("key", "")).strip()
        label = str(e.get("label", "")).strip()
        cmd = str(e.get("cmd", "")).strip()
        hint = str(e.get("hint", "")).strip()
        if not (key and label) or not _MENU_KEY_RE.match(key) or key in seen:
            logger.warning("menu #%d in %s: missing/invalid/duplicate key or label — skipped",
                           i, project_key)
            continue
        if cmd and not _MENU_CMD_RE.match(cmd):
            logger.warning("menu %r in %s: invalid cmd %r — skipped", key, project_key, cmd)
            continue
        if not (cmd or hint):
            logger.warning("menu %r in %s: neither cmd nor hint — skipped", key, project_key)
            continue
        source = str(e.get("source", "") or "").strip()
        if source:
            try:
                ok = "{path}" in source and bool(shlex.split(source))
            except ValueError:
                ok = False
            if not ok or not cmd:
                logger.warning("menu %r in %s: invalid source %r (needs {path} and a cmd)"
                               " — source ignored", key, project_key, source)
                source = ""
        section = str(e.get("section", "") or "").strip()
        seen.add(key)
        out.append(MenuItem(key, label, cmd, hint, source, section))
    return out


def _parse_pipeline_stages(data: dict, project_key: str) -> list[PipelineStageDecl]:
    out: list[PipelineStageDecl] = []
    for i, e in enumerate(data.get("pipeline_stage", [])):
        name = str(e.get("name", "")).strip()
        typ = str(e.get("type", "")).strip().lower()
        if not name or typ not in STAGE_TYPES:
            logger.warning("pipeline_stage #%d in %s missing name / bad type — skipped",
                           i, project_key)
            continue
        agent = str(e.get("agent", "")).strip()
        args = e.get("args") if isinstance(e.get("args"), dict) else {}
        out.append(PipelineStageDecl(name=name, type=typ, agent=agent, args=dict(args)))
    return out


def _parse_db_targets(data: dict, project_key: str) -> list[DbTargetDecl]:
    out: list[DbTargetDecl] = []
    for i, e in enumerate(data.get("db_target", [])):
        agent = str(e.get("agent", "")).strip()
        if not agent:
            logger.warning("db_target #%d in %s missing agent — skipped", i, project_key)
            continue
        host = str(e.get("host", "")).strip() or agent
        out.append(DbTargetDecl(agent=agent, host=host))
    return out


def _parse_routing(data: dict, project_key: str) -> RoutingDecl:
    """``[routing]``: what makes a message about this node.

    A malformed block is reported and dropped rather than half-read: a routing
    table that silently lost half its entries is worse than none, because the
    misses look like "this node just wasn't relevant".
    """
    raw = data.get("routing")
    if raw is None:
        return RoutingDecl()
    if not isinstance(raw, dict):
        logger.warning("[routing] in %s is not a table — ignored", project_key)
        return RoutingDecl()

    def _strings(field_name: str) -> tuple[str, ...]:
        value = raw.get(field_name, [])
        if not isinstance(value, list):
            logger.warning(
                "routing.%s in %s is not a list — ignored", field_name, project_key
            )
            return ()
        out, dropped = [], 0
        for item in value:
            text = str(item).strip().lower()
            if text:
                out.append(text)
            else:
                dropped += 1
        if dropped:
            logger.warning(
                "routing.%s in %s: %d empty entr%s dropped",
                field_name,
                project_key,
                dropped,
                "y" if dropped == 1 else "ies",
            )
        return tuple(dict.fromkeys(out))  # de-duplicated, order preserved

    return RoutingDecl(domains=_strings("domains"), keywords=_strings("keywords"))


def load(project: Project) -> Manifest:
    """Parsed manifest for the project. Empty Manifest if absent/unreadable/malformed."""
    path = _toml_path(project)
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return Manifest()
    cached = _CACHE.get(project.workspace)
    if cached and cached[0] == mtime:
        return cached[1]
    try:
        with open(path, "rb") as fh:
            data = tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        logger.warning("coordinator.toml unreadable for %s: %s", project.key, exc)
        return Manifest()
    man = Manifest(
        telegram=_parse_telegram(data),
        capabilities=_parse_capabilities(data, project.key),
        team=_parse_team(data),
        notifications=_parse_notifications(data, project),
        pipeline_stages=_parse_pipeline_stages(data, project.key),
        db_targets=_parse_db_targets(data, project.key),
        routing=_parse_routing(data, project.key),
        menu=_parse_menu(data, project.key),
    )
    _CACHE[project.workspace] = (mtime, man)
    return man


def telegram_of(project: Project) -> TelegramConf | None:
    return load(project).telegram


def _parse_team(data: dict) -> TeamConf | None:
    raw = data.get("team")
    if not isinstance(raw, dict):
        return None
    mapping: dict[str, tuple[str, ...]] = {}
    for agent, areas in (raw.get("sector_areas") or {}).items():
        if isinstance(areas, list):
            mapping[str(agent)] = tuple(str(a) for a in areas if str(a).strip())
    return TeamConf(
        github_repo=str(raw.get("github_repo") or "").strip(),
        instance=str(raw.get("instance") or "").strip(),
        sector_areas=mapping,
    )


def capabilities_of(project: Project) -> list[Capability]:
    return load(project).capabilities


def team_of(project: Project) -> TeamConf | None:
    return load(project).team


def notifications_of(project: Project) -> list[NotificationDecl]:
    return load(project).notifications


def menu_of(project: Project) -> list[MenuItem]:
    return load(project).menu


def menu_binding(chat_id: int, thread_id: int = 0) -> tuple[str, list[MenuItem]] | None:
    """(project_key, menu) of the project whose group is ``(chat_id, thread_id)``.

    Only projects that declare ``[[menu]]`` count; None keeps the historical
    /start and /menu. A forum-topic project matches only inside its own topic.
    """
    for p in projects.list_projects():
        tg = telegram_of(p)
        if tg is None or tg.group_chat_id != chat_id:
            continue
        if tg.group_thread_id and tg.group_thread_id != thread_id:
            continue
        items = menu_of(p)
        if items:
            return p.key, items
    return None


def pipeline_stages_of(project: Project) -> list[PipelineStageDecl]:
    return load(project).pipeline_stages


def db_targets_of(project: Project) -> list[DbTargetDecl]:
    return load(project).db_targets


def routing_of(project: Project) -> RoutingDecl:
    return load(project).routing


def group_bindings() -> list[tuple[int, int, str, str]]:
    """(group_chat_id, group_thread_id, project_key, agent) per project with a group.

    ``group_thread_id`` is 0 for a dedicated group (bind its General topic) or the
    forum topic id when the project lives as one topic inside a shared forum.
    """
    out: list[tuple[int, int, str, str]] = []
    for p in projects.list_projects():
        tg = telegram_of(p)
        if tg is not None and tg.group_chat_id is not None:
            out.append((tg.group_chat_id, tg.group_thread_id, p.key, tg.agent))
    return out


def catch_up_seconds(chat_id: int) -> int | None:
    """Longest catch-up window declared by any project bound to ``chat_id``.

    One shared forum hosts several projects; the window belongs to the chat,
    not the topic, because the age check runs before topic routing. None means
    no project asked for one and the global stale limit applies.
    """
    best: float | None = None
    for p in projects.list_projects():
        tg = telegram_of(p)
        if tg is None or tg.group_chat_id != chat_id or tg.catch_up_hours is None:
            continue
        best = tg.catch_up_hours if best is None else max(best, tg.catch_up_hours)
    return int(best * 3600) if best is not None else None


def audio_binding(chat_id: int) -> tuple[str, str] | None:
    """(project_key, audio_command) of the project whose group is ``chat_id``.

    Only projects that declare ``telegram.audio_command`` count; None means the
    chat keeps the historical media handling.
    """
    for p in projects.list_projects():
        tg = telegram_of(p)
        if tg is not None and tg.group_chat_id == chat_id and tg.audio_command:
            return p.key, tg.audio_command
    return None


def audio_command_for(chat_id: int) -> str | None:
    """The lesson-audio command declared by the group's project, if any."""
    found = audio_binding(chat_id)
    return found[1] if found else None


def bot_bindings() -> list[tuple[int, int, str, str, str]]:
    """Like :func:`group_bindings` plus the bot name that serves each project.

    Kept separate rather than widening ``group_bindings``'s tuple: that one is
    consumed by the default bot's registration path, which must stay exactly as
    it is (see the zero-regression test).
    """
    out: list[tuple[int, int, str, str, str]] = []
    for p in projects.list_projects():
        tg = telegram_of(p)
        if tg is not None and tg.group_chat_id is not None:
            out.append((tg.group_chat_id, tg.group_thread_id, p.key, tg.agent, tg.bot))
    return out


def projects_for_bot(bot_name: str) -> list[str]:
    """Project keys whose manifest declares ``telegram.bot = bot_name``."""
    return [
        p.key
        for p in projects.list_projects()
        if (tg := telegram_of(p)) is not None and tg.bot == bot_name
    ]


def group_project_for_chat(chat_id: int) -> tuple[str, TelegramConf] | None:
    """(project_key, telegram conf) of the project whose dedicated group is ``chat_id``.

    Dedicated groups only (thread 0): a shared forum hosting several projects as
    topics has no single owner, so it is not "a project group" here.
    """
    for p in projects.list_projects():
        tg = telegram_of(p)
        if tg is not None and tg.group_chat_id == chat_id and not tg.group_thread_id:
            return p.key, tg
    return None
