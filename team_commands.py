"""Telegram commands of a Life team instance (ComUnity Task 14).

``/segnala``, ``/incarichi``, ``/prendi`` and ``/chiudi`` are thin, deterministic
wrappers: no LLM in the loop. They reuse the instance's own ClickUp CLI (Task 9)
and the ComUnity invite path, and they only *read* the Life role the gate already
granted. The manifest decides which project has them and which areas each topic
owns; nothing here names ComUnity.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import sys
from pathlib import Path

import httpx

import instances as instances_mod

logger = logging.getLogger("coordinator.team_commands")

CLICKUP_MODULE = ("scripts.istanza.clickup", "python -m scripts.istanza.clickup")
GITHUB_API = "https://api.github.com"
CLI_TIMEOUT = 60.0
TELEGRAM_CHUNK = 3900


class TeamError(RuntimeError):
    """A wrapper could not complete; the message is safe to show to the user."""


# --- small helpers -----------------------------------------------------------

def _instance_path(instance_id: str) -> Path | None:
    inst = instances_mod.get(instance_id)
    return Path(inst.path) if inst is not None else None


def _instance_config(instance_id: str) -> dict:
    inst = instances_mod.get(instance_id)
    return instances_mod.instance_config(inst) if inst is not None else {}


def _areas(instance_id: str) -> list[str]:
    cfg = _instance_config(instance_id)
    return [str(a.get("slug")) for a in (cfg.get("areas") or []) if a.get("slug")]


def _sector_areas(team, agent_name: str | None) -> list[str]:
    if team is None:
        return []
    if agent_name and agent_name in team.sector_areas:
        return list(team.sector_areas[agent_name])
    return []


def _clickup_person(instance_id: str, telegram_id: int, person_id: int | None) -> int | None:
    """The ClickUp user id of the sender, from ``channels.clickup.people``.

    Accepted shapes (the second is what this code writes in the docs):

        "people": {"5429690501": 1234567}
        "people": {"5429690501": {"clickup_user_id": 1234567, "name": "Alice"}}

    The key is the Telegram id; a Life ``person_id`` key is accepted too, so an
    instance can migrate without rewriting the map.
    """
    cfg = _instance_config(instance_id)
    people = ((cfg.get("channels") or {}).get("clickup") or {}).get("people") or {}
    for key in (str(telegram_id), str(person_id) if person_id is not None else ""):
        if not key:
            continue
        raw = people.get(key)
        if raw is None:
            continue
        value = raw.get("clickup_user_id") if isinstance(raw, dict) else raw
        try:
            return int(value)
        except (TypeError, ValueError):
            logger.warning("channels.clickup.people[%s] non numerico: %r", key, value)
            return None
    return None


def _app_version(project) -> str:
    path = Path(project.workspace) / "frontend" / "pubspec.yaml"
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            m = re.match(r"^version:\s*(\S+)", line.strip())
            if m:
                return m.group(1)
    except OSError:
        pass
    return "sconosciuta"


def _telegram_link(chat_id: int, thread_id: int | None, message_id: int) -> str:
    internal = str(chat_id)
    if internal.startswith("-100"):
        internal = internal[4:]
    else:
        internal = internal.lstrip("-")
    url = f"https://t.me/c/{internal}/{message_id}"
    if thread_id:
        url += f"?thread={thread_id}"
    return url


def _error_line(stderr: str) -> str:
    lines = [ln.strip() for ln in (stderr or "").splitlines() if ln.strip()]
    return lines[-1] if lines else "nessun dettaglio"


def _chunks(text: str, size: int = TELEGRAM_CHUNK) -> list[str]:
    out: list[str] = []
    while text:
        out.append(text[:size])
        text = text[size:]
    return out or [""]


async def _reply(msg, text: str) -> None:
    for chunk in _chunks(text):
        await msg.reply_text(chunk)


async def _run_cli(project, instance_path: Path, argv: list[str]) -> tuple[int, str, str]:
    cmd = [sys.executable, "-m", CLICKUP_MODULE[0], "--istanza", str(instance_path), *argv]
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=project.workspace,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=CLI_TIMEOUT)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        return 124, "", "ClickUp: il comando non ha risposto entro 60s"
    return (proc.returncode or 0,
            (out or b"").decode("utf-8", "replace").strip(),
            (err or b"").decode("utf-8", "replace").strip())


# --- /segnala ----------------------------------------------------------------

def _issue_title(text: str) -> str:
    first = " ".join((text or "").split()).strip()
    return f"[Telegram] {first[:120]}" if first else "[Telegram] segnalazione"


def _issue_body(text: str, version: str, author: str, tg_id: int,
                chat_title: str | None, photo_name: str | None) -> str:
    lines = [text.strip() or "(nessun testo)", "", "---",
             f"Versione app: {version}",
             f"Autore: {author} (Telegram `{tg_id}`)",
             f"Segnalato da: {chat_title or 'Telegram'}"]
    if photo_name:
        lines.append(f"Foto: ricevuta su Telegram ({photo_name})")
    return "\n".join(lines)


async def create_issue(repo: str, token: str, title: str, body: str, *,
                       transport: httpx.AsyncBaseTransport | None = None) -> dict:
    if not token:
        raise TeamError("GitHub non configurato: manca GH_TOKEN")
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    try:
        async with httpx.AsyncClient(timeout=20.0, transport=transport) as c:
            r = await c.post(f"{GITHUB_API}/repos/{repo}/issues",
                             json={"title": title, "body": body}, headers=headers)
    except httpx.HTTPError as exc:
        raise TeamError(f"GitHub irraggiungibile ({type(exc).__name__})") from None
    try:
        data = r.json()
    except ValueError:
        data = {}
    if r.status_code not in (200, 201):
        detail = data.get("message") if isinstance(data, dict) else ""
        raise TeamError(f"GitHub ha rifiutato l'issue (HTTP {r.status_code}: {detail or '?'})")
    return data if isinstance(data, dict) else {}


async def _save_photo(update, issue_number: int | None, data_dir: Path) -> str | None:
    msg = update.effective_message
    if msg is None or not getattr(msg, "photo", None):
        return None
    folder = data_dir / "segnalazioni"
    try:
        folder.mkdir(parents=True, exist_ok=True)
        tg_file = await msg.photo[-1].get_file()
        suffix = Path(getattr(tg_file, "file_path", "") or "foto.jpg").suffix or ".jpg"
        name = f"{issue_number or 'bozza'}{suffix}"
        await tg_file.download_to_drive(folder / name)
        return name
    except Exception:
        logger.exception("segnala: foto non scaricata")
        return None


async def segnala(update, project, team, instance_id: str, args: str) -> None:
    msg = update.effective_message
    user = update.effective_user
    text = (args or "").strip()
    if not text and not getattr(msg, "photo", None):
        await msg.reply_text("Scrivi cosa non va: /segnala <testo> (puoi allegare una foto).")
        return
    repo = (team.github_repo if team else "").strip()
    if not repo:
        await msg.reply_text("Segnalazioni non configurate: manca `team.github_repo` nel progetto.")
        return
    version = _app_version(project)
    author = (user.full_name if user else "?") or "?"
    tg_id = user.id if user else 0
    chat_title = getattr(update.effective_chat, "title", None)
    body = _issue_body(text, version, author, tg_id, chat_title, None)
    try:
        token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN", "")
        issue = await create_issue(repo, token, _issue_title(text), body)
    except TeamError as exc:
        await msg.reply_text(f"⚠️ {exc}")
        return
    number = issue.get("number")
    issue_url = issue.get("html_url") or ""
    photo = await _save_photo(update, number if isinstance(number, int) else None,
                              Path("/app/data"))
    if photo:
        logger.info("segnala: foto salvata in data/segnalazioni/%s (issue #%s)", photo, number)

    task_line = ""
    rc, out, err = await _run_cli(
        project, _instance_path(instance_id) or Path("."),
        ["crea", "--area", "bizTech", "--titolo", _issue_title(text),
         "--descrizione", f"{body}\n\nIssue: {issue_url}", "--link", issue_url])
    if rc != 0:
        task_line = f"⚠️ Task ClickUp non creato: {_error_line(err)}"
    else:
        first = (out.splitlines() or [""])[0]
        cols = first.split("\t")
        task_id = cols[0].strip() if cols else ""
        task_url = cols[1].strip() if len(cols) > 1 else ""
        task_url = task_url or (f"https://app.clickup.com/t/{task_id}" if task_id else "?")
        task_line = f"✅ Task ClickUp: {task_url}"

    lines = [f"🐞 Segnalazione aperta: {issue_url}"]
    if photo:
        lines.append(f"📎 Foto salvata: data/segnalazioni/{photo}")
    lines.append(task_line)
    await _reply(msg, "\n".join(lines))


# --- /incarichi, /prendi, /chiudi --------------------------------------------

def _requested_area(args: str) -> str | None:
    words = (args or "").split()
    if not words:
        return None
    if words[0] == "--area" and len(words) > 1:
        return words[1]
    if words[0].startswith("--area="):
        return words[0].split("=", 1)[1]
    return words[0] if words[0] and not words[0].startswith("-") else None


def _areas_for(update, instance_id: str, team, agent_name: str | None, args: str) -> list[str]:
    known = _areas(instance_id)
    wanted = _requested_area(args)
    if wanted:
        return [wanted]
    sector = _sector_areas(team, agent_name)
    return sector or known


async def incarichi(update, project, team, instance_id: str,
                    agent_name: str | None, args: str) -> None:
    msg = update.effective_message
    instance_path = _instance_path(instance_id)
    if instance_path is None:
        await msg.reply_text(f"Istanza `{instance_id}` non registrata su questo device.")
        return
    areas = _areas_for(update, instance_id, team, agent_name, args)
    if not areas:
        await msg.reply_text("Nessuna area configurata per questo topic.")
        return
    blocks: list[str] = []
    for area in areas:
        rc, out, err = await _run_cli(project, instance_path, ["incarichi", "--area", area])
        blocks.append(f"📋 {area}")
        blocks.append(out if rc == 0 else f"⚠️ {_error_line(err)}")
    await _reply(msg, "\n\n".join(blocks))


async def prendi(update, project, team, instance_id: str, grant, args: str) -> None:
    msg = update.effective_message
    user = update.effective_user
    task_id = (args or "").strip().split()[0] if (args or "").strip() else ""
    if not task_id:
        await msg.reply_text("Uso: /prendi <id incarico>")
        return
    person = _clickup_person(instance_id, user.id if user else 0,
                             grant.person_id if grant else None)
    if person is None:
        await msg.reply_text(
            "Non so a quale utente ClickUp corrispondi: aggiungi il tuo id Telegram a "
            "`channels.clickup.people` in instance.config.json (vedi la doc del CLI).")
        return
    instance_path = _instance_path(instance_id)
    if instance_path is None:
        await msg.reply_text(f"Istanza `{instance_id}` non registrata su questo device.")
        return
    rc, _out, err = await _run_cli(project, instance_path,
                                   ["prendi", task_id, "--persona", str(person)])
    if rc != 0:
        await msg.reply_text(f"⚠️ {_error_line(err)}")
        return
    await msg.reply_text(f"✅ Incarico {task_id} preso.")


async def chiudi(update, project, team, instance_id: str, grant, args: str) -> None:
    msg = update.effective_message
    words = (args or "").split()
    if not words:
        await msg.reply_text("Uso: /chiudi <id incarico> [nota] [documento]")
        return
    task_id = words[0]
    note = words[1] if len(words) > 1 else _telegram_link(
        update.effective_chat.id,
        getattr(msg, "message_thread_id", None),
        msg.message_id,
    )
    doc = words[2] if len(words) > 2 else None
    instance_path = _instance_path(instance_id)
    if instance_path is None:
        await msg.reply_text(f"Istanza `{instance_id}` non registrata su questo device.")
        return
    argv = ["chiudi", task_id, "--nota", note]
    if doc:
        argv += ["--doc", doc]
    rc, _out, err = await _run_cli(project, instance_path, argv)
    if rc != 0:
        await msg.reply_text(f"⚠️ {_error_line(err)}")
        return
    await msg.reply_text(f"✅ Incarico {task_id} in revisione.\nNota: {note}")


# --- entry point -------------------------------------------------------------

async def handle(update, project, cap, args: str, *, grant, team,
                 instance_id: str, agent_name: str | None) -> None:
    kind = cap.kind or cap.key
    if kind == "segnala":
        await segnala(update, project, team, instance_id, args)
    elif kind == "incarichi":
        await incarichi(update, project, team, instance_id, agent_name, args)
    elif kind == "prendi":
        await prendi(update, project, team, instance_id, grant, args)
    elif kind == "chiudi":
        await chiudi(update, project, team, instance_id, grant, args)
    else:
        logger.warning("team command %r: kind sconosciuto %r", cap.key, kind)
        await update.effective_message.reply_text("Comando non configurato.")
