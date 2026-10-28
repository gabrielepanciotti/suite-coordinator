# remote_cards.py
"""Generic remote-card protocol: a Life node posts a ready card, the bot renders
it without knowing what it is, taps go back to the node's HTTP API (spec §4,
Coordinator#45 / TicketManager#40).

Never imports ``bot.py``: the bot injects ``send_card``/``resolve_target``
into ``_S`` at startup, next to where ``scheduler._S`` is
filled — same discipline as taskgen/butler in ``bot.py``.

Protocol recap (spec §4.1/§4.3)
--------------------------------
``POST /api/cards`` header ``X-Card-Token``:
    {"source", "card_id", "target": {"project_key"}, "text",
     "buttons": [[{"label", "action"}]],
     "attachments": [{"type": "text"|"photo"|"document", ...}]}  # optional
- attachments (TicketManager#15) are sent after the digest, on a dedup miss
  only, as separate messages; a Telegram refusal is reported in
  ``attachments_failed``, never fails the card.
- 403 unknown source, 401 bad token, 400 invalid payload/ids/callback>64B,
  503 the bot is not started, 422 no target to send to.
- Idempotent on ``(source, card_id)``: a repeat returns 200 with the first
  ``message_id`` and sends nothing — checked BEFORE the 503/422 gates, so a
  cached answer never depends on the bot being up.
- The dedup map is persisted (atomic write, cap 500, oldest dropped) so a
  Coordinator restart does not re-send already-delivered cards.

``cx:<source>:<card_id>:<action>`` callback taps are forwarded with
``forward_action`` to ``POST <source url>/api/cards/{card_id}/actions/{action}``
by ``bot._on_remote_card_cb``, which edits the card from the node's answer
(spec §4.2). Any non-200 answer, or no answer at all, becomes a toast.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import logging
import os
import re
import secrets
import tempfile
import time

import httpx

from atomic_io import replace_with_retry
from cb_pack import pack_cb, parse_cb

logger = logging.getLogger("coordinator.remote_cards")

CB_PREFIX = "cx"
ID_RE = re.compile(r"^[a-z0-9-]{1,40}$")

REMOTE_CARDS_PATH = os.environ.get("REMOTE_CARDS_PATH", "/app/data/remote_cards.json")

_DEDUP_CAP = 500

# Injected by bot.py at startup (mirrors scheduler._S). None means "not wired
# yet" — a card posted before the bot is up gets 503, not a crash.
_S: dict = {
    "send_card": None, "resolve_target": None, "edit_card": None, "delete_card": None,
    "send_attachment": None,
}


# ---------------------------------------------------------------------------
# Source registry
# ---------------------------------------------------------------------------

def load_sources(env: dict | None = None) -> dict[str, tuple[str, str]]:
    """``CARD_SOURCES=tm=http://h:10055,other=http://x:1`` -> alias -> (url, token).

    The token for an alias comes from ``CARD_SOURCE_TOKEN_<ALIAS upper>``; an
    alias with no matching token is dropped (logged), never registered with an
    empty/missing secret.
    """
    env = env if env is not None else os.environ
    raw = env.get("CARD_SOURCES", "")
    sources: dict[str, tuple[str, str]] = {}
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry:
            continue
        if "=" not in entry:
            logger.warning("CARD_SOURCES: malformed entry %r, skipping", entry)
            continue
        alias, url = entry.split("=", 1)
        alias, url = alias.strip(), url.strip()
        if not alias or not url:
            logger.warning("CARD_SOURCES: malformed entry %r, skipping", entry)
            continue
        token = env.get(f"CARD_SOURCE_TOKEN_{alias.upper()}")
        if not token:
            logger.warning(
                "CARD_SOURCES: alias %r has no CARD_SOURCE_TOKEN_%s, skipping",
                alias, alias.upper(),
            )
            continue
        sources[alias] = (url, token)
    return sources


# ---------------------------------------------------------------------------
# Callback data codec — cx:<source>:<card_id>:<action>
# ---------------------------------------------------------------------------

def callback_data(source: str, card_id: str, action: str) -> str:
    """Encode Telegram ``callback_data``, raising if any id is malformed or the
    encoded string would exceed Telegram's 64-byte cap."""
    for name, value in (("source", source), ("card_id", card_id), ("action", action)):
        if not ID_RE.match(value):
            raise ValueError(f"invalid {name}: {value!r}")
    return pack_cb(CB_PREFIX, source, card_id, action)


def parse_callback(data: str) -> tuple[str, str, str] | None:
    """Reverse of ``callback_data``; ``None`` on anything malformed (never raises
    — a stale/foreign callback must not crash the dispatcher)."""
    parts = parse_cb(data)
    if len(parts) != 4 or parts[0] != CB_PREFIX:
        return None
    _, source, card_id, action = parts
    if not (ID_RE.match(source) and ID_RE.match(card_id) and ID_RE.match(action)):
        return None
    return source, card_id, action


# ---------------------------------------------------------------------------
# Per-card lock — serializes the load->send->save sequence in show() below
# ---------------------------------------------------------------------------

# One asyncio.Lock per (source, card_id). Never cleaned up: card posts are
# rare (a handful of platforms, a few tickets a day), so the dict stays tiny
# for the life of the process — the same trade-off wake_pool.py makes for its
# per-conv_key locks, minus the refcount bookkeeping wake_pool needs because
# its keyspace (conversations) is much larger and longer-lived.
_dedup_locks: dict[str, asyncio.Lock] = {}


def _lock_for(dedup_key: str) -> asyncio.Lock:
    lock = _dedup_locks.get(dedup_key)
    if lock is None:
        lock = asyncio.Lock()
        _dedup_locks[dedup_key] = lock
    return lock


# ---------------------------------------------------------------------------
# Dedup persistence — data/remote_cards.json, twin of the other data/*.json stores
# ---------------------------------------------------------------------------

def _load_dedup() -> dict[str, int]:
    try:
        with open(REMOTE_CARDS_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return {}
    except Exception:
        logger.exception("could not load remote cards dedup from %s", REMOTE_CARDS_PATH)
        return {}
    if not isinstance(data, dict):
        logger.warning("remote cards dedup at %s is not an object, ignoring",
                        REMOTE_CARDS_PATH)
        return {}
    out: dict[str, int] = {}
    for key, message_id in data.items():
        if isinstance(message_id, int):
            out[str(key)] = message_id
    return out


def _save_dedup(dedup: dict[str, int]) -> None:
    """Persist atomically, capped at 500 entries (oldest inserted dropped)."""
    if len(dedup) > _DEDUP_CAP:
        dedup = dict(list(dedup.items())[-_DEDUP_CAP:])
    d = os.path.dirname(REMOTE_CARDS_PATH) or "."
    if not os.path.isdir(d):
        try:
            os.makedirs(d, exist_ok=True)
        except OSError:
            logger.warning("no writable data dir for remote cards dedup: %s", d)
            return
    tmp = None
    try:
        fd, tmp = tempfile.mkstemp(dir=d, suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(dedup, fh, ensure_ascii=False, indent=2)
        replace_with_retry(tmp, REMOTE_CARDS_PATH)
        tmp = None
    except Exception:
        logger.exception("could not persist remote cards dedup to %s", REMOTE_CARDS_PATH)
    finally:
        if tmp and os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                logger.warning("could not remove temp file %s", tmp)


# ---------------------------------------------------------------------------
# Payload validation
# ---------------------------------------------------------------------------

def _validate_payload(payload: dict) -> tuple[str, str, str, list]:
    """Returns (card_id, project_key, text, buttons) or raises ValueError."""
    card_id = payload.get("card_id")
    if not isinstance(card_id, str) or not ID_RE.match(card_id):
        raise ValueError(f"invalid card_id: {card_id!r}")

    target = payload.get("target")
    if not isinstance(target, dict) or "project_key" not in target:
        raise ValueError(f"invalid target: {target!r}")
    project_key = target["project_key"]
    if not isinstance(project_key, str):
        raise ValueError(f"invalid target.project_key: {project_key!r}")

    text = payload.get("text")
    if not isinstance(text, str) or not text:
        raise ValueError(f"invalid text: {text!r}")

    buttons = payload.get("buttons")
    if not isinstance(buttons, list):
        raise ValueError(f"invalid buttons: {buttons!r}")
    for row in buttons:
        if not isinstance(row, list):
            raise ValueError(f"invalid buttons row: {row!r}")
        for btn in row:
            if not isinstance(btn, dict):
                raise ValueError(f"invalid button: {btn!r}")
            label, action = btn.get("label"), btn.get("action")
            if not isinstance(label, str) or not label:
                raise ValueError(f"invalid button label: {label!r}")
            if not isinstance(action, str) or not ID_RE.match(action):
                raise ValueError(f"invalid button action: {action!r}")

    return card_id, project_key, text, buttons


# ---------------------------------------------------------------------------
# Attachments (TicketManager#15): the preview of a change does not fit in a
# 350-char digest line, so a card may carry text/photo/document messages sent
# on their own, after the digest, in the same chat/topic. They are the record
# of what was proposed: never tracked by the digest, never deleted with it.
# ---------------------------------------------------------------------------

ATTACHMENTS_MAX = 8
ATTACHMENT_TEXT_MAX = 4096                 # Telegram sendMessage cap
ATTACHMENT_CAPTION_MAX = 1024              # Telegram caption cap
ATTACHMENT_PHOTO_MAX_BYTES = 8 * 1024 * 1024
ATTACHMENT_DOCUMENT_MAX_BYTES = 10 * 1024 * 1024
_PHOTO_MAGIC = (b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff")


def _validate_attachments(raw) -> list[dict]:
    """``payload["attachments"]`` -> list of ready-to-send dicts (photo and
    document carry decoded ``data`` bytes). Raises ValueError: a malformed
    attachment is a bug of the node, so the whole card is refused."""
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValueError("invalid attachments: expected a list")
    if len(raw) > ATTACHMENTS_MAX:
        raise ValueError(f"too many attachments: {len(raw)} > {ATTACHMENTS_MAX}")
    out = []
    for i, att in enumerate(raw):
        if not isinstance(att, dict):
            raise ValueError(f"attachments[{i}]: expected an object")
        kind = att.get("type")
        if kind == "text":
            text = att.get("text")
            if not isinstance(text, str) or not text.strip():
                raise ValueError(f"attachments[{i}]: invalid text")
            if len(text) > ATTACHMENT_TEXT_MAX:
                raise ValueError(
                    f"attachments[{i}]: text longer than {ATTACHMENT_TEXT_MAX} characters"
                )
            out.append({"type": "text", "text": text})
            continue
        if kind not in ("photo", "document"):
            raise ValueError(f"attachments[{i}]: unknown type {kind!r}")
        name = att.get("name")
        if not isinstance(name, str) or not name.strip() or len(name) > 255 or "/" in name:
            raise ValueError(f"attachments[{i}]: invalid name {name!r}")
        caption = att.get("caption")
        if caption is not None and not isinstance(caption, str):
            raise ValueError(f"attachments[{i}]: invalid caption")
        if caption and len(caption) > ATTACHMENT_CAPTION_MAX:
            raise ValueError(
                f"attachments[{i}]: caption longer than {ATTACHMENT_CAPTION_MAX} characters"
            )
        b64 = att.get("data_b64")
        if not isinstance(b64, str) or not b64:
            raise ValueError(f"attachments[{i}]: missing data_b64")
        try:
            data = base64.b64decode(b64, validate=True)
        except (binascii.Error, ValueError):
            raise ValueError(f"attachments[{i}]: data_b64 is not valid base64") from None
        if not data:
            raise ValueError(f"attachments[{i}]: empty file")
        limit = ATTACHMENT_PHOTO_MAX_BYTES if kind == "photo" else ATTACHMENT_DOCUMENT_MAX_BYTES
        if len(data) > limit:
            raise ValueError(
                f"attachments[{i}]: {kind} is {len(data)} bytes, limit {limit}"
            )
        if kind == "photo" and not data.startswith(_PHOTO_MAGIC):
            raise ValueError(f"attachments[{i}]: photo must be PNG or JPEG")
        out.append({"type": kind, "name": name, "data": data, "caption": caption or None})
    return out


async def _send_attachments(chat_id: int, thread_id, attachments: list[dict]) -> tuple[int, list]:
    """Send in order; one Telegram refusal never stops the others nor fails
    the card. Returns (sent count, [{"index", "error"}])."""
    send = _S.get("send_attachment")
    sent, failed = 0, []
    for i, att in enumerate(attachments):
        if send is None:
            failed.append({"index": i, "error": "bot cannot send attachments"})
            continue
        try:
            await send(chat_id, thread_id, att)
            sent += 1
        except Exception as exc:
            logger.warning("card attachment %d (%s) not sent: %s", i, att["type"], exc)
            failed.append({"index": i, "error": str(exc)[:300] or type(exc).__name__})
    return sent, failed


# ---------------------------------------------------------------------------
# Digest: one message per destination, not one per card
# ---------------------------------------------------------------------------
# Twenty ticket cards were twenty messages in the private chat: unreadable
# from Telegram. Every open card of a destination (chat, topic) now lives in
# ONE message, edited in place. A new card re-posts it (old one deleted) so it
# still notifies -- at most once every DIGEST_RENOTIFY_SEC, a burst of cards
# notifies once. A card the node closes (``buttons: []``) leaves the digest.
# Cards sent before the digest existed are not in it: their taps keep the
# old one-message-per-card behaviour (bot._on_remote_card_cb).

DIGEST_MAX_CARDS = 15          # 15 cards x their buttons stays under Telegram's caps
DIGEST_CARD_TEXT_MAX = 350
DIGEST_TEXT_MAX = 3900         # Telegram: 4096 chars per message
DIGEST_RENOTIFY_SEC = int(os.environ.get("REMOTE_CARDS_RENOTIFY_SEC", "600"))

_digest_lock = asyncio.Lock()


def _digest_path() -> str:
    return os.environ.get("REMOTE_DIGEST_PATH") or os.path.join(
        os.path.dirname(REMOTE_CARDS_PATH) or ".", "remote_digest.json"
    )


def _load_digests() -> dict:
    try:
        with open(_digest_path(), encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return {}
    except Exception:
        logger.exception("could not load remote card digests")
        return {}
    return data if isinstance(data, dict) else {}


def _save_digests(digests: dict) -> None:
    path = _digest_path()
    tmp = None
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path) or ".", suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(digests, fh, ensure_ascii=False, indent=2)
        replace_with_retry(tmp, path)
        tmp = None
    except Exception:
        logger.exception("could not persist remote card digests to %s", path)
    finally:
        if tmp and os.path.exists(tmp):
            os.remove(tmp)


def _tag(card_id: str) -> str:
    """Short handle shown in the digest and on its buttons: the trailing number
    of the card id (``redmine-43`` -> ``43``), else the id itself."""
    m = re.search(r"(\d+)$", card_id)
    return m.group(1) if m else card_id


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit].rstrip() + "\u2026"


def render_digest(cards: list[dict]) -> tuple[str, list]:
    """(plain text, wired button rows) of a digest. Newest cards win when they
    do not all fit; each card's buttons are one row, labelled with its tag."""
    shown = cards[-DIGEST_MAX_CARDS:]
    parts = [f"\U0001f3ab Card aperte: {len(cards)}"]
    rows = []
    for card in shown:
        tag = _tag(card["card_id"])
        parts.append(f"\n[{tag}] {_clip(card['text'].strip(), DIGEST_CARD_TEXT_MAX)}")
        for row in card.get("buttons") or []:
            wired = [
                {"label": f"{tag} \u00b7 {btn['label']}",
                 "callback_data": callback_data(card["source"], card["card_id"], btn["action"])}
                for btn in row
            ]
            if wired:
                rows.append(wired)
    hidden = len(cards) - len(shown)
    if hidden:
        parts.append(f"\n\u2026 e altre {hidden} più vecchie")
    return _clip("\n".join(parts), DIGEST_TEXT_MAX), rows


async def _publish_digest(entry: dict, *, notify: bool) -> int | None:
    """Show ``entry`` on Telegram; returns the digest's message id (None once
    the digest is empty and its message gone). Raises only if a needed send
    fails -- an edit or delete failure falls back or is logged."""
    chat_id, thread_id = entry["chat_id"], entry.get("thread_id") or 0
    old = entry.get("message_id")
    delete_card = _S.get("delete_card")
    if not entry["cards"]:
        if old and delete_card:
            try:
                await delete_card(chat_id, old)
            except Exception as exc:
                logger.info("empty digest %s not deleted: %s", old, exc)
        entry["message_id"] = None
        return None
    text, rows = render_digest(entry["cards"])
    renotify = notify and time.time() - float(entry.get("posted_at") or 0) >= DIGEST_RENOTIFY_SEC
    edit_card = _S.get("edit_card")
    if old and edit_card and not renotify:
        try:
            await edit_card(chat_id, old, text, rows)
            return old
        except Exception as exc:
            if "not modified" in str(exc).lower():
                return old
            logger.warning("digest edit %s failed (%s), reposting", old, exc)
    message_id = await _S["send_card"](chat_id, thread_id, text, rows)
    entry["message_id"] = message_id
    entry["posted_at"] = time.time()
    if old and old != message_id and delete_card:
        try:
            await delete_card(chat_id, old)
        except Exception as exc:
            logger.info("previous digest %s not deleted: %s", old, exc)
    return message_id


async def _add_to_digest(chat_id: int, thread_id, card: dict) -> int:
    async with _digest_lock:
        digests = _load_digests()
        entry = digests.setdefault(
            f"{chat_id}:{thread_id or 0}",
            {"chat_id": chat_id, "thread_id": thread_id or 0, "cards": []},
        )
        dkey = (card["source"], card["card_id"])
        entry["cards"] = [
            c for c in entry["cards"] if (c["source"], c["card_id"]) != dkey
        ] + [card]
        try:
            message_id = await _publish_digest(entry, notify=True)
        finally:
            _save_digests(digests)
        return message_id


async def apply_answer(source: str, card_id: str, result: dict) -> bool:
    """Apply the node's answer to a tap on a DIGEST card. Returns False when
    the card is not in any digest (a pre-digest card: the caller edits the
    tapped message as before). ``text`` updates the card, ``buttons: []`` (or
    none) closes it, a toast-only answer leaves it as it is."""
    async with _digest_lock:
        digests = _load_digests()
        for entry in digests.values():
            cards = entry.get("cards") or []
            idx = next(
                (i for i, c in enumerate(cards)
                 if c["source"] == source and c["card_id"] == card_id),
                None,
            )
            if idx is None:
                continue
            text = result.get("text")
            if text is None:
                return True
            buttons = [
                [{"label": b["label"], "action": b["action"]}
                 for b in row
                 if isinstance(b, dict) and isinstance(b.get("label"), str)
                 and isinstance(b.get("action"), str) and ID_RE.match(b["action"])]
                for row in (result.get("buttons") or []) if isinstance(row, list)
            ]
            buttons = [row for row in buttons if row]
            if buttons and isinstance(text, str) and text:
                cards[idx] = {**cards[idx], "text": text, "buttons": buttons}
            else:
                cards.pop(idx)
            try:
                await _publish_digest(entry, notify=False)
            except Exception:
                logger.exception("digest update after %s/%s failed", source, card_id)
            _save_digests(digests)
            return True
    return False


# ---------------------------------------------------------------------------
# POST /api/cards
# ---------------------------------------------------------------------------

async def show(payload: dict, token: str | None) -> tuple[int, dict]:
    """Handle an inbound card. Returns (http status, body) for the route to
    wrap in a ``JSONResponse`` — never raises on a malformed/unauthorized
    request, only on a programming error."""
    source = payload.get("source") if isinstance(payload, dict) else None
    sources = load_sources()
    if not isinstance(source, str) or source not in sources:
        return 403, {"error": "unknown source"}

    _, expected_token = sources[source]
    if not token or not secrets.compare_digest(token, expected_token):
        return 401, {"error": "invalid token"}

    try:
        card_id, project_key, text, buttons = _validate_payload(payload)
        attachments = _validate_attachments(payload.get("attachments"))
    except ValueError as exc:
        return 400, {"error": str(exc)}

    dedup_key = f"{source}:{card_id}"
    # The whole load -> (miss) -> send -> save sequence must be atomic per
    # dedup_key: without the lock, two POSTs for the same card arriving close
    # together (a node retrying a slow response) both read a dedup miss before
    # either writes, and both call send_card -> a duplicate Telegram card,
    # which is exactly the idempotency contract spec §4.1 promises against.
    # Per-key (not one global lock): unrelated cards must not serialize behind
    # each other's Telegram round-trip. Same pattern as wake_pool.py's
    # per-conv_key asyncio.Lock.
    async with _lock_for(dedup_key):
        dedup = _load_dedup()
        if dedup_key in dedup:
            return 200, {"ok": True, "message_id": dedup[dedup_key]}

        # Only built on a dedup MISS: a duplicate of an already-accepted card
        # skips this (and its callback_data length check) entirely — it was
        # already validated the first time it was accepted.
        try:
            for row in buttons:
                for btn in row:
                    callback_data(source, card_id, btn["action"])
        except ValueError as exc:
            return 400, {"error": str(exc)}

        send_card = _S.get("send_card")
        if send_card is None:
            return 503, {"error": "bot not started"}

        resolve_target = _S.get("resolve_target")
        target = resolve_target(project_key) if resolve_target else None
        if target is None:
            return 422, {"error": "no target to send to"}
        chat_id, thread_id = target

        card = {"source": source, "card_id": card_id, "text": text, "buttons": buttons}
        message_id = await _add_to_digest(chat_id, thread_id, card)

        dedup[dedup_key] = message_id
        _save_dedup(dedup)
        if not attachments:
            return 200, {"ok": True, "message_id": message_id}
        # After the dedup save: a retry of a card whose attachments were
        # interrupted must not re-post the card (the attachments are lost,
        # the node sees the failure in its log, not a duplicate).
        sent, failed = await _send_attachments(chat_id, thread_id, attachments)
        return 200, {"ok": True, "message_id": message_id,
                     "attachments_sent": sent, "attachments_failed": failed}


# ---------------------------------------------------------------------------
# Tap -> node (spec §4.2)
# ---------------------------------------------------------------------------

# Must stay under Telegram's ~15s callback-query validity window: the bot's
# cx: handler awaits forward_action before it can even attempt q.answer(...),
# so a timeout at or above that window would let the query expire while we
# are still waiting on the node — the tap then looks like a failure to the
# operator even after the node actually executed the action. 12s leaves a
# margin under 15s for the round-trip back through the Telegram API itself.
_FORWARD_TIMEOUT_SEC = 12


async def forward_action(source: str, card_id: str, action: str) -> dict:
    """POST the tap to the node's own API. Returns the node's JSON on a 200,
    or a toast telling the operator the node could not be reached — on a
    network failure AND on any non-200 status (a 401/500 body such as
    ``{"detail": ...}`` is not an answer). A failure here must never crash
    the callback handler nor touch the card."""
    sources = load_sources()
    if source not in sources:
        logger.warning("forward_action: unknown source %r", source)
        return {"toast": f"⚠️ {source} non raggiungibile, riprova"}
    url, token = sources[source]
    try:
        async with httpx.AsyncClient(timeout=_FORWARD_TIMEOUT_SEC) as client:
            resp = await client.post(
                f"{url}/api/cards/{card_id}/actions/{action}",
                headers={"X-Card-Token": token},
            )
            if resp.status_code != 200:
                logger.warning("forward_action to %s: HTTP %s (%s/%s)",
                               source, resp.status_code, card_id, action)
                return {"toast": f"⚠️ {source} non raggiungibile, riprova"}
            return resp.json()
    except Exception:
        logger.exception("forward_action to %s failed (%s/%s)", source, card_id, action)
        return {"toast": f"⚠️ {source} non raggiungibile, riprova"}
