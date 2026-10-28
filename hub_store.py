"""Persistent map conversation_id <-> (chat, topic, session, turns) + SSE cursors.
Atomic JSON writes (temp+rename), mirroring notif_store.py."""

from __future__ import annotations

import json
import logging
import os
import socket
import tempfile
import time

from atomic_io import replace_with_retry

logger = logging.getLogger("coordinator.hub_store")

HUB_TOPICS_PATH = os.environ.get("HUB_TOPICS_PATH", "/app/data/hub_topics.json")

# Single-flight claims: a live session (e.g. an interactive Claude) can "claim"
# an agent so the Coordinator does NOT spawn a headless twin for it (the double
# dispatcher problem -- two network-dev-linux on the same thread). The claim is
# a heartbeat file under CLAIMS_DIR: it is honoured only while fresh, so a crashed
# claimer auto-releases after CLAIM_TTL seconds.
CLAIMS_DIR = os.environ.get("HUB_CLAIMS_DIR", "/app/data/claims")
CLAIM_TTL = int(os.environ.get("HUB_CLAIM_TTL", "90"))

# A claim can come from two very different sources sharing the same file:
# hub_watch.py (a real SSE watcher, source="watch") heartbeats every REFRESH_S
# (30s, see hub_watch.py) while it is actually listening on the bus; the
# hub-auto-claim.sh Claude Code hook (source="hook", the default -- it writes
# the file directly in bash, without going through claim(), so it never sets
# `source`) refreshes on every SessionStart/Stop of an interactive session
# regardless of whether anything is listening on the bus. WATCH_TTL is tighter
# than CLAIM_TTL and gives a margin over REFRESH_S so a real watcher's
# heartbeat is never mistaken for stale between refreshes.
WATCH_TTL = int(os.environ.get("HUB_WATCH_TTL", "45"))


def _load() -> dict:
    try:
        with open(HUB_TOPICS_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        data = {}
    data.setdefault("conversations", {})
    data.setdefault("cursors", {})
    data.setdefault("epochs", {})
    data.setdefault("wake_queue", {})
    data.setdefault("running_wakes", {})
    return data


def _save(data: dict) -> None:
    d = os.path.dirname(HUB_TOPICS_PATH) or "."
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
        replace_with_retry(tmp, HUB_TOPICS_PATH)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def get_conv(conv_id: int) -> dict | None:
    return _load()["conversations"].get(str(conv_id))


def upsert_conv(
    conv_id: int,
    *,
    tenant: str,
    chat_id: int,
    topic_id: int,
    counterpart: str | None = None,
) -> None:
    data = _load()
    rec = data["conversations"].get(str(conv_id), {"turns": 0})
    rec.update(tenant=tenant, chat_id=chat_id, topic_id=topic_id)
    # `counterpart` is the other agent on the conversation (the escalating agent
    # from Telegram's view). Stored so an A2A reply can address POST
    # /a2a/agents/{counterpart}. Never overwrite a known value with None.
    if counterpart:
        rec["counterpart"] = counterpart
    data["conversations"][str(conv_id)] = rec
    _save(data)


def set_conv_fields(conv_id: int, **fields) -> None:
    """Merge ``fields`` into the conversation record (created if missing)."""
    data = _load()
    rec = data["conversations"].setdefault(str(conv_id), {"turns": 0})
    rec.update(fields)
    _save(data)


def get_session(conv_id: int, agent_id: str, workspace: str) -> str | None:
    """Resumable session for ONE agent on a conversation, or None.

    Keyed on ``(conv_id, agent_id)``, never on ``conv_id`` alone: a
    conversation can have several participants with a local listener on this
    device (conversation 33 had two, `life` and `homeResearcher-minipc`) and a
    single slot makes them alias. The last one to run overwrote it and the
    next wake of the OTHER agent resumed that session in its own workspace —
    so two nodes shared one model conversation, and the node's own folder
    looked *external* to a session born elsewhere, which is what raised the
    headless `permission=external_directory ask` this issue is about (#30).

    ``workspace`` is a second, independent guard: a session is only resumed in
    the directory it was born in. Even correctly keyed, a session whose project
    resolved elsewhere blocks on every resume, and ``--dir`` does not rewrite
    it — it pins the process, not the session. Mismatch degrades to a fresh
    session, which costs one cold start instead of an idle timeout.

    The legacy top-level ``session_id`` is deliberately NOT read: every value
    written before this change may belong to another agent, so there is no way
    to tell a sound one from a poisoned one. Treating them all as absent is the
    migration (see `purge_legacy_sessions`).
    """
    rec = get_conv(conv_id) or {}
    entry = (rec.get("sessions") or {}).get(agent_id)
    if not entry:
        return None
    born_in = entry.get("workspace") or ""
    if born_in and workspace and born_in != workspace:
        logger.warning(
            "hub session for #%s/%s born in '%s' but resuming in '%s' — "
            "starting a fresh session instead (a session keeps the project it "
            "was created with; --dir does not rewrite it)",
            conv_id,
            agent_id,
            born_in,
            workspace,
        )
        return None
    return entry.get("session_id") or None


def set_session(
    conv_id: int,
    session_id: str | None,
    *,
    agent_id: str,
    workspace: str = "",
    running: bool | None = None,
) -> None:
    """Store the resumable session of ONE agent, with the workspace it ran in.

    ``running`` (TicketManager#10) says whether a headless run is on that
    session right now: True as soon as the run's first stream event names the
    session, False when the run ends. None leaves it as it was. ``started_at``
    is when this session id was first recorded and survives later runs on it.

    Also drops the legacy per-conversation ``session_id`` on the way: leaving
    it behind would keep a poisoned value in the file for anything that read it.
    """
    data = _load()
    rec = data["conversations"].setdefault(str(conv_id), {"turns": 0})
    rec.pop("session_id", None)
    sessions = rec.setdefault("sessions", {})
    if session_id:
        prev = sessions.get(agent_id) or {}
        entry = {"session_id": session_id, "workspace": workspace}
        if prev.get("session_id") == session_id:
            entry["started_at"] = prev.get("started_at")
            entry["running"] = bool(prev.get("running"))
        entry["started_at"] = entry.get("started_at") or _now_iso()
        if running is not None:
            entry["running"] = running
        entry.setdefault("running", False)
        sessions[agent_id] = entry
    else:
        sessions.pop(agent_id, None)
    _save(data)


def _now_iso() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).isoformat(timespec="seconds")


def mark_session_stopped(conv_id: int, agent_id: str) -> None:
    """No headless run is on this agent's session any more (whatever happened)."""
    data = _load()
    entry = ((data["conversations"].get(str(conv_id)) or {}).get("sessions") or {}).get(
        agent_id
    )
    if entry and entry.get("running"):
        entry["running"] = False
        _save(data)


def clear_running_sessions() -> int:
    """At boot no run of this process is alive: a ``running`` left from before a
    restart would make the Launcher show a session as busy forever."""
    data = _load()
    n = 0
    for rec in data["conversations"].values():
        for entry in (rec.get("sessions") or {}).values():
            if entry.get("running"):
                entry["running"] = False
                n += 1
    if n:
        _save(data)
    return n


def purge_legacy_sessions() -> int:
    """Strip the pre-#30 per-conversation ``session_id`` from every record.

    Those sessions were shared between the agents of a conversation, so each
    one may carry another node's project and history: any of them can block on
    a resume and none can be told apart. Dropping them is the whole migration —
    the cost is one cold session per conversation, the alternative is a 5-minute
    idle timeout on every poisoned resume. Returns how many were removed.
    """
    data = _load()
    stale = [
        rec for rec in data["conversations"].values() if rec.get("session_id")
    ]
    for rec in stale:
        rec.pop("session_id", None)
    if stale:
        _save(data)
    return len(stale)


def incr_turn(conv_id: int) -> int:
    data = _load()
    rec = data["conversations"].setdefault(str(conv_id), {"turns": 0})
    rec["turns"] = int(rec.get("turns", 0)) + 1
    _save(data)
    return rec["turns"]


def set_wake_escalated(conv_id: int, *, at_message_id: int = 0) -> None:
    """Mark a conversation as escalated after a failed wake (idempotent).

    Once set, _wake_local_agent short-circuits: the conversation is in the
    human's court, so re-wakes from its own escalation event are no-ops
    (mirrors the turn-cap guard against incident #32).

    ``at_message_id`` is the message we gave up on, and it SCOPES the
    suppression: anything newer is fresh content, not an echo, and re-opens the
    conversation (see `wake_escalated_at` / `clear_wake_escalated`). Without
    that scope the flag was a permanent gag — a 12-second bus outage burned both
    wake attempts and silenced conversation 33 for good (issue #29).
    """
    data = _load()
    rec = data["conversations"].setdefault(str(conv_id), {"turns": 0})
    rec["wake_escalated"] = True
    rec["wake_escalated_at"] = int(at_message_id)
    _save(data)


def wake_escalated_at(conv_id: int) -> int | None:
    """Message id this conversation was escalated at, or None if not escalated.

    Records written before #29 carry the flag without an id: they resolve to 0,
    so the first message that arrives afterwards lifts the gag on its own — no
    migration needed to un-silence a conversation stuck from the old behaviour.
    """
    rec = get_conv(conv_id) or {}
    if not rec.get("wake_escalated"):
        return None
    return int(rec.get("wake_escalated_at") or 0)


def clear_wake_escalated(conv_id: int) -> bool:
    """Lift the escalation gag and give the conversation a fresh turn budget.

    Returns True if a gag was actually set. The turn counter is reset with it:
    a conversation that reached a human and then received new content starts
    over, otherwise the two guards compose into a second permanent silencer —
    the gag lifts and the turn cap keeps dropping every event (#29).

    Resetting turns is safe ONLY because this runs on the un-gag path, which
    requires a prior escalation (two failed answer attempts). A healthy
    agent<->agent ping-pong never reaches it, so the cap still bounds it.
    That also requires the un-gagging message to be REAL content: an automatic
    notice from another agent's escalation must never get here, or two silent
    agents un-gag each other forever with the cap reset every round (#59) —
    `hub._wake_local_agent` drops those notices before this point.
    """
    data = _load()
    rec = data["conversations"].get(str(conv_id))
    if not rec or not rec.get("wake_escalated"):
        return False
    rec.pop("wake_escalated", None)
    rec.pop("wake_escalated_at", None)
    rec["turns"] = 0
    _save(data)
    return True


def auto_notice_posted(conv_id: int) -> bool:
    """True if an automatic notice was already posted on this conversation and
    nothing but automatic notices has been said since (#59)."""
    return bool((get_conv(conv_id) or {}).get("auto_notice"))


def mark_auto_notice(conv_id: int) -> None:
    """Record that an automatic notice was posted: the next one is suppressed
    until a real message (human or agent) arrives and clears it."""
    data = _load()
    rec = data["conversations"].setdefault(str(conv_id), {"turns": 0})
    rec["auto_notice"] = True
    _save(data)


def clear_auto_notice(conv_id: int) -> bool:
    """A real message arrived: the conversation may carry one notice again.
    Writes only when the flag was set, so the common path costs a read."""
    data = _load()
    rec = data["conversations"].get(str(conv_id))
    if not rec or not rec.get("auto_notice"):
        return False
    rec.pop("auto_notice", None)
    _save(data)
    return True


def mark_human_replied(conv_id: int) -> None:
    """The human answered on this conversation from Telegram: from now on the
    agents' replies are forwarded into the topic too, not only escalations (#66)."""
    data = _load()
    rec = data["conversations"].setdefault(str(conv_id), {"turns": 0})
    if rec.get("human_replied"):
        return
    rec["human_replied"] = True
    _save(data)


def human_replied(conv_id: int) -> bool:
    return bool((get_conv(conv_id) or {}).get("human_replied"))


def conv_id_for_topic(chat_id: int, topic_id: int) -> int | None:
    for cid, rec in _load()["conversations"].items():
        if rec.get("chat_id") == chat_id and rec.get("topic_id") == topic_id:
            return int(cid)
    return None


def get_epoch(tenant: str) -> str | None:
    """Last-seen bus DB fingerprint for a tenant (None before the first check)."""
    return _load()["epochs"].get(tenant)


def set_epoch(tenant: str, epoch: str) -> None:
    data = _load()
    data["epochs"][tenant] = epoch
    _save(data)


def reset_bus_state(tenant: str) -> int:
    """Drop stale conv records + SSE cursors for a tenant after a bus DB rebuild.

    conv_id / event_id restart from 1 when Network's DB is recreated, so records
    keyed by them silently alias onto brand-new conversations (a leftover
    ``wake_escalated`` suppresses every wake; a high cursor skips new events —
    issue #25). Called only when the bus epoch changed. Removes conv records whose
    ``tenant`` matches OR that carry no tenant attribution (the ambiguous ones
    created by incr_turn/set_session before an upsert_conv — exactly the stale
    escalation records) and clears the ``{tenant}:*`` cursors. Returns the number
    of conv records removed. Losing this state only costs a bounded re-wake
    (turn_cap), never data.
    """
    data = _load()
    convs = data["conversations"]
    removed = [cid for cid, rec in convs.items() if rec.get("tenant") in (tenant, None)]
    for cid in removed:
        del convs[cid]
    cursors = data["cursors"]
    for key in [k for k in cursors if k.startswith(f"{tenant}:")]:
        del cursors[key]
    # Queued wakes point at conv ids of the old DB: same aliasing hazard.
    queue = data["wake_queue"]
    for key in [k for k in queue if k.startswith(f"{tenant}:")]:
        del queue[key]
    running = data["running_wakes"]
    for key in [k for k in running if k.startswith(f"{tenant}:")]:
        del running[key]
    _save(data)
    return len(removed)


def get_cursor(tenant: str, agent: str) -> int | None:
    return _load()["cursors"].get(f"{tenant}:{agent}")


def set_cursor(tenant: str, agent: str, event_id: int) -> None:
    data = _load()
    data["cursors"][f"{tenant}:{agent}"] = event_id
    _save(data)


# Per-agent wake queue (#67): a wake that finds the agent's single headless
# slot busy (#38) is parked here instead of dropped, and started when the slot
# frees up. Persisted so a container restart does not lose it. One entry per
# conversation: a newer message on an already-queued conversation replaces the
# older payload in place (the woken session reads the whole thread anyway).


def enqueue_wake(tenant: str, agent_id: str, ev_type: str, event: dict) -> bool:
    """Park a wake for ``agent_id``. Returns False if the conversation was
    already queued (payload refreshed in place, position kept)."""
    data = _load()
    queue = data["wake_queue"].setdefault(f"{tenant}:{agent_id}", [])
    conv_id = int(event["conversation_id"])
    entry = {"conv_id": conv_id, "ev_type": ev_type, "data": event}
    for i, item in enumerate(queue):
        if item.get("conv_id") == conv_id:
            if int(event.get("message_id") or 0) >= int(item["data"].get("message_id") or 0):
                queue[i] = entry
                _save(data)
            return False
    queue.append(entry)
    _save(data)
    return True


def pop_wake(tenant: str, agent_id: str) -> dict | None:
    """Remove and return the oldest queued wake for ``agent_id`` (or None)."""
    data = _load()
    key = f"{tenant}:{agent_id}"
    queue = data["wake_queue"].get(key) or []
    if not queue:
        return None
    item = queue.pop(0)
    if not queue:
        del data["wake_queue"][key]
    _save(data)
    return item


def queued_wakes(tenant: str, agent_id: str) -> list[int]:
    """Conversation ids waiting for ``agent_id``, oldest first."""
    return [int(i["conv_id"]) for i in _load()["wake_queue"].get(f"{tenant}:{agent_id}", [])]


def wake_queue_agents() -> list[tuple[str, str]]:
    """(tenant, agent) pairs with at least one queued wake."""
    return [tuple(k.split(":", 1)) for k, v in _load()["wake_queue"].items() if v]


# Wakes in flight (#73): written when a headless run starts, removed when it
# ends. A record still here at boot means the process died mid-run -- typically
# a recreate of the bot container, which kills the ssh child the host session
# hangs off. Those wakes go back to the head of the queue instead of being lost.


def mark_wake_running(tenant: str, agent_id: str, ev_type: str, event: dict) -> None:
    data = _load()
    data["running_wakes"][f"{tenant}:{agent_id}"] = {
        "conv_id": int(event["conversation_id"]),
        "ev_type": ev_type,
        "data": event,
        "started_at": time.time(),
    }
    _save(data)


def clear_wake_running(tenant: str, agent_id: str) -> None:
    data = _load()
    if data["running_wakes"].pop(f"{tenant}:{agent_id}", None) is not None:
        _save(data)


def running_wakes() -> list[dict]:
    """Wakes currently marked in flight: ``{tenant, agent, conv_id, started_at}``."""
    out = []
    for key, rec in _load()["running_wakes"].items():
        tenant, agent = key.split(":", 1)
        out.append({"tenant": tenant, "agent": agent, "conv_id": rec["conv_id"],
                    "started_at": rec.get("started_at")})
    return out


def requeue_interrupted_wakes() -> list[tuple[str, str, int]]:
    """Move every wake left marked in flight to the HEAD of its agent's queue.

    Called once at boot, when no run of this process can be alive. If the same
    conversation is already queued, the newer payload wins and it moves first.
    Returns the ``(tenant, agent, conv_id)`` requeued."""
    data = _load()
    running = data["running_wakes"]
    if not running:
        return []
    moved = []
    for key, rec in running.items():
        queue = data["wake_queue"].setdefault(key, [])
        entry = {"conv_id": rec["conv_id"], "ev_type": rec["ev_type"], "data": rec["data"]}
        for i, item in enumerate(queue):
            if item.get("conv_id") == rec["conv_id"]:
                old = queue.pop(i)
                if int(old["data"].get("message_id") or 0) > int(rec["data"].get("message_id") or 0):
                    entry = old
                break
        queue.insert(0, entry)
        tenant, agent = key.split(":", 1)
        moved.append((tenant, agent, rec["conv_id"]))
    data["running_wakes"] = {}
    _save(data)
    return moved


def _claim_path(tenant: str, agent_id: str) -> str:
    safe = f"{tenant}__{agent_id}".replace("/", "_").replace(os.sep, "_")
    return os.path.join(CLAIMS_DIR, f"{safe}.json")


def is_claimed(tenant: str, agent_id: str) -> bool:
    """True while a live session holds a fresh claim on this agent.

    Stale claims (heartbeat older than CLAIM_TTL) are ignored, so a crashed or
    forgotten claimer never wedges the agent permanently.
    """
    try:
        with open(_claim_path(tenant, agent_id), encoding="utf-8") as fh:
            rec = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return False
    return (time.time() - float(rec.get("ts", 0))) < CLAIM_TTL


def is_watched(tenant: str, agent_id: str) -> bool:
    """True while a REAL bus watcher (hub_watch.py) holds a fresh claim.

    Unlike `is_claimed` -- which only tells us "an interactive session seems
    present" and is left untouched here -- this tells us "something is
    actually listening on the bus for this agent". A claim written by the
    hub-auto-claim.sh hook (source missing or "hook") never satisfies this,
    even if fresh: it only proves an interactive Claude Code session exists,
    not that it (or anything else) is reading bus events for this agent.
    """
    try:
        with open(_claim_path(tenant, agent_id), encoding="utf-8") as fh:
            rec = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return False
    if rec.get("source", "hook") != "watch":
        return False
    vivo = _processo_vivo(rec)
    if vivo is not None:
        return vivo
    return (time.time() - float(rec.get("ts", 0))) < WATCH_TTL


def _processo_vivo(rec: dict) -> bool | None:
    """Il watcher che ha scritto questo claim e' ancora in esecuzione?

    ``None`` quando non si puo' sapere — il claim viene da un'altra macchina,
    o non porta un pid — e allora decide la freschezza del timestamp.

    Guardare il processo invece dell'orologio conta perche' il modo tipico in
    cui un watcher muore e' l'uccisione dall'esterno (il reaper dei task in
    background lo fa per "low memory", misurato quattro volte in mezz'ora su
    dev-linux, issue #36). Il claim resta indietro con un timestamp fresco, e
    per tutto il TTL l'anti-gemello crede che qualcuno stia ascoltando: il
    worker headless — che invece ci sarebbe — viene zittito proprio nei
    secondi in cui era l'unico rimasto. Un `kill -0` toglie quella finestra,
    e la toglie tutta.

    E' la stessa domanda che si pone `hub_watch_vivo` nell'hook Stop di
    ~/.claude (issue #27). Due risposte diverse alla stessa domanda erano
    proprio il difetto.
    """
    pid = rec.get("pid")
    host = rec.get("host") or ""
    if not isinstance(pid, int) or pid <= 0:
        return None
    if host and host != socket.gethostname():
        return None  # un altro box: il suo pid qui non significa niente
    if os.name == "nt":
        # Mai os.kill su Windows: per ogni segnale che non sia CTRL_C/CTRL_BREAK
        # chiama TerminateProcess, quindi "sei vivo?" uccideva il watcher, e un
        # pid inesistente dava OSError generico e ricadeva sul timestamp.
        return _processo_vivo_windows(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # esiste, e' solo di un altro utente
    except OSError:
        return None
    return True


def _processo_vivo_windows(pid: int) -> bool | None:
    """``kill -0`` for Windows: open the process read-only and ask if it exited."""
    import ctypes
    from ctypes import wintypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    k32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
    k32.CloseHandle.argtypes = (wintypes.HANDLE,)
    process_query_limited_information, still_active = 0x1000, 259
    error_access_denied, error_invalid_parameter = 5, 87
    handle = k32.OpenProcess(process_query_limited_information, False, pid)
    if not handle:
        err = ctypes.get_last_error()
        if err == error_invalid_parameter:
            return False  # no such pid
        if err == error_access_denied:
            return True  # exists, belongs to someone else
        return None
    try:
        code = wintypes.DWORD()
        if not k32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return None
        return code.value == still_active
    finally:
        k32.CloseHandle(handle)


def claim(
    tenant: str,
    agent_id: str,
    *,
    pid: int | None = None,
    host: str = "",
    source: str = "hook",
) -> None:
    """Take/refresh a claim on an agent (call periodically as a heartbeat).

    `source` defaults to "hook" (the hub-auto-claim.sh hook writes the claim
    file directly in bash, without calling this function, so a record with no
    `source` field must be treated the same as `source="hook"`). hub_watch.py
    is the only caller that passes `source="watch"`.
    """
    os.makedirs(CLAIMS_DIR, exist_ok=True)
    rec = {
        "pid": pid if pid is not None else os.getpid(),
        "host": host,
        "ts": time.time(),
        "source": source,
    }
    fd, tmp = tempfile.mkstemp(dir=CLAIMS_DIR, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(rec, fh)
        replace_with_retry(tmp, _claim_path(tenant, agent_id))
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def release(tenant: str, agent_id: str, *, only_pid: int | None = None) -> None:
    """Release a claim (best-effort).

    ``only_pid`` limita il rilascio al claim scritto da QUEL processo. Serve
    perche' un claim e' un file condiviso fra chiunque tenga quell'agente: un
    watcher che muore e ritira il claim alla cieca puo' cancellare quello di
    uno **vivo** — succede davvero quando per sbaglio ne partono due sullo
    stesso nome, e il risultato e' un agente che risulta libero mentre qualcuno
    lo sta ascoltando, cioe' l'invito a svegliare un gemello headless. Chi
    rilascia perche' sta morendo passa il proprio pid; chi rilascia perche' la
    sessione e' finita (l'hook) non ha un pid da confrontare e passa None.
    """
    percorso = _claim_path(tenant, agent_id)
    if only_pid is not None:
        try:
            with open(percorso, encoding="utf-8") as fh:
                if json.load(fh).get("pid") != only_pid:
                    return  # il claim e' di un altro: non e' nostro da ritirare
        except (OSError, json.JSONDecodeError):
            return
    try:
        os.remove(percorso)
    except OSError:
        pass
