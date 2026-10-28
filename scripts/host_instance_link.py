"""Write an instance's Telegram channel on the HOST, then commit and push it (#82).

Reached through the forced command of the host-exec key (``host_claude_exec.py``,
request ``{"op": "instance-telegram", ...}``). It runs here and not in the
container for two reasons, both verified on dev-linux: the instance checkouts are
mounted read-only in the bot, and the container's git token cannot even see the
personal repos (``life-energypass``: "Repository not found"). On the host the
user's own credentials push like a terminal would.

The request is narrow on purpose — a key that can write any file is a key that
can write every file:

* the instance is named by **id**; its path comes from the host's registry
  (``$LIFE_HOME/instances.json``), never from the client;
* the only thing written is ``channels.telegram`` = ``{chatId, agent}``, through
  Life's single writer ``suite_core.instances.connect.write_channel``;
* a group already linked to a *different* chat is refused, not overwritten:
  re-linking an instance is a decision, not the side effect of a stale menu.

Answers with one JSON line: ``{"ok", "written", "pushed", "detail"}``.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

ID_RE = re.compile(r"^[a-z][a-z0-9_-]*$")
AGENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
CONFIG = "instance.config.json"
COMMIT_MSG = "feat(channels): gruppo Telegram collegato dal Coordinator"


def life_home(env) -> Path:
    raw = env.get("LIFE_HOME")
    return Path(raw) if raw else Path.home() / ".life"


def framework_root(env) -> Path:
    """Life's checkout: ``LIFE_FRAMEWORK_ROOT``, else the sibling of this repo."""
    raw = env.get("LIFE_FRAMEWORK_ROOT")
    return Path(raw) if raw else Path(__file__).resolve().parents[2] / "Life"


def _write_channel(root: Path):
    """Life's ``write_channel``, the only writer of the channels block.

    ``connect``/``channels``/``paths`` are stdlib-only, but the package
    ``__init__`` of ``suite_core.instances`` pulls ``filelock`` (the registry
    store), which the host's ``/usr/bin/python3`` — the forced command's
    interpreter — does not have. When the plain import fails the two packages
    are registered bare, so only the three stdlib modules are loaded: still
    Life's code, not a copy.
    """
    import importlib
    import types

    shared = root / "packages" / "shared"
    if str(shared) not in sys.path:
        sys.path.insert(0, str(shared))
    try:
        from suite_core.instances.connect import write_channel
    except ImportError:
        for name, sub in (("suite_core", ""), ("suite_core.instances", "instances")):
            if name not in sys.modules:
                pkg = types.ModuleType(name)
                pkg.__path__ = [str(shared / "suite_core" / sub) if sub else str(shared / "suite_core")]
                sys.modules[name] = pkg
        write_channel = importlib.import_module("suite_core.instances.connect").write_channel
    return write_channel


def _git(path: Path, *args: str) -> tuple[int, str]:
    out = subprocess.run(
        ["git", "-C", str(path), *args], capture_output=True, text=True, timeout=120
    )
    return out.returncode, (out.stdout + out.stderr).strip()


def validate(req: dict) -> tuple[str, int, str]:
    iid = req.get("instance")
    chat = req.get("chatId")
    agent = req.get("agent")
    if not isinstance(iid, str) or not ID_RE.match(iid):
        raise ValueError("instance non valida")
    if not isinstance(chat, int) or isinstance(chat, bool) or chat >= 0:
        raise ValueError("chatId deve essere l'id negativo di un gruppo")
    if not isinstance(agent, str) or not AGENT_RE.match(agent):
        raise ValueError("agent non valido")
    return iid, chat, agent


def link_telegram(req: dict, *, env=None, git=_git, writer=None) -> dict:
    env = os.environ if env is None else env
    try:
        iid, chat, agent = validate(req)
    except ValueError as exc:
        return {"ok": False, "written": False, "pushed": False, "detail": str(exc)}

    registry_file = life_home(env) / "instances.json"
    try:
        registry = json.loads(registry_file.read_text(encoding="utf-8"))
        entry = registry["instances"][iid]
        path = Path(entry["path"])
    except (OSError, ValueError, KeyError, TypeError):
        return {"ok": False, "written": False, "pushed": False,
                "detail": f"istanza {iid} non registrata su questo host"}
    try:
        config = json.loads((path / CONFIG).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return {"ok": False, "written": False, "pushed": False,
                "detail": f"{path / CONFIG} illeggibile: {exc}"}

    # A chat belongs to one instance (#83, F0): refuse a chatId another one declares.
    for other_id, other in sorted((registry.get("instances") or {}).items()):
        if other_id == iid or not isinstance(other, dict):
            continue
        try:
            other_cfg = json.loads((Path(other["path"]) / CONFIG).read_text(encoding="utf-8"))
        except (OSError, ValueError, KeyError, TypeError):
            continue
        other_chat = ((other_cfg.get("channels") or {}).get("telegram") or {}).get("chatId")
        if other_chat == chat:
            return {"ok": False, "written": False, "pushed": False,
                    "detail": f"il gruppo {chat} e' gia' dell'istanza {other_id}: "
                              "una chat appartiene a una sola istanza"}

    current = ((config.get("channels") or {}).get("telegram") or {}).get("chatId")
    if current not in (None, "", 0) and current != chat:
        return {"ok": False, "written": False, "pushed": False,
                "detail": f"istanza {iid} gia' collegata al gruppo {current}"}

    if current != chat:
        write = writer or _write_channel(framework_root(env))
        write(path, "telegram", {"chatId": chat, "agent": agent})

    def run(*args: str) -> tuple[int, str]:
        return git(path, *args)

    # Commit only this file: the instance checkout may hold someone else's work.
    run("add", "--", CONFIG)
    rc, out = run("diff", "--cached", "--quiet", "--", CONFIG)
    if rc != 0:
        rc, out = run("commit", "-m", COMMIT_MSG, "--", CONFIG)
        if rc != 0:
            return {"ok": False, "written": True, "pushed": False,
                    "detail": f"scritto ma commit fallito: {out[-300:]}"}
    rc, out = run("push")
    if rc != 0:
        return {"ok": False, "written": True, "pushed": False,
                "detail": f"scritto e committato, push fallito: {out[-300:]}"}
    rc, sha = run("rev-parse", "--short", "HEAD")
    return {"ok": True, "written": True, "pushed": True,
            "detail": f"{path.name} {sha if rc == 0 else ''}".strip()}


# --- instance-idea (#78): a row in the brain queue of the chat's instance --------
#
# Same narrowness as above: the instance is an id resolved here, the only file
# touched is ``<instance>/.brain-inbox/idee.jsonl`` (append + commit of that path
# alone, through ``voice_inbox`` — the writer the bot used when one instance was
# mounted read-write), and every field is checked before anything is written.

NODO_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,80}$")
SESSION_RE = re.compile(r"^telegram:-?\d{1,20}:\d{1,12}$")
MARKERS = ("vocale-telegram", "testo-telegram")
MAX_TEXT = 8000


def validate_idea(req: dict) -> tuple[str, str, str, str, str]:
    iid, nodo, testo = req.get("instance"), req.get("nodo"), req.get("testo")
    session, marker = req.get("session"), req.get("marker")
    if not isinstance(iid, str) or not ID_RE.match(iid):
        raise ValueError("instance non valida")
    if not isinstance(nodo, str) or not NODO_RE.match(nodo):
        raise ValueError("nodo non valido")
    if not isinstance(testo, str) or not testo.strip() or len(testo) > MAX_TEXT:
        raise ValueError("testo vuoto o troppo lungo")
    if not isinstance(session, str) or not SESSION_RE.match(session):
        raise ValueError("session non valida")
    if marker not in MARKERS:
        raise ValueError("marcatore non ammesso")
    return iid, nodo, testo.strip(), session, marker


def _instance_path(iid: str, env) -> Path | None:
    try:
        registry = json.loads((life_home(env) / "instances.json").read_text(encoding="utf-8"))
        path = Path(registry["instances"][iid]["path"])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return path if (path / CONFIG).is_file() else None


def queue_idea(req: dict, *, env=None, append=None, publish=None) -> dict:
    env = os.environ if env is None else env
    try:
        iid, nodo, testo, session, marker = validate_idea(req)
    except ValueError as exc:
        return {"ok": False, "written": False, "pushed": False, "detail": str(exc)}
    path = _instance_path(iid, env)
    if path is None:
        return {"ok": False, "written": False, "pushed": False,
                "detail": f"istanza {iid} non registrata su questo host"}
    if append is None or publish is None:
        repo = str(Path(__file__).resolve().parents[1])
        if repo not in sys.path:
            sys.path.insert(0, repo)
        import voice_inbox

        append = append or voice_inbox.append_idea
        publish = publish or voice_inbox.publish
    try:
        append(path, nodo, testo, session, marker=marker)
    except OSError as exc:
        return {"ok": False, "written": False, "pushed": False,
                "detail": f"coda di {iid} non scrivibile: {exc}"}
    # A failed push is not lost: the row is committed on the host and leaves
    # with the next successful publish (merge=union on the queue).
    pushed = bool(publish(path, nodo))
    return {"ok": True, "written": True, "pushed": pushed, "detail": path.name}


if __name__ == "__main__":
    print(json.dumps(link_telegram(json.loads(sys.stdin.readline() or "{}"))))
