# bot_registry.py
"""Config-driven registry of the Telegram bots this Coordinator process runs.

Historically there was exactly one bot: ``TELEGRAM_BOT_TOKEN``. That bot is
still here, still called ``default``, and still behaves identically — this
module only *adds* the ability to declare extra, node-dedicated bots:

    COORDINATOR_BOTS=chef,travel,home,health
    TELEGRAM_BOT_TOKEN_CHEF=...
    TELEGRAM_BOT_TOKEN_TRAVEL=...
    COORDINATOR_BOT_NODE_HEALTH=healthTracker  # optional: project key != bot name
    COORDINATOR_BOT_ROOT_HOME=home             # optional: menu root != project
    COORDINATOR_BOT_LABEL_HEALTH=Salute        # optional: menu title
    COORDINATOR_BOT_INSTANCE_HEALTH=personal   # optional: Life instance of the menu

``node`` and ``root`` answer two different questions and only look like one.
``node`` is a **project**: a workspace on disk, where a session actually runs, so
it must be a key of the project registry. ``root`` is a **graph node**: where the
bot's menu starts, and it may be a cluster — a node with children and no repo of
its own. Collapsing the two is what limits a bot to a single leaf: a bot rooted
at a cluster shows the whole subtree and still has one concrete place to run.
``root`` defaults to ``node``, so a bot that is its own root behaves exactly as
before.

An empty/absent ``COORDINATOR_BOTS`` yields an empty list, which is what makes
"no extra bots configured" mean *exactly today's behaviour* upstream.

Failure isolation is deliberate and per-bot: a missing, malformed or duplicated
token disqualifies **that** bot only. It is logged as an error and skipped; the
default bot and every other valid extra bot are unaffected. A bot that would
poll a token another bot already polls is rejected outright — Telegram allows a
single ``getUpdates`` per token and a second poller earns a permanent 409.
"""
from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass

logger = logging.getLogger("coordinator.bot_registry")

DEFAULT_BOT = "default"
BOTS_ENV = "COORDINATOR_BOTS"
TOKEN_ENV = "TELEGRAM_BOT_TOKEN"
TOKEN_ENV_PREFIX = "TELEGRAM_BOT_TOKEN_"
NODE_ENV_PREFIX = "COORDINATOR_BOT_NODE_"
ROOT_ENV_PREFIX = "COORDINATOR_BOT_ROOT_"
LABEL_ENV_PREFIX = "COORDINATOR_BOT_LABEL_"
INSTANCE_ENV_PREFIX = "COORDINATOR_BOT_INSTANCE_"

_NAME_RE = re.compile(r"[a-z0-9][a-z0-9_-]*\Z")
# Telegram bot tokens are "<numeric bot id>:<secret>". Checked loosely — enough
# to catch a placeholder or a shell-mangled value, not so strict that a valid
# token from a future Telegram change gets rejected.
_TOKEN_RE = re.compile(r"\d{5,}:[A-Za-z0-9_-]{20,}\Z")


@dataclass(frozen=True)
class BotConf:
    """One pollable Telegram surface."""

    name: str
    token: str
    node: str  # project key: the workspace a session runs in. Must resolve.
    label: str = ""
    root: str = ""  # graph node the menu starts at; may be a cluster. "" => node
    instance: str = ""  # Life instance whose graph the menu reads; "" => derive

    @property
    def menu_root(self) -> str:
        """Where this bot's menu starts. Falls back to its project."""
        return self.root or self.node

    @property
    def is_default(self) -> bool:
        return self.name == DEFAULT_BOT

    def __repr__(self) -> str:  # never let a token reach a log line
        return f"BotConf(name={self.name!r}, node={self.node!r}, token=***)"


def normalize_name(raw: str) -> str:
    """Canonical bot name, or "" when the input can never be one."""
    name = str(raw or "").strip().lower()
    return name if _NAME_RE.match(name) else ""


def env_suffix(name: str) -> str:
    """The ``<NAME>`` used in TELEGRAM_BOT_TOKEN_<NAME> for this bot."""
    return re.sub(r"[^A-Z0-9]", "_", name.upper())


TOKEN_FILE_ENV = "TELEGRAM_BOT_TOKEN_FILE"


def default_token(env: dict[str, str] | None = None) -> str:
    """The default bot's token: the device secret file when named, else the env.

    ``TELEGRAM_BOT_TOKEN_FILE`` points at a 0600 device secret (the one bot of
    #83 lives in ``$LIFE_HOME/secrets/telegram-coordinator-bot.token`` on every
    device of the ladder), so the token never sits in a ``.env``. When the file
    is named it wins, and an unreadable file yields "" — loudly — rather than a
    silent fallback to whatever token the ``.env`` still carries: polling the
    wrong token is the 409 war this whole ladder exists to prevent.
    """
    env = os.environ if env is None else env
    path = (env.get(TOKEN_FILE_ENV) or "").strip()
    if path:
        try:
            with open(path, encoding="utf-8") as fh:
                return fh.read().strip()
        except OSError as exc:
            logger.error("%s=%s unreadable: %s", TOKEN_FILE_ENV, path, exc)
            return ""
    return (env.get(TOKEN_ENV) or "").strip()


def _valid_token(token: str) -> bool:
    return bool(_TOKEN_RE.match(token))


def load_extra_bots(env: dict[str, str] | None = None) -> list[BotConf]:
    """Every well-formed extra bot declared in the environment.

    Never raises and never returns the default bot. Order follows
    ``COORDINATOR_BOTS`` so startup logs are reproducible.
    """
    env = os.environ if env is None else env
    raw = (env.get(BOTS_ENV) or "").strip()
    if not raw:
        return []

    taken_tokens: dict[str, str] = {}
    dflt = default_token(env)
    if dflt:
        taken_tokens[dflt] = DEFAULT_BOT

    out: list[BotConf] = []
    seen: set[str] = set()
    for chunk in re.split(r"[,\s]+", raw):
        if not chunk:
            continue
        name = normalize_name(chunk)
        if not name:
            logger.error("bot registry: skipping invalid bot name %r", chunk)
            continue
        if name == DEFAULT_BOT:
            logger.error(
                "bot registry: %r is reserved for TELEGRAM_BOT_TOKEN; skipping", DEFAULT_BOT
            )
            continue
        if name in seen:
            logger.error("bot registry: duplicate bot name %r; skipping", name)
            continue
        seen.add(name)

        suffix = env_suffix(name)
        token_var = f"{TOKEN_ENV_PREFIX}{suffix}"
        token = (env.get(token_var) or "").strip()
        if not token:
            logger.error("bot registry: %s is unset/empty; skipping bot %r", token_var, name)
            continue
        if not _valid_token(token):
            logger.error(
                "bot registry: %s does not look like a Telegram token; skipping bot %r",
                token_var,
                name,
            )
            continue
        if token in taken_tokens:
            logger.error(
                "bot registry: %s reuses the token of bot %r — two pollers on one token is a "
                "guaranteed 409 Conflict; skipping bot %r",
                token_var,
                taken_tokens[token],
                name,
            )
            continue
        taken_tokens[token] = name

        node = (env.get(f"{NODE_ENV_PREFIX}{suffix}") or "").strip() or name
        root = (env.get(f"{ROOT_ENV_PREFIX}{suffix}") or "").strip()
        label = (env.get(f"{LABEL_ENV_PREFIX}{suffix}") or "").strip()
        instance = (env.get(f"{INSTANCE_ENV_PREFIX}{suffix}") or "").strip()
        out.append(
            BotConf(
                name=name, token=token, node=node, label=label, root=root,
                instance=instance,
            )
        )

    return out


def configured_names(env: dict[str, str] | None = None) -> frozenset[str]:
    """Names of the extra bots that actually loaded (default excluded)."""
    return frozenset(c.name for c in load_extra_bots(env))
