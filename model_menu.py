"""Multi-level /modello menu (#68): provider -> model -> effort.

Telegram-independent on purpose: keyboards are returned as rows of
``(label, callback_data)`` tuples and bot.py wraps them in
InlineKeyboardButton. That keeps the navigation testable without a bot and
keeps the callback grammar in one place.

callback_data grammar (all under ``mdl``, Telegram cap 64 bytes):

  ``mdl:home``                      level 1 -- providers
  ``mdl:pv:<prov>:<page>``          level 2 -- models of a provider
  ``mdl:cm:<prov>:<alias>``         level 3 -- effort for a Claude model
  ``mdl:ce:<prov>:<alias>:<eff>``   final   -- pin a Claude model+effort
  ``mdl:set:<choice_id>``           final   -- pin a catalog choice (legacy, kept
                                               for messages already sent)
  ``mdl:oca:<page>``                level 2b -- every OpenCode model, paginated
  ``mdl:oc:<hash>``                 final   -- pin an OpenCode model
  ``mdl:auto``                      final   -- back to the default
  ``mdl:odp`` / ``mdl:oda:<page>`` / ``mdl:od:<hash>`` / ``mdl:odauto``
                                    admin   -- /opencodemodel deployment default

OpenCode model ids are carried as a short hash, not verbatim: ids reach ~50
bytes and the provider can ship a longer one tomorrow. The tap resolves the
hash against the current model list, so a stale button fails cleanly instead
of pinning a model nobody offered.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import time
from collections.abc import Iterable
from pathlib import Path
from typing import NamedTuple

logger = logging.getLogger(__name__)

PAGE_SIZE = 8
OPENCODE_CACHE_TTL_SEC = int(os.environ.get("OPENCODE_MODELS_CACHE_SEC", "3600"))
OPENCODE_LIST_TIMEOUT_SEC = 20


class Provider(NamedTuple):
    id: str  # short, goes into callback_data
    label: str
    choice_id: str  # MODEL_CATALOG entry the final pick is pinned to
    key_env: str | None  # None = Claude subscription (OAuth credentials)


PROVIDERS: list[Provider] = [
    Provider("csub", "Claude (abbonamento)", "claude-sub", None),
    Provider("capi", "Claude (API key)", "claude-api", "ANTHROPIC_API_KEY"),
    Provider("ds", "DeepSeek", "deepseek-flash", "DEEPSEEK_API_KEY"),
    Provider("oc", "OpenCode", "opencode", "OPENCODE_API_KEY"),
]
PROVIDER_BY_ID = {p.id: p for p in PROVIDERS}

# Claude level 2: (alias handed to ``claude --model``, button label).
CLAUDE_MODELS: list[tuple[str, str]] = [
    ("opus", "Opus"),
    ("sonnet", "Sonnet"),
    ("haiku", "Haiku"),
    ("fable", "Fable"),
]
# DeepSeek level 2: (catalog choice_id, button label).
DEEPSEEK_MODELS: list[tuple[str, str]] = [
    ("deepseek-flash", "V4 Flash"),
    ("deepseek-pro", "V4 Pro"),
]
EFFORTS: list[str] = ["auto", "low", "medium", "high", "xhigh", "max"]

Row = list[tuple[str, str]]


def claude_subscription_connected() -> bool:
    """True when the Claude CLI here has subscription credentials.

    Either a long-lived ``CLAUDE_CODE_OAUTH_TOKEN`` or a non-empty
    ``.credentials.json`` in the CLI's config dir. Presence is all we check:
    an expired token still shows the button, and the turn then fails loudly --
    better than hiding a provider because of a refresh in flight.
    """
    if os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", "").strip():
        return True
    base = os.environ.get("CLAUDE_CONFIG_DIR") or str(Path.home() / ".claude")
    cred = Path(base) / ".credentials.json"
    try:
        return cred.is_file() and cred.stat().st_size > 0
    except OSError:
        return False


def model_hash(model_id: str) -> str:
    return hashlib.sha1(model_id.encode()).hexdigest()[:10]


def resolve_hash(h: str, models: Iterable[str]) -> str | None:
    for m in models:
        if model_hash(m) == h:
            return m
    return None


# --- opencode models: live list with a 1h cache ------------------------------

_OC_CACHE: dict[str, object] = {"ts": 0.0, "models": [], "top": [], "costs": {}}
_ID_LINE = re.compile(r"^[ \t]*([\w.-]+/[\w.:@+-]+)[ \t]*\r?$", re.M)

# `opencode models` has no notion of relevance: it lists ids alphabetically.
# "The best right now" is a judgment, so it lives here as an ORDER OF FAMILIES
# (claude-opus, gpt-sol...), not of model ids: the family is stable, the
# newest member of each is picked from the live metadata (`--verbose` gives
# family + release_date). A new Opus shows up on its own; only a brand-new
# family needs this list touched -- or OPENCODE_TOP_FAMILIES, comma separated.
TOP_FAMILIES: list[str] = [
    f.strip()
    for f in os.environ.get(
        "OPENCODE_TOP_FAMILIES",
        "claude-opus,gpt-sol,claude-sonnet,gemini-pro,kimi-k3,glm,"
        "deepseek-thinking,grok,deepseek-flash,big-pickle",
    ).split(",")
    if f.strip()
]


class OcModel(NamedTuple):
    id: str  # "opencode/<model>"
    family: str | None
    release: str  # ISO date, "" when unknown
    usable: bool  # status active + tool calls (the bot always runs agentic)
    cost: tuple[float, float] | None = None  # $ per 1M tokens (input, output)


def parse_opencode_models(output: str, prefix: str = "opencode/") -> list[OcModel]:
    """Models from ``opencode models [--verbose]`` output, one provider only.

    Plain output is one id per line; ``--verbose`` follows each id with its
    JSON metadata. Both are accepted, so a CLI that drops ``--verbose`` still
    yields a (metadata-less) list. Only ``opencode/*``: OPENCODE_API_KEY pays
    for those alone, any other provider would be a button that cannot run.
    """
    dec = json.JSONDecoder()
    out: dict[str, OcModel] = {}
    for m in _ID_LINE.finditer(output):
        mid = m.group(1)
        if not mid.startswith(prefix) or mid in out:
            continue
        meta: dict = {}
        j = m.end()
        while j < len(output) and output[j].isspace():
            j += 1
        if output.startswith("{", j):
            try:
                meta, _ = dec.raw_decode(output, j)
            except ValueError:
                meta = {}
        caps = meta.get("capabilities") or {}
        usable = (
            meta.get("status", "active") == "active" and caps.get("toolcall", True) is not False
        )
        out[mid] = OcModel(
            mid, meta.get("family"), str(meta.get("release_date") or ""), usable, _cost(meta)
        )
    return list(out.values())


def _cost(meta: dict) -> tuple[float, float] | None:
    """models.dev prices are $ per 1M tokens; None when the metadata has none."""
    c = meta.get("cost")
    if not isinstance(c, dict):
        return None
    try:
        return float(c.get("input", 0) or 0), float(c.get("output", 0) or 0)
    except (TypeError, ValueError):
        return None


def _price(x: float) -> str:
    return f"{x:g}"


def model_label(model_id: str) -> str:
    """Button text: short id plus the price per 1M tokens (input/output), when
    the live metadata knows it -- the static fallback has no prices."""
    name = model_id.removeprefix("opencode/")
    cost = _OC_CACHE["costs"].get(model_id)  # type: ignore[union-attr]
    if cost is None:
        return name
    cin, cout = cost
    if cin == 0 and cout == 0:
        return f"{name} \u00b7 gratis"
    return f"{name} \u00b7 ${_price(cin)}/${_price(cout)}"


def top_models(models: list[OcModel], families: list[str] | None = None) -> list[str]:
    """Newest usable model of each top family, in the families' order."""
    best: dict[str, OcModel] = {}
    for m in models:
        if m.usable and m.family and (m.family not in best or m.release > best[m.family].release):
            best[m.family] = m
    return [best[f].id for f in (families or TOP_FAMILIES) if f in best]


async def _run_opencode_models(bin_path: str) -> str:
    proc = await asyncio.create_subprocess_exec(
        bin_path,
        "models",
        "opencode",
        "--verbose",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), OPENCODE_LIST_TIMEOUT_SEC)
    except TimeoutError:
        proc.kill()
        raise
    return out.decode("utf-8", "replace")


# opencode reads its catalog from a local models.dev cache that it does not
# refresh on its own: on the minipc it was weeks behind (Opus 5 listed, 5.5
# missing). `--refresh` fixes that but takes ~40s, too long for a button tap,
# so it runs in the background at most once per TTL and the NEXT open reads
# the fresh cache.
_REFRESH: dict[str, object] = {"task": None, "ts": 0.0}
OPENCODE_REFRESH_TIMEOUT_SEC = 180


async def _refresh_models_dev(bin_path: str) -> None:
    try:
        proc = await asyncio.create_subprocess_exec(
            bin_path,
            "models",
            "opencode",
            "--refresh",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            await asyncio.wait_for(proc.wait(), OPENCODE_REFRESH_TIMEOUT_SEC)
        except TimeoutError:
            proc.kill()
            raise
        _OC_CACHE["ts"] = 0.0  # re-read on the next open
    except (TimeoutError, OSError) as exc:
        logger.info("opencode models --refresh failed (%s): keeping the local catalog", exc)


def _maybe_refresh(bin_path: str, now: float) -> None:
    task = _REFRESH["task"]
    if task is not None and not task.done():  # type: ignore[attr-defined]
        return
    if now - float(_REFRESH["ts"]) < OPENCODE_CACHE_TTL_SEC and _REFRESH["ts"]:
        return
    _REFRESH["ts"] = now
    _REFRESH["task"] = asyncio.get_running_loop().create_task(_refresh_models_dev(bin_path))


async def opencode_catalog(bin_path: str, fallback: list[str]) -> tuple[list[str], list[str]]:
    """``(top, all)`` from the live OpenCode catalog, cached for an hour.

    ``all`` is newest first. Without metadata (or without the CLI) ``top``
    falls back to ``fallback``, the static catalog. A failed read is not cached:
    the next open retries.
    """
    now = time.monotonic()
    cached = _OC_CACHE["models"]
    if cached and now - float(_OC_CACHE["ts"]) < OPENCODE_CACHE_TTL_SEC:
        return list(_OC_CACHE["top"]), list(cached)  # type: ignore[arg-type]
    _maybe_refresh(bin_path, now)
    try:
        parsed = parse_opencode_models(await _run_opencode_models(bin_path))
    except (TimeoutError, OSError) as exc:
        logger.warning("opencode models unavailable (%s): using the static catalog", exc)
        parsed = []
    if parsed:
        everything = [m.id for m in sorted(parsed, key=lambda m: m.release, reverse=True)]
        top = (
            top_models(parsed) or [m for m in fallback if m in everything] or everything[:PAGE_SIZE]
        )
        costs = {m.id: m.cost for m in parsed if m.cost is not None}
        _OC_CACHE.update(ts=now, models=everything, top=top, costs=costs)
        return list(top), everything
    if cached:
        return list(_OC_CACHE["top"]), list(cached)  # type: ignore[arg-type]
    return list(fallback), list(fallback)


async def opencode_models(bin_path: str, fallback: list[str]) -> list[str]:
    """Every OpenCode model id (newest first) -- see opencode_catalog."""
    return (await opencode_catalog(bin_path, fallback))[1]


# --- keyboards ---------------------------------------------------------------


def _paginate(items: list, page: int) -> tuple[list, int, int]:
    pages = max(1, (len(items) + PAGE_SIZE - 1) // PAGE_SIZE)
    page = min(max(0, page), pages - 1)
    return items[page * PAGE_SIZE : (page + 1) * PAGE_SIZE], page, pages


def _pager(prefix: str, page: int, pages: int) -> Row:
    row: Row = []
    if page > 0:
        row.append(("◀", f"{prefix}:{page - 1}"))
    if pages > 1:
        row.append((f"{page + 1}/{pages}", f"{prefix}:{page}"))
    if page < pages - 1:
        row.append(("▶", f"{prefix}:{page + 1}"))
    return row


def _mark(selected: bool, label: str) -> str:
    return f"✓ {label}" if selected else label


def kb_providers(available: list[Provider], current_provider: str | None) -> list[Row]:
    rows = [[(_mark(p.id == current_provider, p.label), f"mdl:pv:{p.id}:0")] for p in available]
    rows.append([("↩︎ torna al default", "mdl:auto")])
    return rows


def kb_claude_models(prov: str, current_model: str | None) -> list[Row]:
    rows = [
        [(_mark(alias == current_model, label), f"mdl:cm:{prov}:{alias}")]
        for alias, label in CLAUDE_MODELS
    ]
    rows.append([("⬅ indietro", "mdl:home")])
    return rows


def kb_efforts(prov: str, alias: str, current_effort: str | None) -> list[Row]:
    cur = current_effort or "auto"
    buttons = [(_mark(e == cur, e), f"mdl:ce:{prov}:{alias}:{e}") for e in EFFORTS]
    rows = [buttons[i : i + 3] for i in range(0, len(buttons), 3)]
    rows.append([("⬅ indietro", f"mdl:pv:{prov}:0")])
    return rows


def kb_deepseek(current_choice: str | None) -> list[Row]:
    rows = [
        [(_mark(cid == current_choice, label), f"mdl:set:{cid}")] for cid, label in DEEPSEEK_MODELS
    ]
    rows.append([("⬅ indietro", "mdl:home")])
    return rows


def kb_opencode_top(
    top: list[str],
    current: str | None,
    *,
    pick: str = "mdl:oc",
    more: str = "mdl:oca:0",
    back: tuple[str, str] | None = ("⬅ indietro", "mdl:home"),
) -> list[Row]:
    """The few models worth offering first; the current pick is always shown,
    even when it isn't one of them, so the check mark never disappears."""
    shown = ([current] if current and current not in top else []) + top
    rows = [[(_mark(m == current, model_label(m)), f"{pick}:{model_hash(m)}")] for m in shown]
    rows.append([("altri modelli…", more)])
    if back:
        rows.append([back])
    return rows


def kb_opencode(
    models: list[str],
    current: str | None,
    page: int,
    *,
    pick: str = "mdl:oc",
    pager: str = "mdl:pv:oc",
    back: tuple[str, str] | None = ("⬅ indietro", "mdl:home"),
) -> list[Row]:
    """Paginated OpenCode list. The same keyboard serves the per-card pick and
    the admin /opencodemodel default: only the prefixes change."""
    chunk, page, pages = _paginate(models, page)
    rows = [[(_mark(m == current, model_label(m)), f"{pick}:{model_hash(m)}")] for m in chunk]
    pg = _pager(pager, page, pages)
    if pg:
        rows.append(pg)
    if back:
        rows.append([back])
    return rows
