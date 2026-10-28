"""Who takes a turn on a conversation that a resumable session opened.

The Coordinator's counterpart of Network's ``scripts/hub_origin.py``: the same
four rules from Network
``docs/superpowers/specs/2026-09-21-cross-project-requests-design.md``, but
reading the host's session registry and transcript store through the
container's mounts (``~/.claude`` -> ``/home/app/.claude``). Pure decision over
facts, kept out of hub.py so it can be tested with fake dirs and no bus.

Invariant above all: two processes must never run on one Claude session UUID.
Whenever a fact is missing or unclear the answer is "derived" or "skip", never
"resume_origin".
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Literal


# Same window as ~/.claude/hooks/lib_sessions.py: an open session's /loop 5m
# writes its transcript at least this often, so a transcript newer than this
# proves the session is still following the conversation.
STALE_S = 45 * 60


@dataclass(frozen=True)
class Turn:
    action: Literal["derived", "skip", "resume_origin"]
    reason: str
    session_id: str | None = None
    cwd: str | None = None


def transcript_path(projects_dir: Path, cwd: str, session_id: str) -> Path:
    """Where the origin session's transcript lives, via the mounted projects dir.

    ``claude_slug`` is the same slug Claude Code uses to name its project
    directory (every non-alphanumeric char becomes ``-``), so a host cwd like
    ``${LIFE_HOME}`` maps to the transcript directory ``-home-dev-Life``.
    """
    return projects_dir / claude_slug(cwd) / f"{session_id}.jsonl"


def who_takes_turn(
    origin,
    agent: str,
    host: str,
    *,
    sessions_dir: Path,
    projects_dir: Path,
    now: float,
) -> Turn:
    """Decide who runs for a message on a conversation, given its origin_session.

    ``origin`` is the conversation's ``origin_session`` (from the bus detail),
    ``agent`` the agent this dispatcher is about to wake, ``host`` this
    dispatcher's own host. Returns one of:

    - ``derived``: behave as today (derived UUID), the origin logic does not apply;
    - ``skip``: this message is not ours to answer — another host owns the origin
      session, or its /loop is still following it;
    - ``resume_origin``: the origin session closed; resume ``session_id`` in ``cwd``.
    """
    if not origin or origin.get("agent") != agent:
        return Turn("derived", "no origin session for this agent")
    if host and str(origin.get("host", "")).lower() != str(host).lower():
        return Turn("skip", f"origin session lives on {origin.get('host')}")
    sid, cwd = origin.get("session_id"), origin.get("cwd")
    if not isinstance(sid, str) or not isinstance(cwd, str) or not sid or not cwd:
        return Turn("derived", "origin session has no session_id or cwd")
    tpath = transcript_path(projects_dir, cwd, sid)
    if not tpath.is_file():
        return Turn("derived", "origin transcript not on this host")
    # Alive means the session is still open (registry entry present) AND its
    # transcript was written recently. We cannot probe the host claude process
    # from the container, so the registry entry + transcript mtime IS the
    # aliveness signal. When the registry dir is not visible we cannot tell
    # alive from dead -> derived, never resume.
    if not sessions_dir.is_dir():
        return Turn(
            "derived",
            "cannot tell whether the origin session is alive (no registry here)",
        )
    rec = sessions_dir / f"{sid}.json"
    if rec.is_file():
        try:
            mtime = tpath.stat().st_mtime
        except OSError:
            return Turn("derived", "origin transcript unreadable")
        if now - mtime < STALE_S:
            return Turn(
                "skip",
                "origin session is alive; its loop takes the turn",
                session_id=sid,
                cwd=cwd,
            )
    return Turn("resume_origin", "origin session closed; resuming it", session_id=sid, cwd=cwd)


def last_interactive_voice(messages, agent: str) -> dict | None:
    """The interactive session that last spoke as ``agent`` on this conversation.

    ``hub_send.py`` stamps every message sent from a terminal session with
    ``context.voice == "interactive"`` and ``context.session`` (same shape as
    ``origin_session``). A conversation the session did not open has no
    ``origin_session`` for it, so without this the Coordinator would wake a
    headless twin that answers under the same name while the terminal session
    is still talking -- two contradictory voices for one identity (#54).

    Returns the session of the NEWEST such message, or None. A headless
    message after it does not cancel it: the terminal session may still be
    following, and that is exactly what ``who_takes_turn`` checks.
    """
    for msg in reversed(list(messages or [])):
        if msg.get("from_agent") != agent:
            continue
        ctx = msg.get("context")
        if not isinstance(ctx, dict) or ctx.get("voice") != "interactive":
            continue
        session = ctx.get("session")
        if isinstance(session, dict) and session.get("agent") == agent:
            return session
    return None


def pick_turn(origin_turn: Turn, voice_turn: Turn | None) -> Turn:
    """Combine the origin-session decision with the last-interactive-voice one.

    ``skip`` from either wins: one live terminal session on the conversation is
    enough to keep the headless twin out. Otherwise the more recent voice is
    the better session to resume, then the origin, then ``derived``.
    """
    if voice_turn is None:
        return origin_turn
    if "skip" in (origin_turn.action, voice_turn.action):
        return origin_turn if origin_turn.action == "skip" else voice_turn
    if voice_turn.action == "resume_origin":
        return voice_turn
    return origin_turn


# --- Conversation -> session view (TicketManager#10) -------------------------
# The Launcher opens the SAME session a bus wake is running, so it needs the cwd
# claude really lives in. That is not the cwd the wake launched it in: a session
# can EnterWorktree mid-run, and Claude Code then moves the transcript under the
# worktree's project dir. `claude --resume` only finds it from there.

def claude_slug(cwd: str) -> str:
    """The project-dir name Claude Code files a session under: every
    non-alphanumeric char becomes ``-`` (``_`` included, unlike
    ``projects.escape_path``: ``Portale_Segnalazioni`` -> ``Portale-Segnalazioni``)."""
    return re.sub(r"[^A-Za-z0-9]", "-", cwd)


def find_transcript(projects_dir: Path, session_id: str) -> Path | None:
    """The transcript of ``session_id`` under any project dir, newest if several."""
    if not session_id or "/" in session_id or not projects_dir.is_dir():
        return None
    hits = [p for p in projects_dir.glob(f"*/{session_id}.jsonl") if p.is_file()]
    if not hits:
        return None
    return max(hits, key=lambda p: p.stat().st_mtime)


def _cwds(path: Path) -> list[str]:
    out = []
    with path.open(encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if '"cwd"' not in line:
                continue
            try:
                cwd = json.loads(line).get("cwd")
            except (ValueError, AttributeError):
                continue
            if isinstance(cwd, str) and cwd:
                out.append(cwd)
    return out


def transcript_cwd(path: Path) -> str | None:
    """The folder ``claude --resume`` finds this session from.

    That is the cwd whose slug names the project dir holding the transcript —
    NOT the last cwd: a ``cd backend`` inside the session moves the cwd but
    leaves the transcript where it was, and from ``.../backend`` the resume
    finds nothing. The slug is lossy, so it is matched against the cwds the
    transcript actually saw (and their parents), never inverted.
    """
    try:
        cwds = _cwds(path)
    except OSError:
        return None
    target = path.parent.name
    for cwd in reversed(cwds):
        if claude_slug(cwd) == target:
            return cwd
    for cwd in reversed(cwds):
        for parent in PurePosixPath(cwd).parents:
            if claude_slug(str(parent)) == target:
                return str(parent)
    return cwds[-1] if cwds else None


def real_session_cwd(projects_dir: Path, session_id: str) -> str | None:
    tpath = find_transcript(projects_dir, session_id)
    return transcript_cwd(tpath) if tpath else None


def last_write(projects_dir: Path, session_id: str) -> float | None:
    """When ``session_id``'s transcript was last written, or None if not found.

    The only activity signal a dispatcher has for an interactive session: it
    writes its transcript on every turn, and on nothing else. A registry entry
    proves the window is open, not that anyone is behind it (#74)."""
    tpath = find_transcript(projects_dir, session_id)
    if tpath is None:
        return None
    try:
        return tpath.stat().st_mtime
    except OSError:
        return None


def conversation_sessions(rec: dict | None, projects_dir: Path) -> list[dict]:
    """One row per agent with a session on the conversation record."""
    rows = []
    for agent_id, entry in sorted(((rec or {}).get("sessions") or {}).items()):
        sid = (entry or {}).get("session_id")
        if not sid:
            continue
        rows.append(
            {
                "agent_id": agent_id,
                "session_id": sid,
                "cwd": real_session_cwd(projects_dir, sid) or entry.get("workspace") or "",
                "running": bool(entry.get("running")),
                "started_at": entry.get("started_at"),
            }
        )
    return rows


def interactive_holder(
    session_id: str,
    agent: str,
    host: str,
    *,
    sessions_dir: Path,
    projects_dir: Path,
    now: float,
) -> Turn | None:
    """``skip`` if an interactive session (e.g. opened by the Launcher with
    ``claude --resume``) holds ``session_id`` right now, else None.

    Same aliveness facts as the origin rule — registry entry + fresh transcript —
    applied to the worker's own stored session: two processes must never run on
    one session UUID, whoever resumed it.
    """
    tpath = find_transcript(projects_dir, session_id)
    if tpath is None or not (sessions_dir / f"{session_id}.json").is_file():
        return None
    try:
        fresh = now - tpath.stat().st_mtime < STALE_S
    except OSError:
        return None
    if fresh:
        return Turn("skip", f"session {session_id} is held by a live interactive session")
    return None
