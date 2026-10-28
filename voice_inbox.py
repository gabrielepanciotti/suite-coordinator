"""Telegram voice notes -> transcription -> the Life instance's brain queue (#56).

A voice note is an idea captured on the move. It ends in the same place the
``/idea`` skill writes to: the queue of the Life *instance* the bot serves,
``<instance>/.brain-inbox/idee.jsonl``, one JSON row per entry with ``nodo``
naming the node it is about. Everything downstream (the nightly digest, the one
general ``/idee-revisione``) keeps working without knowing a phone was involved.
Only the ``marcatore`` differs: ``vocale-telegram``.
Contract: ``Life/docs/brain-coda-istanza.md``.

In the container the instance is whatever ``LIFE_INSTANCE_DIR`` points at (the
siblings the hooks look at on a host are not visible here). The queue is
versioned in the instance repo with ``merge=union``, so after appending the bot
commits and pushes it: the digest may well run on another device.

Three steps, each replaceable on its own:

* ``Transcriber`` turns an audio file into text. The real one is faster-whisper,
  local, on CPU — the ``small`` model is already baked into the image for the
  reel pipeline, so no audio ever leaves the machine and nothing is downloaded.
  Tests use a fake.
* ``split_named_node`` reads "Casa, compra le lampadine" as (Casa, "compra le
  lampadine"). Only a *whole* leading name followed by punctuation counts: a
  sentence that merely starts with a word that happens to be a node name must
  not be rerouted by accident.
* ``append_idea`` writes the row, in the exact format of the ``/idea`` skill.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import os
import re
import threading
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Protocol

logger = logging.getLogger("coordinator.voice_inbox")

MARKER = "vocale-telegram"
# Same queue, typed instead of spoken (``/ricorda``).
TEXT_MARKER = "testo-telegram"
INBOX_DIR = ".brain-inbox"
INBOX_FILE = "idee.jsonl"

# The node a direct-chat voice note lands in when it names none.
DEFAULT_NODE_ENV = "VOICE_DEFAULT_NODE"
DEFAULT_NODE = "life"
# Spoken names that are neither a node key nor its label ("casa" for Home):
# "parola=nodo,parola=nodo". Per device, like every other name mapping.
ALIASES_ENV = "VOICE_NODE_ALIASES"
MODEL_ENV = "VOICE_WHISPER_MODEL"
LANGUAGE_ENV = "VOICE_WHISPER_LANGUAGE"

# A leading name is at most a few words, closed by punctuation: "Casa, ...",
# "Offerte: ...", "Home Expense Splitter. ...". Without the punctuation it is
# just the first word of a sentence.
_LEAD_RE = re.compile(r"^\s*([\w'][\w' -]{0,40}?)\s*[,:.;!\-–—]\s*(.+)$", re.S | re.U)
_EMOJI_RE = re.compile(r"[^\w\s'-]", re.U)


class Transcriber(Protocol):
    def __call__(self, audio_path: str) -> str: ...


def _norm(name: str) -> str:
    return " ".join(_EMOJI_RE.sub(" ", name or "").lower().split())


def parse_aliases(raw: str | None) -> dict[str, str]:
    """``"casa=home, lavoro=offerte"`` -> ``{"casa": "home", "lavoro": "offerte"}``."""
    out: dict[str, str] = {}
    for pair in (raw or "").split(","):
        word, sep, node = pair.partition("=")
        if sep and _norm(word) and node.strip():
            out[_norm(word)] = node.strip()
    return out


def node_names(projects: Iterable, aliases: dict[str, str]) -> dict[str, str]:
    """Every spoken form a node answers to -> its key.

    Key, registry aliases and label (emoji stripped), plus the device aliases.
    A device alias wins over a registry name: it is the more specific choice.
    """
    names: dict[str, str] = {}
    for p in projects:
        for form in (p.key, getattr(p, "label", ""), *getattr(p, "aliases", ())):
            if _norm(form):
                names.setdefault(_norm(form), p.key)
    names.update(aliases)
    return names


def split_named_node(text: str, names: dict[str, str]) -> tuple[str | None, str]:
    """``(node_key, rest)`` if the text opens by naming a node, else ``(None, text)``."""
    m = _LEAD_RE.match(text or "")
    if not m:
        return None, (text or "").strip()
    key = names.get(_norm(m.group(1)))
    if key is None:
        return None, text.strip()
    return key, m.group(2).strip()


INSTANCE_ENV = "LIFE_INSTANCE_DIR"
INSTANCE_MARKER = "instance.config.json"


def instance_dir(env: dict | None = None) -> Path | None:
    """The Life instance this bot writes to, or ``None`` if none is mounted.

    Only a directory that really is an instance counts: an unset variable, or a
    mount that fell back to the shared placeholder, must not turn into a queue
    that nobody's digest will ever read.
    """
    raw = ((os.environ if env is None else env).get(INSTANCE_ENV) or "").strip()
    if not raw:
        return None
    path = Path(raw)
    return path if (path / INSTANCE_MARKER).is_file() else None


def instance_name(instance: Path, run: "Runner | None" = None) -> str:
    """The instance's real name (``life-personal``), not its mount point.

    In the container the checkout is mounted at ``/life-instance``: telling the
    person "in coda per il brain di life-instance" names a path nobody knows.
    The repo's remote carries the true name; the directory is the fallback.
    """
    try:
        res = (run or _run)(["git", "-C", str(instance), "config", "--get", "remote.origin.url"])
        url = (res.stdout or "").strip() if res.returncode == 0 else ""
    except Exception:
        url = ""
    name = url.rstrip("/").rsplit("/", 1)[-1].rsplit(":", 1)[-1]
    name = name[:-4] if name.endswith(".git") else name
    return name or instance.name


def inbox_path(root: str | Path) -> Path:
    return Path(root) / INBOX_DIR / INBOX_FILE


def append_idea(
    root: str | Path,
    nodo: str,
    testo: str,
    session: str,
    *,
    now: Callable[[], dt.datetime] | None = None,
    marker: str = MARKER,
) -> Path:
    """Append one row to the queue under ``root`` (instance or node), ``/idea`` format."""
    stamp = (now or (lambda: dt.datetime.now(dt.UTC)))()
    row = {
        "ts": stamp.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "session": session,
        "nodo": nodo,
        "marcatore": marker,
        "testo": testo,
    }
    path = inbox_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    return path


Runner = Callable[[list[str]], "subprocess.CompletedProcess"]


def _run(args: list[str]):
    import subprocess

    return subprocess.run(args, capture_output=True, text=True, timeout=60)


_PUBLISH_LOCK = threading.Lock()


def publish(instance: Path, nodo: str, run: Runner | None = None) -> bool:
    """Commit the queue file alone and push it. ``True`` when it reached the remote.

    Only the queue path is committed (pathspec), never whatever else happens to
    be dirty in the instance checkout. A failure is not fatal: the row is on
    disk, and it leaves with the next successful push — so it is logged, not
    raised, and the caller tells the person it is queued *locally*.
    """
    run = run or _run
    rel = f"{INBOX_DIR}/{INBOX_FILE}"
    git = ["git", "-C", str(instance)]
    steps = [
        [*git, "add", "--", rel],
        [*git, "commit", "-q", "-m", f"chore(brain): vocale in coda ({nodo})", "--", rel],
        # merge=union on the queue (.gitattributes) makes rows appended on two
        # devices merge instead of conflicting, rebase included.
        [*git, "pull", "-q", "--rebase", "--autostash"],
        [*git, "push", "-q"],
    ]
    with _PUBLISH_LOCK:
        for step in steps:
            try:
                res = run(step)
            except Exception as exc:  # git missing, timeout
                logger.warning("voice publish: %s fallito (%s)", step[3], exc)
                return False
            if res.returncode != 0:
                logger.warning("voice publish: %s exit %s: %s",
                               step[3], res.returncode, (res.stderr or res.stdout or "").strip()[:300])
                return False
    return True


def node_dir_name(project) -> str:
    """The ``nodo`` field: the node's directory name, as ``/idea`` writes it."""
    for raw in (getattr(project, "host_path", ""), getattr(project, "workspace", "")):
        name = Path((raw or "").replace("\\", "/")).name
        if name and not name.startswith("."):
            return name
    return project.key


# One transcription at a time: the bot shares a small box with a dozen
# containers, and two models loaded side by side is how the OOM killer gets in.
_LOCK = threading.Lock()


def whisper_transcribe(audio_path: str) -> str:
    """Local faster-whisper, CPU int8. Loaded per call and dropped after.

    Voice notes are rare; keeping ~500 MB resident between them would cost more
    than the second or two it takes to load ``small`` from the baked cache.
    """
    from faster_whisper import WhisperModel  # heavy import, only when a note arrives

    model_name = os.environ.get(MODEL_ENV, "small")
    language = os.environ.get(LANGUAGE_ENV, "it") or None
    with _LOCK:
        model = WhisperModel(model_name, device="cpu", compute_type="int8")
        try:
            segments, _info = model.transcribe(audio_path, language=language, vad_filter=True)
            return " ".join(s.text.strip() for s in segments).strip()
        finally:
            del model
