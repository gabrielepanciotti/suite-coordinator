"""Controllo unico sui messaggi Telegram in uscita.

Un solo punto: ``install()`` avvolge ``telegram.Bot.send_message`` (``reply_text``
passa da li'), quindi copre tutti i ~230 punti di invio di bot.py e hub.py senza
toccarli uno a uno. Un testo vuoto o senza senso non parte: finisce nel log con il
motivo, cosi' si vede che cosa stava uscendo e da dove.

Modulo foglia: non importa nulla del progetto (stesso criterio di project_model).
"""
from __future__ import annotations

import functools
import html
import json
import logging
import os
import re
import secrets
import time
from collections import OrderedDict

logger = logging.getLogger("coordinator")

# Stesso valore di hub.NO_REPLY_MARKER: duplicato apposta, hub importa bot e
# questo modulo deve restare una foglia.
NO_REPLY_MARKER = "[[NESSUNA_RISPOSTA]]"

DUP_WINDOW_SEC = int(os.environ.get("MESSAGE_GUARD_DUP_SEC", "60"))
DUP_MIN_LEN = 12  # sotto, "ok"/"Fatto" ripetuti sono risposte legittime
_DUP_MAX_ENTRIES = 512

_TAG_RE = re.compile(r"<[^>]*>")
_INVISIBLE_RE = re.compile(r"[​-‏⁠⠀﻿­]")
_CODE_RE = re.compile(r"```.*?```|<pre>.*?</pre>|<code>.*?</code>|`[^`\n]*`", re.DOTALL)
# Segnaposto di formattazione rimasti nel testo: {nome}, %s, %(x)s, ${x}, {{x}}.
_PLACEHOLDER_RE = re.compile(r"\{\{?\s*[A-Za-z_][\w.]*\s*\}?\}|%\(\w+\)[sd]|(?<![\w%])%[sd]\b|\$\{[^}]*\}")
_PLACEHOLDER_MAX_LEN = 200  # in un testo lungo `{x}` e' quasi sempre contenuto vero
_SENTINELS = frozenset({
    "none", "null", "nil", "undefined", "nan", "n/a",
    "{}", "[]", "()", "''", '""', "(empty)", "(vuoto)", "(no output)",
   
    "(nessun output)", "no output", "nessun output",
    "<empty>", "<none>", "<vuoto>",
    # Marcatore di silenzio senza le parentesi: l'agente a volte le perde (#93).
    "nessuna_risposta",
})
_RUN_RE = re.compile(r"([^\s\-=_*#.~\u2500-\u259F])\1{29,}")  # 30 volte lo stesso carattere
_PUNCT_ONLY_RE = re.compile(r"^[\s!-/:-@\[-`{-~]*$")  # solo spazi e punteggiatura ASCII

# (chat, thread, testo normalizzato) -> istante dell'ultimo invio.
_recent: "OrderedDict[tuple, float]" = OrderedDict()

stats: dict[str, int] = {}

# --- Registro degli invii e pulizia (/pulisci) -----------------------------
# Telegram non elenca lo storico di una chat e un bot cancella solo entro 48h:
# per poter ripulire serve sapere cosa abbiamo mandato. Una riga per messaggio
# effettivamente inviato; si rivalutano con le regole *correnti*, quindi una
# regola stretta dopo permette di ripulire anche cio' che prima passava.
DELETE_WINDOW_SEC = 48 * 3600  # limite Telegram per i messaggi di un bot
_SENT_MAX_BYTES = 2_000_000
_sent_path: str | None = None
_pending: dict[str, tuple[int, list[int]]] = {}


def configure(sent_log_path: str | None) -> None:
    """Dove tenere il registro degli invii. ``None`` = nessun registro."""
    global _sent_path
    _sent_path = sent_log_path


def _write_sent(rows: list[dict]) -> None:
    with open(_sent_path, "w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")


# --- Messaggi di servizio: spariscono dopo 24h --------------------------------
# Errori, avvisi e «sto pensando» servono sul momento; dopo un giorno nei gruppi deve restare
# solo cio' che e' utile. Si riconoscono dal simbolo con cui iniziano o dal contenuto vuoto.
NOISE_AFTER_SEC = 24 * 3600
_NOISE_PREFIXES = ("❌", "⛔", "⏱", "⚠", "😕", "🔒", "⏳", "🚨", "🔄", "💤")
_NOISE_WORDS = ("nessun output", "nessun messaggio", "empty response", "non sono riuscito",
                "non è arrivato", "non mi è arrivato", "non appartiene a")


def is_noise(text) -> bool:
    if not isinstance(text, str):
        return False
    visible = _visible(text).lower()
    if visible.startswith(tuple(p.lower() for p in _NOISE_PREFIXES)):
        return True
    return len(visible) < 200 and any(w in visible for w in _NOISE_WORDS)


def due_noise(now: float | None = None) -> list[dict]:
    """Messaggi di servizio piu' vecchi di 24h ancora nel registro (ultimo giorno utile: 48h)."""
    now = time.time() if now is None else now
    return [r for r in _read_sent()
            if (r.get("noise") or ("noise" not in r and is_noise(r.get("text")))) and now - r["ts"] >= NOISE_AFTER_SEC]


def _record(chat_id, thread_id, message_id, text: str, bot_id=None) -> None:
    if not _sent_path or not isinstance(message_id, int):
        return
    try:
        if os.path.exists(_sent_path) and os.path.getsize(_sent_path) > _SENT_MAX_BYTES:
            _write_sent([r for r in _read_sent() if time.time() - r["ts"] < DELETE_WINDOW_SEC])
        with open(_sent_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(
                {"ts": time.time(), "chat": chat_id, "thread": thread_id or 0,
                 "id": message_id, "text": text[:2000], "bot": bot_id,
                 "noise": is_noise(text)}, ensure_ascii=False) + "\n")
    except OSError:
        logger.exception("registro invii non scrivibile: %s", _sent_path)


def _read_sent() -> list[dict]:
    if not _sent_path or not os.path.exists(_sent_path):
        return []
    out = []
    with open(_sent_path, encoding="utf-8") as fh:
        for line in fh:
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
    return out


def find_bad(chat_id, now: float | None = None) -> list[dict]:
    """Messaggi cancellabili di ``chat_id`` che le regole di oggi non farebbero partire.

    Dei duplicati si tiene il primo. Solo entro 48h, oltre Telegram rifiuta.
    """
    now = time.time() if now is None else now
    seen: dict[tuple, float] = {}
    bad = []
    for r in sorted(_read_sent(), key=lambda r: r["ts"]):
        if r["chat"] != chat_id or now - r["ts"] >= DELETE_WINDOW_SEC:
            continue
        cleaned, reason = inspect(r["text"])
        if cleaned is None:
            bad.append({**r, "reason": reason})
            continue
        visible = _visible(cleaned)
        if len(visible) >= DUP_MIN_LEN:
            key = (r["thread"], visible)
            last = seen.get(key)
            seen[key] = r["ts"]
            if last is not None and r["ts"] - last < DUP_WINDOW_SEC:
                bad.append({**r, "reason": "duplicato"})
    return bad


def stash_pending(chat_id: int, ids: list[int]) -> str:
    """Parcheggia gli id da cancellare e torna un token corto per il callback."""
    token = secrets.token_hex(4)
    _pending[token] = (chat_id, ids)
    while len(_pending) > 20:
        _pending.pop(next(iter(_pending)))
    return token


def take_pending(token: str) -> tuple[int, list[int]] | None:
    return _pending.pop(token, None)


def forget(chat_id, ids) -> None:
    """Toglie dal registro i messaggi cancellati."""
    if not _sent_path:
        return
    gone = set(ids)
    _write_sent([r for r in _read_sent() if not (r["chat"] == chat_id and r["id"] in gone)])


def _visible(text: str) -> str:
    """Il testo come lo vedrebbe chi lo legge: senza tag, entita', invisibili."""
    return _INVISIBLE_RE.sub("", html.unescape(_TAG_RE.sub("", text))).strip()


# Un agente di notifica che parla di se' invece di fare il lavoro (26/10: ~45 messaggi
# «Non ho inviato nessun messaggio: il comando richiede un'approvazione…» nel gruppo di
# a member). Solo per i risultati delle notifiche agent: in una chat normale sono frasi lecite.
_AGENT_META_RE = re.compile(
    r"non ho inviato|richiede un.approvazione|stringa vuota|the command produced no output",
    re.IGNORECASE)


def is_agent_meta(text) -> bool:
    """Il risultato di una notifica agent e' meta-testo (o un frammento come «K»)?"""
    visible = _visible(text) if isinstance(text, str) else ""
    return len(visible) < 3 or bool(_AGENT_META_RE.search(visible))


def _repeated_lines(visible: str) -> bool:
    lines = [ln.strip() for ln in visible.splitlines() if ln.strip()]
    if len(lines) < 5:
        return False
    run = 1
    for prev, cur in zip(lines, lines[1:]):
        run = run + 1 if cur == prev else 1
        if run >= 5:
            return True
    return False


def inspect(text) -> tuple[str | None, str | None]:
    """(testo da inviare, motivo). Testo ``None`` = non inviare, il motivo dice perche'.

    Se il testo e' accettabile ma sporco (marker di silenzio rimasto) torna ripulito,
    con il motivo valorizzato lo stesso: il chiamante lo logga a livello INFO.
    """
    if text is None or not isinstance(text, str):
        return None, "non e' una stringa"
    cleaned = text
    reason = None
    if NO_REPLY_MARKER in cleaned:
        cleaned = cleaned.replace(NO_REPLY_MARKER, "").strip()
        reason = "marker di silenzio tolto"
    visible = _visible(cleaned)
    if not visible:
        return None, "vuoto"
    if _PUNCT_ONLY_RE.match(visible):
        return None, "solo punteggiatura"
    if visible.lower() in _SENTINELS:
        return None, f"valore segnaposto ({visible[:20]!r})"
    if len(visible) <= _PLACEHOLDER_MAX_LEN and _PLACEHOLDER_RE.search(_CODE_RE.sub("", cleaned)):
        return None, "segnaposto non riempito"
    if _RUN_RE.search(visible):
        return None, "carattere ripetuto"
    if _repeated_lines(visible):
        return None, "righe identiche ripetute"
    return cleaned, reason


def _is_duplicate(chat_id, thread_id, text: str, now: float) -> bool:
    visible = _visible(text)
    if len(visible) < DUP_MIN_LEN:
        return False
    key = (chat_id, thread_id or 0, visible)
    last = _recent.get(key)
    _recent[key] = now
    _recent.move_to_end(key)
    while len(_recent) > _DUP_MAX_ENTRIES:
        _recent.popitem(last=False)
    return last is not None and now - last < DUP_WINDOW_SEC


def check(chat_id, thread_id, text, now: float | None = None) -> tuple[str | None, str | None]:
    """``inspect`` piu' l'anti-ripetizione per scheda. Stesso contratto."""
    cleaned, reason = inspect(text)
    if cleaned is None:
        return None, reason
    if _is_duplicate(chat_id, thread_id, cleaned, time.monotonic() if now is None else now):
        return None, f"identico a uno inviato negli ultimi {DUP_WINDOW_SEC}s"
    return cleaned, reason


def _count(reason: str) -> None:
    key = reason.split(" (")[0]
    stats[key] = stats.get(key, 0) + 1


def install(bot_cls) -> None:
    """Avvolge ``bot_cls.send_message``. Idempotente; ``MESSAGE_GUARD=off`` lo spegne."""
    if os.environ.get("MESSAGE_GUARD", "on").lower() in ("off", "0", "false"):
        return
    original = bot_cls.send_message
    if getattr(original, "_message_guard", False):
        return

    @functools.wraps(original)
    async def guarded(self, chat_id, text=None, *args, **kwargs):
        cleaned, reason = check(chat_id, kwargs.get("message_thread_id"), text)
        if cleaned is None:
            _count(reason or "?")
            logger.warning(
                "messaggio scartato (%s) chat=%s thread=%s testo=%r",
                reason, chat_id, kwargs.get("message_thread_id"), (text or "")[:80] if isinstance(text, str) else text,
            )
            return None
        if reason:
            logger.info("messaggio ripulito (%s) chat=%s", reason, chat_id)
        sent = await original(self, chat_id, cleaned, *args, **kwargs)
        _record(chat_id, kwargs.get("message_thread_id"), getattr(sent, "message_id", None), cleaned,
                str(getattr(self, "token", "") or "").split(":")[0] or None)
        return sent

    guarded._message_guard = True  # type: ignore[attr-defined]
    bot_cls.send_message = guarded
