# providers.py
"""Content providers for M4 notifications.

Each provider is ``async def (notif, *, run_claude) -> str | None`` (None ->
nothing to send). Dependencies (run_claude) are INJECTED so this module never
imports bot.py — keeping the import graph acyclic. ``run_provider`` dispatches
on ``notif.provider``.
"""
from __future__ import annotations

import asyncio
import html
import logging
import os
import re
import time
from pathlib import Path

import httpx

import llm_balance
import llm_usage
import projects
import message_guard
import wake_routing
from message_guard import NO_REPLY_MARKER
from notifications import Notification

logger = logging.getLogger("coordinator.providers")

HA_BASE_URL = os.environ.get("HA_BASE_URL", "")
HA_TOKEN = os.environ.get("HA_TOKEN", "")
HA_TIMEOUT = float(os.environ.get("HA_TIMEOUT_SEC", "10"))

# (entity_id, label) — verificati contro mycorise-agent monitor.py (Step 0):
# ENV_SENSORS (fruiting growbox) + PLUG_SWITCH_ENTITY (4 prese Sonoff S60TPF).
GROWBOX_ENTITIES: tuple[tuple[str, str], ...] = (
    ("sensor.growbox_1_sensor_growbox_1_co2", "CO₂"),
    ("sensor.fruiting_rh_avg", "Umidità"),
    ("sensor.fruiting_t_avg", "Temperatura"),
)

# Sonoff plug switches (label, entity_id) — on/off state per growbox actuator.
GROWBOX_PLUGS: tuple[tuple[str, str], ...] = (
    ("Estrattore", "switch.estrattore_sonoff_10027aeaed_1"),
    ("Umidificatore", "switch.umidificatore_sonoff_10027aeafb_1"),
    ("Ventola", "switch.ventola_sonoff_10027aed2c_1"),
    ("Scaldino", "switch.scaldino_sonoff_10027afd91_1"),
)


# Declared in <istanza>/config/llm-routing.json: model and fallback are the queue's.
SERVICE_AGENT = "coordinator.notificaAgente"

ERROR_MARKER = "[[ERRORE]]"
# Appended to every agent prompt: without it a blocked run narrates its failure
# ("non ho potuto eseguire…") and that prose went to the group every 30 minutes.
AGENT_DELIVERY_RULE = (
    "\n\n(Regola di consegna: se non c'è niente da comunicare rispondi solo "
    f"{NO_REPLY_MARKER}. Se non riesci a fare quanto chiesto — comando bloccato, "
    f"permesso negato, dato mancante — rispondi solo {ERROR_MARKER} seguito dal "
    "motivo in una riga. Qualsiasi altro testo viene inviato così com'è.)"
)


class ProviderError(Exception):
    """The provider failed: the reason belongs to the outcome ledger, not the chat."""


async def _provider_static(notif: Notification, *, run_claude) -> str | None:
    return (notif.params.get("text") or "").strip() or None


async def _provider_agent(notif: Notification, *, run_claude) -> str | None:
    proj = projects.resolve_project(notif.params.get("project", ""))
    if proj is None:
        logger.warning("agent provider: unknown project %r", notif.params.get("project"))
        return None
    prompt = (notif.params.get("prompt") or "").strip()
    if not prompt:
        return None
    if str(notif.params.get("command") or "").strip():
        # The Coordinator runs the command, not the LLM: runner permissions and cwd
        # stop mattering, and an empty stdout costs no LLM call at all.
        out = await _run_command(notif)  # ProviderError -> outcome ledger
        if not out:
            return None
        prompt += f"\n\nOutput del comando:\n{out}"
    prompt += AGENT_DELIVERY_RULE
    # The instance's queue when declared (Life routing v2), the legacy runner otherwise.
    run = await wake_routing.run_node_service(SERVICE_AGENT, proj.key, prompt)
    if run is None:
        run = await run_claude(prompt, workspace=proj.workspace,
                               persona=None, resume_id=None)
    text, _sid = run
    # The notification's own key as the caller: this is the granularity the
    # bench in #28 was measured at ("stessa notifica, stesso workspace"), and
    # the one that makes a run that got 193x cheaper visible per notification.
    llm_usage.record(run, node=proj.key, caller=notif.key)
    # An agent that produced nothing (empty stream, "(risposta vuota)" sentinel)
    # must not become an empty Telegram message — that is exactly the noise a
    # scheduled report turns into. Treat it as "nothing to send" so _fire
    # classifies it as no-content instead of delivering a blank bubble.
    stripped = (text or "").strip()
    if not stripped or stripped == "(risposta vuota)" or NO_REPLY_MARKER in stripped:
        logger.info("agent provider: nothing to send for %s", notif.key)
        return None
    if ERROR_MARKER in stripped:
        reason = stripped.split(ERROR_MARKER, 1)[1].strip() or "l'agente non è riuscito"
        raise ProviderError(reason)
    if stripped.startswith(("❌", "⏱")):
        raise ProviderError(stripped)
    # Deterministic net for an agent that narrates instead of using [[ERRORE]].
    if message_guard.is_agent_meta(stripped):
        raise ProviderError(f"meta-testo dell'agente: {stripped[:200]}")
    return text


def _format_growbox(states: list[dict]) -> str:
    by_id = {s.get("entity_id"): s for s in states}
    lines = ["🍄 <b>Coltivazione</b>"]
    for ent, label in GROWBOX_ENTITIES:
        s = by_id.get(ent)
        if s is not None:
            lines.append(f"• {label}: {s.get('state')}")
    plug_states = [
        f"{label} {'🟢' if (by_id.get(ent) or {}).get('state') == 'on' else '⚫️'}"
        for label, ent in GROWBOX_PLUGS if by_id.get(ent) is not None
    ]
    if plug_states:
        lines.append("🔌 " + " · ".join(plug_states))
    return "\n".join(lines) if len(lines) > 1 else "🍄 Coltivazione: nessun dato."


async def _provider_growbox(notif: Notification, *, run_claude) -> str | None:
    if not HA_BASE_URL or not HA_TOKEN:
        return "🍄 Coltivazione: dati non disponibili (HA non configurato)."
    try:
        async with httpx.AsyncClient(timeout=HA_TIMEOUT) as client:
            r = await client.get(
                f"{HA_BASE_URL}/api/states",
                headers={"Authorization": f"Bearer {HA_TOKEN}"},
            )
            r.raise_for_status()
            states = r.json()
    except Exception as exc:
        logger.warning("growbox: HA fetch failed: %r", exc)
        return "🍄 Coltivazione: HomeAssistant non raggiungibile."
    return _format_growbox(states)


async def _provider_report(notif: Notification, *, run_claude) -> str | None:
    """Deliver a report a node's own scheduled job already wrote to a file.

    The alternative was the ``agent`` provider — a full Claude session every
    morning to read a file that already exists. This one is deterministic, costs
    nothing and cannot hallucinate a network status.

    Three behaviours, and only the first is silence:

    * **empty file** -> ``None``. Silence is the feature: a report that always
      arrives is a report nobody reads. The producer prints nothing when there
      is nothing to say.
    * **stale file** -> a loud line. A file older than its job's period means
      the producer died, and re-sending yesterday's content as if fresh is the
      exact failure this whole effort exists to kill: reassuring, and wrong.
    * **unreadable/absent** -> a loud line naming the path, never ``None``.
    """
    raw = (notif.params.get("path") or "").strip()
    if not raw:
        logger.warning("report provider: no path in %s", notif.key)
        return None

    # Reports live inside the mounted workspaces. Confining them there keeps a
    # notification — which is created from a chat — from reading arbitrary files.
    root = Path(os.environ.get("REPORT_PROVIDER_ROOT", "/workspaces")).resolve()
    path = (root / raw).resolve()
    if root not in path.parents:
        logger.warning("report provider: %r escapes %s", raw, root)
        return f"⚠️ Report non consegnato: percorso fuori da {root} ({raw})."

    try:
        text = path.read_text(encoding="utf-8", errors="replace").strip()
        age_h = (time.time() - path.stat().st_mtime) / 3600
    except OSError as exc:
        return f"⚠️ Report non leggibile ({raw}): {exc.strerror or exc}."

    max_age_h = float(notif.params.get("max_age_h") or 26)
    if age_h > max_age_h:
        return (f"⚠️ Il report «{notif.title}» è fermo da {age_h:.0f} ore "
                f"({raw}): il job che lo produce non sta più girando.")
    return text or None


async def _provider_balance(notif: Notification, *, run_claude) -> str | None:
    """DeepSeek credits, asked before a failed wake is the first evidence (#95).

    Silent while the balance is fine; one alert on the way down, a reminder at
    most every few hours, one recovery message on the way back up.
    """
    del notif, run_claude
    return await llm_balance.check()


async def _provider_health(notif: Notification, *, run_claude) -> str | None:
    """The notification subsystem watching itself: alert only when something is mute.

    Returns None (nothing sent) while everything is healthy — on purpose. This
    provider's own liveness is not proven by what it sends but by the fact that
    it RAN, which scheduler._fire records as ``last_attempt_at`` in the outcome
    ledger and notif_health._watchdog_block checks over HTTP. See the long note
    there: an alert that speaks daily to say "fine" is an alert nobody reads.
    """
    import notif_health  # local import: notif_health -> notif_store only (no cycle)

    return notif_health.format_alert(notif_health.build_report())


HEALTH_URL = os.environ.get("HEALTH_URL", "http://127.0.0.1:10030").rstrip("/")
HEALTH_TIMEOUT = float(os.environ.get("HEALTH_TIMEOUT_SEC", "10"))

# Both end up in a URL built from a notification that was created from a chat.
_REGIME_PERSON_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
_REGIME_SLOT_RE = re.compile(r"^[a-z_]+$")


async def _provider_regime(notif: Notification, *, run_claude) -> str | None:
    """What a person has to take in this slot, rendered by Health at send time.

    Health owns the regimen and its rendering (``render_slot``): this provider
    forwards ``text`` and composes nothing. The point over a ``static`` that
    Health rewrites is freshness — no window between a plan change and the sync,
    and things a text written last week cannot know (yesterday's adherence, a
    ceiling exceeded today by two protocols overlapping).

    Every failure is ``None`` + a log line, never a message: a health reminder
    that arrives empty or wrong is worse than one that does not arrive, because
    it says "nothing to take". ``notif_health`` sees the silence via the outcome.
    """
    person = str(notif.params.get("person") or "").strip()
    slot = str(notif.params.get("slot") or "").strip()
    if not _REGIME_PERSON_RE.match(person):
        logger.warning("regime: invalid person %r in %s", person, notif.key)
        return None
    if slot and not _REGIME_SLOT_RE.match(slot):
        logger.warning("regime: invalid slot %r in %s", slot, notif.key)
        return None

    try:
        async with httpx.AsyncClient(timeout=HEALTH_TIMEOUT) as client:
            r = await client.get(
                f"{HEALTH_URL}/api/persons/{person}/agenda/next",
                params={"slot": slot} if slot else None,
            )
            r.raise_for_status()
            data = r.json()
    except Exception as exc:
        logger.warning("regime: Health fetch failed for %s: %r", notif.key, exc)
        return None

    if not isinstance(data, dict) or data.get("empty"):
        return None
    # A pinned slot that comes back as another one is the wrong pills at the
    # wrong meal: refuse it rather than forward it.
    if slot and data.get("slot") != slot:
        logger.warning("regime: %s asked slot %r, Health answered %r — not sent",
                       notif.key, slot, data.get("slot"))
        return None
    return (data.get("text") or "").strip() or None


async def _provider_sentinella(notif: Notification, *, run_claude) -> str | None:
    """La prod work instance sorvegliata da fuori. None quando e' tutto a posto.

    Vive qui, e non in uno script a parte su dev-linux, per una ragione precisa:
    cosi' l'allarme e' una notifica ordinaria e `notif_health` sorveglia la
    sentinella senza una riga in piu'. Il guasto dell'8 agosto e' passato
    inosservato perche' il sorvegliante moriva col sorvegliato: il sorvegliante
    deve a sua volta avere un sorvegliante.
    """
    import sentinella_giro  # import locale: nessun ciclo con providers

    modo = str(notif.params.get("modo", "allarmi"))
    return await sentinella_giro.giro(modo=modo)


COMMAND_DEFAULT_TIMEOUT_SEC = 300.0
COMMAND_TAIL_LINES = 20
COMMAND_TAIL_CHARS = 1500


def _tail(text: str) -> str:
    """Last lines of ``text``, bounded: the end of a log is where the cause is."""
    lines = text.strip().splitlines()[-COMMAND_TAIL_LINES:]
    return "\n".join(lines)[-COMMAND_TAIL_CHARS:]


def command_timeout(params: dict) -> float:
    """Effective timeout of a ``command`` notification (validated 1..3600)."""
    raw = params.get("timeout_sec")
    if raw is None or raw == "":
        return COMMAND_DEFAULT_TIMEOUT_SEC
    return float(raw)


async def _kill_tree(proc) -> None:
    """Kill the shell AND what it spawned, then reap it (bounded).

    ``proc.kill()`` alone only reaches the shell: a grandchild (the script) keeps
    running and holds the pipes, so ``wait()`` can hang for as long as it lives.
    """
    try:
        if os.name == "nt":
            killer = await asyncio.create_subprocess_exec(
                "taskkill", "/F", "/T", "/PID", str(proc.pid),
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
            await killer.wait()
        else:
            import signal
            os.killpg(proc.pid, signal.SIGKILL)
    except Exception:
        logger.debug("tree kill failed, killing the shell only", exc_info=True)
    try:
        proc.kill()
    except ProcessLookupError:
        pass
    try:
        await asyncio.wait_for(proc.wait(), timeout=10)
    except asyncio.TimeoutError:
        logger.error("command process %s did not exit after kill", proc.pid)


async def _provider_command(notif: Notification, *, run_claude) -> str | None:
    """Run a node's own deterministic job; no LLM involved.

    The text is sent as Telegram HTML, so everything that comes from the command
    is escaped. Outcomes:

    * exit 0, stdout non-empty -> the stdout; exit 0 and empty -> ``None``
      (silence is the job saying "nothing to report").
    * exit != 0 -> ``ProviderError`` with the exit code and the TAIL of stderr
      (of stdout when stderr is empty). Never silence: a broken job says so in
      the outcome ledger (frontend + daily health digest), not in the chat.
    * timeout -> process killed, ``ProviderError``. Unresolvable project / empty
      command / launch failure -> ``ProviderError`` too.

    Only ever created from a versioned ``coordinator.toml`` (see
    ``notifications.MANIFEST_ONLY_PROVIDERS`` / ``api``): it runs shell commands.
    """
    return html.escape(await _run_command(notif), quote=False) or None


async def _run_command(notif: Notification) -> str:
    """Run ``params["command"]`` in the project workspace: raw stripped stdout on
    exit 0, ``ProviderError`` otherwise (shared by the command and agent providers)."""
    title = notif.title
    command = str(notif.params.get("command") or "").strip()
    if not command:
        logger.error("command provider: no command in %s", notif.key)
        raise ProviderError(f"«{title}» non eseguito: comando mancante")
    proj = projects.resolve_project(str(notif.params.get("project") or ""))
    if proj is None:
        logger.error("command provider: unknown project in %s", notif.key)
        raise ProviderError(f"«{title}» non eseguito: progetto sconosciuto")
    try:
        timeout = command_timeout(notif.params)
    except (TypeError, ValueError):
        raise ProviderError(f"«{title}» non eseguito: timeout_sec non valido") from None
    try:
        proc = await asyncio.create_subprocess_shell(
            command,
            cwd=proj.workspace,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=(os.name != "nt"),  # own process group -> killpg on timeout
        )
    except Exception as exc:
        logger.exception("command %s failed to launch (%r)", notif.key, command)
        raise ProviderError(f"«{title}» non eseguibile: {exc}") from exc
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        await _kill_tree(proc)
        logger.error("command %s timed out after %ss", notif.key, timeout)
        raise ProviderError(f"«{title}» scaduto dopo {timeout:g} s") from None
    stdout = (out or b"").decode("utf-8", "replace")
    stderr = (err or b"").decode("utf-8", "replace")
    if proc.returncode == 0:
        return stdout.strip()
    logger.error("command %s exited %s", notif.key, proc.returncode)
    tail = _tail(stderr) or _tail(stdout)
    head = f"«{title}» fallito (exit {proc.returncode})"
    raise ProviderError(f"{head}\n{tail}" if tail else head)


PROVIDERS = {
    "static": _provider_static,
    "health": _provider_health,
    "deepseek-balance": _provider_balance,
    "sentinella": _provider_sentinella,
    "agent": _provider_agent,
    "growbox": _provider_growbox,
    "report": _provider_report,
    "regime": _provider_regime,
    "command": _provider_command,
}

# Providers owned by a specific project: offered in the UI only when that
# project is the notification's target. Providers absent here are global
# (always available). Add an entry when a new node ships its own provider.
PROVIDER_PROJECT: dict[str, str] = {
    "growbox": "mycorise",
    # Not "health": that key is the mute-notification watchdog, a namesake.
    "regime": "health",
}


async def run_provider(notif: Notification, *, run_claude) -> str | None:
    fn = PROVIDERS.get(notif.provider)
    if fn is None:
        logger.warning("unknown provider %r", notif.provider)
        return None
    return await fn(notif, run_claude=run_claude)
