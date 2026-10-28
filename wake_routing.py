"""LLM queue of bus wakes, with fallback (Coordinator#91, Life#164).

A wake follows the queue of its instance (``<istanza>/config/llm-routing.json``):
the service ``coordinator.<progetto>`` when present, else the category
``sessione``. The first entry, when it is ``claude:<profilo>``, is today's path
(resume, same flags) run with that profile's ``CLAUDE_CONFIG_DIR``. Whatever
makes it fail, the rest of the queue runs on the HOST through the Life engine
(``suite_backend_core.llm_routing``), fresh, via the ``llm-route`` op of the
HOST_EXEC wrapper — the engine needs the device's CLIs, profiles and secrets,
none of which live in this container.

This module never reads ``ANTHROPIC_API_KEY``: the engine puts it in the env of
its child process only (mezzi ``claude-api``/``anthropic``).

The config is parsed here with plain json (the bot image does not ship Life):
only the shape the wake needs — queue, timeout, profile names. Validation is
the engine's job (``llm-run check``).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

import instances

logger = logging.getLogger("coordinator.wake_routing")

CATEGORY = "sessione"
SERVICE_PREFIX = "coordinator."
CONFIG_REL = Path("config") / "llm-routing.json"
OP_LLM_ROUTE = "llm-route"
MEZZI = ("claude", "claude-api", "anthropic", "opencode", "codex", "gateway", "deepseek")
DEFAULT_TIMEOUT_MIN = 20.0
# Margin over the engine's own timeouts: ssh, python start-up, the worker's +30 s.
FALLBACK_MARGIN_S = 180

# How an exhausted subscription or an API without credit shows up in the text of
# `claude -p` (a result event with is_error, not a ❌ runner failure). Lowercase
# fragments, version-tolerant: the exact wording has changed more than once.
_QUOTA_MARKERS = (
    "credit balance is too low",
    "credit balance too low",
    "hit your limit",
    "usage limit reached",
    "usage limit has been reached",
    "out of credits",
    "out of extra usage",
    "insufficient credit",
    "billing_error",
)


def is_quota_error(text: str | None) -> bool:
    """True when the run ended on credits/limit: no retry on the same mezzo can help."""
    low = (text or "").lower()
    return any(m in low for m in _QUOTA_MARKERS)


@dataclass(frozen=True)
class Entry:
    mezzo: str
    modello: str = ""
    profilo: str | None = None

    def spec(self) -> str:
        """The engine's ``--forza`` syntax: ``mezzo:profilo/modello``."""
        head = f"{self.mezzo}:{self.profilo}" if self.profilo else self.mezzo
        return f"{head}/{self.modello}" if self.modello else head

    def label(self) -> str:
        who = f":{self.profilo}" if self.profilo else ""
        return f"{self.mezzo}{who} {self.modello}".strip()


def primary_label(entry: Entry, *, backend: str | None = None) -> str:
    """The name of the executor that will REALLY run ``entry`` on the primary path.

    ``run_claude`` dispatches on ``AI_BACKEND``: with ``opencode`` a queue entry
    written as ``claude:<profilo>`` is served by opencode, with ``OPENCODE_MODEL``
    whenever the entry's model is a Claude alias (no ``/``). Calling it
    ``claude:<profilo>`` in the log and in the failure notice blamed the wrong
    provider — and, on 2026-10-22, the wrong *bill* (#95)."""
    import claude_runner  # late: heavy module, and tests patch it

    actual = (backend or claude_runner.AI_BACKEND or "claude").strip().lower()
    if actual == "opencode" and entry.mezzo == "claude":
        model = entry.modello if "/" in entry.modello else claude_runner.OPENCODE_MODEL
        return f"opencode {model}".strip()
    return entry.label()


_PROFILE_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
_MODEL_RE = re.compile(r"^[A-Za-z0-9_.:@+/\[\]-]{0,128}$")


def parse_force(spec: str) -> Entry:
    """``mezzo:profilo/modello`` or ``mezzo/modello`` (the model may hold a slash).

    Same grammar as the engine's ``parse_force``; ValueError on anything else."""
    spec = (spec or "").strip()
    head, _, modello = spec.partition("/")
    mezzo, _, profilo = head.partition(":")
    if mezzo not in MEZZI:
        raise ValueError(f"mezzo sconosciuto: {mezzo or '(vuoto)'} (validi: {', '.join(MEZZI)})")
    if profilo and not _PROFILE_RE.match(profilo):
        raise ValueError(f"profilo non valido: {profilo}")
    if not _MODEL_RE.match(modello):
        raise ValueError(f"modello non valido: {modello}")
    return Entry(mezzo, modello, profilo or None)


def looks_like_force(spec: str) -> bool:
    """Is this the engine syntax (``claude:owner/opus``, ``opencode/x/y``)?"""
    head = (spec or "").strip().partition("/")[0]
    mezzo, sep, _ = head.partition(":")
    return mezzo in MEZZI and (bool(sep) or "/" in spec)


@dataclass(frozen=True)
class Plan:
    """The queue a wake will walk, already resolved for one (tenant, project)."""

    instance: str  # host path of the instance (same path in the container mount)
    coda: tuple[Entry, ...]
    timeout_min: float = DEFAULT_TIMEOUT_MIN
    categoria: str | None = None
    servizio: str | None = None
    forced: Entry | None = None

    @property
    def source(self) -> str:
        if self.forced:
            return f"forza {self.forced.spec()}"
        return f"servizio {self.servizio}" if self.servizio else f"categoria {self.categoria}"

    @property
    def primary(self) -> Entry | None:
        """The entry run on today's path (resume included), when it is claude."""
        return self.coda[0] if self.coda and self.coda[0].mezzo == "claude" else None

    @property
    def fallback(self) -> tuple[Entry, ...]:
        """What the engine still has to try once the primary path has failed."""
        return self.coda[1:] if self.primary else self.coda


def _entries(raw) -> tuple[Entry, ...]:
    out = []
    for v in raw or ():
        if isinstance(v, dict) and v.get("mezzo") in MEZZI:
            out.append(Entry(str(v["mezzo"]), str(v.get("modello") or ""), v.get("profilo") or None))
    return tuple(out)


def instance_for_tenant(tenant: str, env=None) -> Path | None:
    """The registered instance whose bus tenant is ``tenant``.

    The channel declared by the instance wins; otherwise the historic naming
    ``life-<id>`` (``life-acme`` → instance ``acme``)."""
    try:
        registry, _ = instances.load_registry(env)
    except Exception as exc:
        logger.warning("wake routing: device registry unreadable: %s", exc)
        return None
    for inst in registry.values():
        try:
            if instances.channels_of(inst, env=env).bus_tenant == tenant:
                return Path(inst.path)
        except Exception:
            continue
    inst = registry.get(tenant.removeprefix("life-"))
    return Path(inst.path) if inst else None


def load_plan(tenant: str, project_key: str, *, force: str | None = None, env=None) -> Plan | None:
    """The wake's queue, or ``None`` = no routing config here (today's behaviour)."""
    inst = instance_for_tenant(tenant, env)
    if inst is None:
        return None
    path = inst / CONFIG_REL
    try:
        cfg = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        logger.warning("wake routing: %s unreadable (%s): wakes keep the old path", path, exc)
        return None
    if force:
        try:
            forced = parse_force(force)
        except ValueError as exc:
            logger.warning("wake routing: forza %r ignored: %s", force, exc)
        else:
            cat = (cfg.get("categorie") or {}).get(CATEGORY) or {}
            return Plan(str(inst), (forced,), float(cat.get("timeoutMin") or DEFAULT_TIMEOUT_MIN),
                        categoria=CATEGORY, forced=forced)
    service = f"{SERVICE_PREFIX}{project_key.lower()}"
    group = cfg.get("servizi") or {}
    for key in (service, f"{SERVICE_PREFIX}{project_key}"):
        if isinstance(group.get(key), dict):
            c = group[key]
            coda = _entries(c.get("coda"))
            if coda:
                return Plan(str(inst), coda, float(c.get("timeoutMin") or DEFAULT_TIMEOUT_MIN), servizio=key)
    c = (cfg.get("categorie") or {}).get(CATEGORY)
    if not isinstance(c, dict) or not _entries(c.get("coda")):
        return None
    return Plan(str(inst), _entries(c.get("coda")), float(c.get("timeoutMin") or DEFAULT_TIMEOUT_MIN),
                categoria=CATEGORY)


# ---- /modello as --forza, per project ------------------------------------------------

def _forza_path() -> Path:
    return Path(os.environ.get("WAKE_FORZA_PATH") or "/app/data/wake_forza.json")


def _read_forza() -> dict[str, str]:
    try:
        data = json.loads(_forza_path().read_text(encoding="utf-8"))
        return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def get_forza(project_key: str | None) -> str | None:
    """The ``--forza`` pinned with /modello for this project's wakes, if any."""
    return _read_forza().get((project_key or "").lower()) if project_key else None


def set_forza(project_key: str, spec: str | None) -> None:
    """Pin (or with ``None`` clear) the forced entry of a project's wakes."""
    data = _read_forza()
    key = project_key.lower()
    if spec:
        data[key] = parse_force(spec).spec()
    else:
        data.pop(key, None)
    path = _forza_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


# ---- the fallback, on the host ------------------------------------------------------

@dataclass
class FallbackResult:
    ok: bool
    text: str = ""
    attempts: list[dict] = field(default_factory=list)
    error: str = ""

    @property
    def winner(self) -> str:
        return next((a.get("voce", "") for a in self.attempts if a.get("esito") == "ok"), "")


def route_request(plan: Plan, *, cwd: str, prompt: str, extra_env: dict | None) -> bytes:
    """The JSON line the host wrapper's ``llm-route`` op reads."""
    req: dict = {
        "op": OP_LLM_ROUTE,
        "instance": plan.instance,
        "cwd": cwd,
        "prompt": prompt,
        "env": {k: v for k, v in (extra_env or {}).items() if str(k).startswith("HUB_") and v},
    }
    if plan.forced:
        req["forza"] = plan.forced.spec()
    else:
        req["servizio" if plan.servizio else "categoria"] = plan.servizio or plan.categoria
        req["skip"] = 1 if plan.primary else 0
    return (json.dumps(req) + "\n").encode()


def parse_route_reply(stdout: bytes, stderr: bytes, rc: int | None) -> FallbackResult:
    """The wrapper prints one JSON object as its last stdout line."""
    for line in reversed((stdout or b"").decode(errors="replace").splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            data = json.loads(line)
        except ValueError:
            continue
        return FallbackResult(
            ok=bool(data.get("ok")),
            text=str(data.get("text") or ""),
            attempts=[a for a in data.get("attempts") or [] if isinstance(a, dict)],
            error=str(data.get("error") or ""),
        )
    err = (stderr or b"").decode(errors="replace").strip()[-300:]
    return FallbackResult(False, error=f"wrapper exit {rc}: {err or 'nessuna risposta'}")


def fallback_timeout_s(plan: Plan) -> float:
    return len(plan.fallback) * plan.timeout_min * 60 + FALLBACK_MARGIN_S


async def run_fallback(plan: Plan, *, cwd: str, prompt: str, extra_env: dict | None) -> FallbackResult:
    """Walk ``plan.fallback`` on the host through the engine; never raises."""
    import claude_runner  # late: claude_runner is heavy and the tests fake it

    if not plan.fallback:
        return FallbackResult(False, error="nessun elemento successivo in coda")
    try:
        proc = await asyncio.create_subprocess_exec(
            *claude_runner.host_exec_command(),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as exc:
        return FallbackResult(False, error=f"ssh non avviato: {exc}")
    # stdin stays open: EOF is how the wrapper learns we gave up (kills the engine).
    proc.stdin.write(route_request(plan, cwd=cwd, prompt=prompt, extra_env=extra_env))
    await proc.stdin.drain()
    reader = asyncio.gather(proc.stdout.read(), proc.stderr.read())
    try:
        out, err = await asyncio.wait_for(reader, timeout=fallback_timeout_s(plan))
        await proc.wait()
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        return FallbackResult(False, error=f"ripiego oltre {int(fallback_timeout_s(plan))} s")
    finally:
        if not proc.stdin.is_closing():
            proc.stdin.close()
    return parse_route_reply(out, err, proc.returncode)


def instance_with_service(servizio: str, env=None) -> Path | None:
    """The instance whose llm-routing.json declares ``servizio``: the device's
    default first, then by band (the order of the file is not a choice: on the
    minipc it put comunity before personal, and its accounts)."""
    try:
        registry, _ = instances.load_registry(env)
        default = instances.default_instance_id(env)
    except Exception as exc:
        logger.warning("llm service %s: device registry unreadable: %s", servizio, exc)
        return None
    for inst in sorted(registry.values(), key=lambda i: (i.id != default, i.band)):
        try:
            cfg = json.loads((Path(inst.path) / CONFIG_REL).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance((cfg.get("servizi") or {}).get(servizio), dict):
            return Path(inst.path)
    return None


async def run_service(
    instance: Path, servizio: str, *, cwd: str, prompt: str, timeout_s: float
) -> FallbackResult:
    """One declared service (queue computed by the engine's matrix) on the host.

    Same ``llm-route`` op as the wake fallback, ``skip=0``: the whole queue runs,
    each failure falling to the next entry. Never raises."""
    import claude_runner  # late: see run_fallback

    try:
        proc = await asyncio.create_subprocess_exec(
            *claude_runner.host_exec_command(),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as exc:
        return FallbackResult(False, error=f"ssh non avviato: {exc}")
    req = {"op": OP_LLM_ROUTE, "instance": str(instance), "cwd": cwd, "prompt": prompt,
           "env": {}, "servizio": servizio, "skip": 0}
    proc.stdin.write((json.dumps(req) + "\n").encode())
    await proc.stdin.drain()
    try:
        out, err = await asyncio.wait_for(
            asyncio.gather(proc.stdout.read(), proc.stderr.read()), timeout=timeout_s
        )
        await proc.wait()
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        return FallbackResult(False, error=f"servizio {servizio} oltre {int(timeout_s)} s")
    finally:
        if not proc.stdin.is_closing():
            proc.stdin.close()
    return parse_route_reply(out, err, proc.returncode)


# An entry whose CLI is not logged in on this host: as unusable as a missing key.
_NOT_LOGGED_IN = ("failed to authenticate", "not logged in", "please run /login", "access token has expired")


def _unusable_here(attempt: dict) -> bool:
    if attempt.get("esito") == "skipped":
        return True
    motivo = str(attempt.get("motivo") or "").lower()
    return any(m in motivo for m in _NOT_LOGGED_IN)


def nothing_runs_here(res: FallbackResult) -> bool:
    """No entry could reach a model: engine not started, skipped (no key, no CLI), not logged in.

    Not a failed queue: the engine is out of reach here exactly as when no host
    exec is configured, and the caller keeps its legacy path. minipc 2026-10-25:
    the host's claude-development login had expired and every context service
    answered ❌ while the container's runner still worked."""
    if res.ok:
        return False
    if not res.attempts:
        # the wrapper died before the queue (VPS comunity 2026-10-25: Life's python
        # failed an import): nothing was called. A timeout instead did run something.
        return " oltre " not in res.error
    return all(_unusable_here(a) for a in res.attempts)


def service_timeout_s(instance: Path, servizio: str) -> float:
    """Whole-queue budget of ``servizio``: every entry may use its ``timeoutMin``."""
    try:
        cfg = json.loads((instance / CONFIG_REL).read_text(encoding="utf-8"))
        svc = (cfg.get("servizi") or {})[servizio]
        per_entry = float(svc.get("timeoutMin") or DEFAULT_TIMEOUT_MIN)
        entries = max(1, len(svc.get("coda") or []))
    except (OSError, ValueError, KeyError, TypeError):
        per_entry, entries = DEFAULT_TIMEOUT_MIN, 1
    return entries * per_entry * 60 + FALLBACK_MARGIN_S


async def run_node_service(servizio: str, node_key: str, prompt: str, *, persona: str | None = None):
    """One-shot run of a declared service, on the host, in the node's folder.

    The non-interactive jobs (agent notifications, taskgen, butler) used to call
    ``run_claude`` directly: their model and fallback now belong to the queue of
    ``servizio`` in the instance's llm-routing.json. Returns a ``RunResult``
    (no session id, no usage: the engine logs its own) — a failed queue is an
    answer, ``❌ …``, never a retry elsewhere.

    ``None`` = the engine is out of reach here (no host exec, no instance
    declares ``servizio``, the node has no host path): the caller keeps its
    legacy ``run_claude``, like :func:`reparto_routing.service_chooser`.
    """
    import claude_runner  # late: see run_fallback
    import projects

    if not claude_runner.host_exec_enabled():
        return None
    inst = instance_with_service(servizio)
    project = projects.resolve_project(node_key)
    host_cwd = (getattr(project, "host_path", "") or "").strip()
    if inst is None or not host_cwd:
        return None
    # The engine has no system-prompt slot: the persona rides in front, as on opencode.
    task = f"{persona}\n\n---\n\n{prompt}" if persona else prompt
    res = await run_service(inst, servizio, cwd=host_cwd, prompt=task,
                            timeout_s=service_timeout_s(inst, servizio))
    for a in res.attempts:
        logger.info("[llm-routing] %s %s=%s %s", servizio, a.get("voce"), a.get("esito"), a.get("motivo") or "")
    if res.ok:
        return claude_runner.RunResult(res.text, None)
    if nothing_runs_here(res):
        logger.warning("[llm-routing] %s: no entry of the queue runs on this host, legacy runner", servizio)
        return None
    why = res.error or "; ".join(f"{a.get('voce')}: {a.get('motivo')}" for a in res.attempts)
    return claude_runner.RunResult(f"❌ coda {servizio} esaurita: {why}", None)


def attempts_text(primary_reason: str | None, plan: Plan, res: FallbackResult | None) -> str:
    """One line per attempt, for the notice when the whole queue failed."""
    lines = []
    if plan and plan.primary and primary_reason:
        lines.append(f"- {primary_label(plan.primary)}: {primary_reason}")
    for a in (res.attempts if res else []):
        lines.append(f"- {a.get('voce', '?')}: {a.get('esito', '?')}"
                     + (f" ({a['motivo']})" if a.get("motivo") else ""))
    if res and res.error and not res.attempts:
        lines.append(f"- ripiego: {res.error}")
    return "\n".join(lines)
