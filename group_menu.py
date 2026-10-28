# group_menu.py
"""Button menu of a project group, declared by the node as ``[[menu]]``.

Pure helpers (no bot state): build the inline keyboard, resolve a tap back to
its item, and phrase the answer of a hint button. ``bot.py`` wires them to
/start, /menu and the ``gm:`` callbacks.
"""
from __future__ import annotations

import json
import re
import shlex

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from manifest import MenuItem

CB_PREFIX = "gm:"
MENU_TITLE = "👋 Cosa vuoi fare? Tocca un bottone:"

# "… — es. /interroga Storia dell'arte" in a capability hint: the typed form.
_EXAMPLE_RE = re.compile(r"\bes\.\s*(/\S.*)$")


def build_keyboard(items: list[MenuItem]) -> InlineKeyboardMarkup:
    """Two buttons per row, ``callback_data = "gm:<key>"``.

    A change of ``section`` between consecutive items starts a new row, so each
    section occupies whole rows.
    """
    rows: list[list[InlineKeyboardButton]] = []
    prev: str | None = None
    for it in items:
        btn = InlineKeyboardButton(it.label, callback_data=f"{CB_PREFIX}{it.key}")
        if not rows or it.section != prev or len(rows[-1]) >= 2:
            rows.append([])
        rows[-1].append(btn)
        prev = it.section
    return InlineKeyboardMarkup(rows)


def resolve(items: list[MenuItem], data: str) -> MenuItem | None:
    """The item a ``gm:<key>`` callback refers to; None if foreign or stale."""
    return resolve_arg(items, data)[0]


def resolve_arg(items: list[MenuItem], data: str) -> tuple[MenuItem | None, str]:
    """``gm:<key>`` or ``gm:<key>:<arg>`` -> (item, arg); (None, "") if foreign or stale."""
    if not data or not data.startswith(CB_PREFIX):
        return None, ""
    key, _, arg = data[len(CB_PREFIX) :].partition(":")
    item = next((it for it in items if it.key == key), None)
    return (item, arg) if item is not None else (None, "")


# A line of a command's output ``[[btn: Label | key:arg]]`` becomes an inline
# button that taps the menu item ``key`` with ``arg`` (callback ``gm:key:arg``).
_BTN_RE = re.compile(r"^\s*\[\[btn:\s*(.+?)\s*\|\s*([A-Za-z0-9_-]{1,40})(?::(.*?))?\s*\]\]\s*$")


def extract_buttons(text: str) -> tuple[str, InlineKeyboardMarkup | None]:
    """Strip ``[[btn: …]]`` lines from ``text``; the rest and one button per row.

    A button whose callback_data would exceed Telegram's 64 bytes is dropped.
    """
    kept: list[str] = []
    rows: list[list[InlineKeyboardButton]] = []
    for line in text.split("\n"):
        m = _BTN_RE.match(line)
        if not m:
            kept.append(line)
            continue
        label, key, arg = m.group(1), m.group(2), m.group(3) or ""
        data = f"{CB_PREFIX}{key}" + (f":{arg}" if arg else "")
        if len(data.encode()) <= 64:
            rows.append([InlineKeyboardButton(label, callback_data=data)])
    body = "\n".join(kept).strip()
    return body, InlineKeyboardMarkup(rows) if rows else None


def hint_text(item: MenuItem, cap_hint: str = "") -> str:
    """Answer of a button with a hint.

    With a ``cmd`` it also reminds how to type the request: the example taken
    from the capability's own hint when it has one, the bare command otherwise.
    """
    if not item.cmd:
        return item.hint
    example = ""
    m = _EXAMPLE_RE.search(cap_hint or "")
    if m and m.group(1).split()[0] == item.cmd:
        example = m.group(1).strip()
    if example:
        tail = f"Scrivi il comando seguito dalla richiesta, es. {example}"
    else:
        tail = f"Scrivi {item.cmd} seguito dalla tua richiesta."
    return f"{item.hint}\n{tail}" if item.hint else tail


# --- Drill-down menus (``source`` on a [[menu]] item) -------------------------
# A source command prints one level as JSON:
#   {"title": "...", "items": [{"label", "path", "leaf", "name"?}], "all_label": "..."|null}
# Callback data carries only a short registry id (``gx:<id>``): paths and the
# breadcrumb trail live server side, Telegram caps callback_data at 64 bytes.
DRILL_PREFIX = "gx:"
PER_PAGE = 6
SOURCE_TIMEOUT_SEC = 30.0
BACK_LABEL = "⬅️ Indietro"


def parse_level(raw: str) -> dict | None:
    """Validate a source's stdout; a normalised level dict or None."""
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("items"), list):
        return None
    items = []
    for it in data["items"]:
        if not isinstance(it, dict):
            return None
        label, path = it.get("label"), it.get("path", "")
        if not isinstance(label, str) or not label.strip() or not isinstance(path, str):
            return None
        name = it.get("name")
        items.append({
            "label": label.strip(),
            "path": path,
            "leaf": bool(it.get("leaf", False)),
            "name": name.strip() if isinstance(name, str) and name.strip() else label.strip(),
        })
    title = data.get("title")
    all_label = data.get("all_label")
    return {
        "title": title.strip() if isinstance(title, str) else "",
        "items": items,
        "all_label": (
            all_label.strip() if isinstance(all_label, str) and all_label.strip() else None
        ),
    }


def page_bounds(n: int, page: int, per: int = PER_PAGE) -> tuple[int, int, int, int]:
    """(start, end, page, pages) with ``page`` clamped to the valid range (0-based)."""
    pages = max(1, -(-n // per))
    page = min(max(0, page), pages - 1)
    start = page * per
    return start, min(n, start + per), page, pages


def breadcrumb(names) -> str:
    return " › ".join(n for n in names if n)


def clean_label(label: str) -> str:
    """"📚 Tutti i corsi" -> "tutti i corsi": leading symbols off, lower-cased."""
    return re.sub(r"^[^\w]+", "", label or "").strip().lower()


def mode_prefix(label: str, crumb: str) -> str:
    return f"[Modalità: {label} — {crumb}]" if crumb else f"[Modalità: {label}]"


def build_command(template: str, path: str) -> list[str]:
    """argv of a source: ``{path}`` substituted per token (spaces stay one arg)."""
    return [tok.replace("{path}", path) for tok in shlex.split(template)]


def build_drill_keyboard(level: dict, page: int, trail: tuple, register) -> InlineKeyboardMarkup:
    """Keyboard of one level. ``register(payload) -> id`` stores the action.

    ``trail`` is the ((path, name), ...) chain from the root to this level.
    """
    rows = []
    if level.get("all_label"):
        rows.append([InlineKeyboardButton(
            level["all_label"],
            callback_data=DRILL_PREFIX + register({"kind": "sel", "trail": trail, "all": True}),
        )])
    items = level["items"]
    start, end, page, pages = page_bounds(len(items), page)
    for it in items[start:end]:
        nxt = trail + ((it["path"], it["name"]),)
        kind = "sel" if it["leaf"] else "nav"
        rows.append([InlineKeyboardButton(
            it["label"],
            callback_data=DRILL_PREFIX + register({"kind": kind, "trail": nxt, "page": 0}),
        )])
    if pages > 1:
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton("◀️", callback_data=DRILL_PREFIX + register(
                {"kind": "nav", "trail": trail, "page": page - 1})))
        indicator = DRILL_PREFIX + register({"kind": "noop"})
        nav.append(InlineKeyboardButton(f"{page + 1}/{pages}", callback_data=indicator))
        if page < pages - 1:
            nav.append(InlineKeyboardButton("▶️", callback_data=DRILL_PREFIX + register(
                {"kind": "nav", "trail": trail, "page": page + 1})))
        rows.append(nav)
    if trail:
        rows.append([InlineKeyboardButton(BACK_LABEL, callback_data=DRILL_PREFIX + register(
            {"kind": "nav", "trail": trail[:-1], "page": 0}))])
    return InlineKeyboardMarkup(rows)
