"""Replay of user messages that never got a good answer.

the owner's rule: "when you drop out for any reason, or don't answer properly,
when you come back you must always answer". The case that forced it
(2026-10-09): the minipc lost the network from ~09:50 to 14:27; a message sent
to ComUnity at 09:49 got a failed turn on the PC instance (``❌ opencode``) and,
once the minipc was back, the same message was older than the stale limit and
was dropped without a trace. Nobody ever answered.

Then the second rule (2026-10-11): "recupera anche tutto il resto". Not only
group text: voice notes, photos, documents, videos, edited messages, private
chats. The archive now keeps the Telegram message itself (with its file_id),
so a replay can put it back through the very handler that would have served it
live -- no second copy of what a voice note or a photo turns into.

Three append-only logs hold everything needed to notice:

* ``messages.jsonl`` -- every message the bot saw (``archive_message``);
* ``conversations.jsonl`` -- every agent turn, with its prompt, reply and (new)
  the id of the message it answered;
* ``handled.jsonl`` -- messages a handler answered without an agent turn (a
  voice note queued to the brain, a lesson recording acknowledged).

A message is *unanswered* when none of them says it was answered.
``select_unanswered`` is the pure core of that decision (no I/O, no Telegram),
so the rule is testable row by row; the rest of this module is the small
persistent state that makes sure a message is replayed at most
``MAX_ATTEMPTS`` times, even across the instances (PC, minipc, server) that
share ``data/`` through Syncthing.
"""

from __future__ import annotations

import datetime as dt
import glob
import json
import logging
import os
import re
import tempfile
from collections.abc import Callable, Iterable

import atomic_io

logger = logging.getLogger("coordinator.replay")

# Window used when the chat's manifest declares no catch-up window: long enough
# to cover a morning-long outage, short enough not to dig up yesterday's chat.
DEFAULT_WINDOW_SEC = 12 * 3600
MAX_WINDOW_SEC = 24 * 3600
# Un file (registrazione, dispensa) non ha fretta: finche' il suo file_id vale si riprende.
MEDIA_WINDOW_SEC = 6 * 24 * 3600
# A message younger than this may still be in flight (its turn not logged yet):
# leave it to the normal pipeline.
MIN_AGE_SEC = 180
MAX_ATTEMPTS = 2
# State entries older than this are dropped: past MAX_WINDOW_SEC a message is
# never selected again anyway.
STATE_TTL_SEC = 7 * 24 * 3600
STATE_MAX_ITEMS = 2000

PROMPT_PREFIX = (
    "[Messaggio arrivato mentre eri offline o a cui la risposta è fallita: "
    "rispondi ora]"
)

# Status values stored per message key.
DONE = "done"  # replayed and answered
FAILED = "failed"  # attempts exhausted, failure note posted
SKIPPED = "skipped"  # excluded by hand, or the handler chose silence
PENDING = "pending"  # an attempt is running / was interrupted
LOST = "lost"  # media archived without its file: asked to send it again

_FINAL = {DONE, FAILED, SKIPPED, LOST}
_ERROR_MARKERS = ("❌", "⏱")
_WS = re.compile(r"\s+")

# Kinds of archived message that never deserve a replay: a command already got
# (or will get) its own handler, a service line is not something anyone wrote.
_NEVER = {"comando", "servizio"}
_CHAT_TYPES = ("group", "supergroup", "private")


def msg_key(chat, msg_id) -> str:
    return f"{chat}:{msg_id}"


def _norm(s: str) -> str:
    return _WS.sub(" ", s or "").strip().lower()


def _parse_ts(value) -> dt.datetime | None:
    try:
        ts = dt.datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=dt.UTC)
    return ts


def _turn_ok(turn: dict) -> bool:
    out = (turn.get("out") or "").strip()
    return bool(turn.get("ok")) and bool(out) and not out.startswith(_ERROR_MARKERS)


def _needles(text: str) -> list[str]:
    """Pieces of the message that a turn prompt answering it must contain.

    The prompt is the message text, possibly wrapped (``[CMD:/group-message]
    ... Testo: ...``, the replay prefix) or with an inline ``opus/high:`` model
    prefix stripped off. Matching a bounded chunk of the normalised text, and
    the same after a leading ``word:`` prefix, covers all of these.
    """
    n = _norm(text)
    out = [n[:160]]
    head, sep, rest = n.partition(":")
    if sep and rest.strip() and len(head) <= 24 and " " not in head.strip():
        out.append(rest.strip()[:160])
    return [x for x in out if x]


# Registrazioni (lezioni): ognuna e' un lavoro a se', anche se Telegram le manda nello stesso album.
# Trattate come un'unica unita' ne tornava indietro una sola (il 25/10 una lezione di 3 ore persa cosi').
RECORDINGS = frozenset({"vocale", "audio", "videonota", "video"})


def is_media(rec: dict) -> bool:
    return (rec.get("tipo") or "testo") != "testo"


def is_recoverable(rec: dict) -> bool:
    """Can this message be rebuilt well enough to go back through a handler?

    A text needs nothing but its archived fields. A media needs its archived
    Telegram message (``raw``, with the ``file_id``): rows written before the
    archive kept it have only a type and a caption, and the file is gone.
    """
    if rec.get("raw"):
        return True
    return not is_media(rec) and bool((rec.get("testo") or "").strip())


def _sort_key(rec: dict):
    ts = _parse_ts(rec.get("ts"))
    return (ts or dt.datetime.min.replace(tzinfo=dt.UTC), bool(rec.get("modificato")))


def select_unanswered(
    messages: Iterable[dict],
    turns: Iterable[dict],
    *,
    now: dt.datetime,
    window_for: Callable[[int], int] | None = None,
    is_bound: Callable[[int, int], bool] | None = None,
    state: dict | None = None,
    bot_ids: Iterable[int] = (),
    min_age_sec: int = MIN_AGE_SEC,
    handled: Iterable[str] = (),
    all_since: dt.datetime | None = None,
) -> list[dict]:
    """Messages that deserve a replay, oldest first.

    ``messages``/``turns`` are rows of messages.jsonl/conversations.jsonl (in any
    order, duplicates allowed: the Syncthing conflict copies repeat lines).
    ``window_for(chat)`` gives the look-back in seconds; ``is_bound(chat,
    topic)`` says whether that card reaches an agent (a private chat is asked
    with topic 0); ``state`` is the replay state (``{"items": {key: {...}}}``);
    ``handled`` holds the keys a handler answered without an agent turn.

    Every kind of message counts -- text, voice, photo, document, video -- in
    groups and in private chats. An edited message is one message: what comes
    back is its latest version, dated when it was first sent, and it counts as
    answered if any of its versions was. ``all_since`` is the cutover of this
    rule: before it only group text was archived well enough to be judged, so
    anything else older than it is left alone.
    """
    window_for = window_for or (lambda _chat: DEFAULT_WINDOW_SEC)
    is_bound = is_bound or (lambda _chat, _topic: True)
    items = (state or {}).get("items", {})
    bots = set(bot_ids)
    done_keys = set(handled)

    ok_turns: dict[tuple, list[tuple[dt.datetime, str]]] = {}
    for t in turns:
        if not _turn_ok(t):
            continue
        ts = _parse_ts(t.get("ts"))
        if ts is None:
            continue
        if t.get("msg_id") is not None and t.get("chat") is not None:
            done_keys.add(msg_key(t.get("chat"), t.get("msg_id")))
        key = (t.get("chat"), t.get("topic") or 0)
        ok_turns.setdefault(key, []).append((ts, _norm(t.get("in") or "")))

    # Every version of every message, grouped by key.
    versions: dict[str, list[dict]] = {}
    for m in messages:
        chat, mid = m.get("chat"), m.get("msg_id")
        if chat is None or mid is None:
            continue
        versions.setdefault(msg_key(chat, mid), []).append(m)

    # An album is answered as one: the turn carries the first photo's id.
    albums_done: set[tuple] = set()
    for key, rows in versions.items():
        if key in done_keys:
            for r in rows:
                if r.get("media_group_id") and r.get("tipo") not in RECORDINGS:
                    albums_done.add((r.get("chat"), r.get("media_group_id")))

    out: list[tuple[dt.datetime, dict]] = []
    for key, rows in versions.items():
        rows = sorted(rows, key=_sort_key)
        first, last = rows[0], rows[-1]
        chat, mid = last.get("chat"), last.get("msg_id")
        if (last.get("tipo") or "testo") in _NEVER:
            continue
        chat_tipo = last.get("chat_tipo")
        if chat_tipo not in _CHAT_TYPES:
            continue
        if last.get("da_id") in bots:
            continue
        text = (last.get("testo") or "").strip()
        if text.startswith("/") or text.lower().startswith("prod:"):
            continue
        if not text and not is_media(last):
            continue
        ts = _parse_ts(first.get("ts"))
        if ts is None:
            continue
        if all_since is not None and ts < all_since and (
            is_media(last) or chat_tipo == "private"
        ):
            continue
        age = (now - ts).total_seconds()
        try:
            window = min(int(window_for(chat)), MAX_WINDOW_SEC)
            if is_media(last) and last.get("raw"):
                window = MEDIA_WINDOW_SEC
        except Exception:
            window = DEFAULT_WINDOW_SEC
        if age < min_age_sec or age > window:
            continue
        topic = last.get("topic") or 0
        if not is_bound(chat, topic):
            continue
        entry = items.get(key) or {}
        if entry.get("status") in _FINAL:
            continue
        if key in done_keys:
            continue
        mg = last.get("media_group_id")
        if mg and (chat, mg) in albums_done:
            continue
        needles: list[str] = []
        for r in rows:
            needles += _needles(r.get("testo") or "")
        if is_media(last):
            # Legacy turns carry no msg_id, but their prompt names the saved
            # file: ``<stamp>_<chat>_<mid>.jpg`` (photo), ``<date>_<mid>_<name>``
            # (group attachment).
            needles += [f"_{chat}_{mid}.", f"_{mid}_"]
        answered = any(
            t_ts >= ts and any(n in t_in for n in needles)
            for t_ts, t_in in ok_turns.get((chat, topic), ())
        )
        if answered:
            continue
        rec = dict(last)
        rec["ts"] = first.get("ts")
        out.append((ts, rec))
    out.sort(key=lambda p: p[0])
    return [m for _, m in out]


def group_albums(todo: list[dict]) -> list[list[dict]]:
    """Units of replay: an album's photos go back together, as they came."""
    units: list[list[dict]] = []
    by_album: dict[tuple, list[dict]] = {}
    for rec in todo:
        mg = rec.get("media_group_id")
        if not mg or rec.get("tipo") in RECORDINGS:
            units.append([rec])
            continue
        k = (rec.get("chat"), mg)
        if k in by_album:
            by_album[k].append(rec)
            continue
        by_album[k] = [rec]
        units.append(by_album[k])
    return units


def _epoch(value) -> int:
    ts = _parse_ts(value)
    return int(ts.timestamp()) if ts is not None else 0


def build_raw(rec: dict) -> dict | None:
    """The Telegram message, as a dict ``Update.de_json`` accepts, or None.

    Newer rows archive the message itself (``raw``). Older text rows are
    rebuilt from the archived fields -- everything a text handler reads is
    there. An older media row cannot be rebuilt: its file_id was never kept.
    """
    raw = rec.get("raw")
    if raw:
        return json.loads(json.dumps(raw))  # deep copy: never touch the archive row
    if not is_recoverable(rec):
        return None
    chat = {"id": rec["chat"], "type": rec.get("chat_tipo") or "supergroup"}
    if rec.get("chat_nome"):
        chat["title"] = rec["chat_nome"]
    if chat["type"] in ("group", "supergroup") and rec.get("topic"):
        chat["is_forum"] = True
    out: dict = {
        "message_id": rec["msg_id"],
        "date": _epoch(rec.get("ts")),
        "chat": chat,
        "text": rec.get("testo") or "",
    }
    if rec.get("da_id") is not None:
        out["from"] = {
            "id": rec["da_id"],
            "is_bot": False,
            "first_name": rec.get("da") or rec.get("username") or str(rec["da_id"]),
        }
        if rec.get("username"):
            out["from"]["username"] = rec["username"]
    if rec.get("topic"):
        out["message_thread_id"] = rec["topic"]
        out["is_topic_message"] = True
    return out


def cutover(state: dict, device: str, *, now: dt.datetime) -> dt.datetime:
    """When every instance started judging every kind of message.

    Recorded once per device; the effective cutover is the LATEST of them, so
    a row written by a peer still on the old code (no file_id kept) is not
    mistaken for a media that slipped through and can no longer be fetched.
    """
    marks = state.setdefault("since_all", {})
    marks.setdefault(device, now.isoformat(timespec="seconds"))
    parsed = [p for p in (_parse_ts(v) for v in marks.values()) if p is not None]
    return max(parsed) if parsed else now


# --- I/O ---------------------------------------------------------------------


def _siblings(path: str) -> list[str]:
    """``path`` plus its rotated generation and any Syncthing conflict copies.

    Conflict copies are not noise here: on 2026-10-09 the PC's failed turn for
    the ComUnity message lived only in ``conversations.sync-conflict-*.jsonl``.
    """
    stem, ext = os.path.splitext(path)
    found = [path, f"{path}.1"]
    found += sorted(glob.glob(f"{glob.escape(stem)}.sync-conflict-*{ext}"))
    return [p for p in found if os.path.isfile(p)]


def read_jsonl(path: str, *, since: dt.datetime | None = None) -> list[dict]:
    rows: list[dict] = []
    for p in _siblings(path):
        try:
            with open(p, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        row = json.loads(line)
                    except ValueError:
                        continue
                    if since is not None:
                        ts = _parse_ts(row.get("ts"))
                        if ts is None or ts < since:
                            continue
                    rows.append(row)
        except OSError:
            logger.warning("replay: could not read %s", p)
    return rows


def read_handled(path: str, *, since: dt.datetime | None = None) -> set[str]:
    """Keys of messages a handler answered without an agent turn."""
    return {
        msg_key(r.get("chat"), r.get("msg_id"))
        for r in read_jsonl(path, since=since)
        if r.get("chat") is not None and r.get("msg_id") is not None
    }


def load_state(path: str) -> dict:
    """Load the replay state, merging Syncthing conflict copies.

    Merge rule per key: a final status beats a pending one, and the higher
    attempt count wins -- so a copy can only add knowledge, never undo it.
    """
    merged: dict[str, dict] = {}
    since_all: dict[str, str] = {}
    for p in _siblings(path):
        if p.endswith(".1"):
            continue
        try:
            with open(p, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            continue
        for dev, when in (data.get("since_all") or {}).items():
            # The earliest record of a device is when it really switched.
            if dev not in since_all or str(when) < str(since_all[dev]):
                since_all[dev] = when
        for k, v in (data.get("items") or {}).items():
            cur = merged.get(k)
            if cur is None:
                merged[k] = dict(v)
                continue
            if v.get("status") in _FINAL and cur.get("status") not in _FINAL:
                cur["status"] = v["status"]
            cur["attempts"] = max(cur.get("attempts", 0), v.get("attempts", 0))
    out: dict = {"items": merged}
    if since_all:
        out["since_all"] = since_all
    return out


def save_state(path: str, state: dict, *, now: dt.datetime) -> None:
    """Prune old entries and write atomically (temp file + replace)."""
    items = state.get("items", {})
    cutoff = now - dt.timedelta(seconds=STATE_TTL_SEC)
    keep = {}
    for k, v in items.items():
        ts = _parse_ts(v.get("at"))
        if ts is None or ts >= cutoff:
            keep[k] = v
    if len(keep) > STATE_MAX_ITEMS:
        ordered = sorted(keep.items(), key=lambda kv: str(kv[1].get("at", "")))
        keep = dict(ordered[-STATE_MAX_ITEMS:])
    state["items"] = keep
    d = os.path.dirname(path) or "."
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".replayed-", suffix=".tmp", dir=d)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(state, fh, ensure_ascii=False, indent=1)
        atomic_io.replace_with_retry(tmp, path)
    except Exception:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def mark(state: dict, key: str, *, status: str, now: dt.datetime, attempts=None) -> dict:
    entry = state.setdefault("items", {}).setdefault(key, {"attempts": 0})
    entry["status"] = status
    entry["at"] = now.isoformat(timespec="seconds")
    if attempts is not None:
        entry["attempts"] = attempts
    return entry


def prompt_header(message: dict) -> str:
    """What an agent reads before a replayed message: why it is late, from whom."""
    who = message.get("da") or message.get("username") or "qualcuno"
    when = message.get("ts") or ""
    return f"{PROMPT_PREFIX}\n(scritto da {who} alle {when})"


def build_prompt(message: dict) -> str:
    return f"{prompt_header(message)}\n\n{message.get('testo', '').strip()}"
