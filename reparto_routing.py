"""Two-level routing: first the department (*reparto*), then its agent picks the node.

``node_routing`` scores nodes by the domains and keywords their manifests declare.
That works for links and for nodes that bothered to declare words, and fails
quietly for everything else: "il bus non consegna i messaggi all'agente di
CallManager" names CallManager, yet it is Network's problem. Keywords cannot tell
"about X" from "mentions X"; a reader can.

Life#117 gives each cluster an optional ``reparto`` field: an agent
(``reparto-<cluster>``, materialised by ``suite-apply`` into ``.claude/agents/``)
and the list of nodes it may hand out. This module puts that agent between the
message and the deterministic router, with three rules:

1. **A link still wins.** A domain hit (score >= ``DOMAIN_WEIGHT``) is already a
   certain choice; asking a model to confirm it costs seconds and adds nothing.
2. **The agent only chooses inside its department.** A node outside ``nodi``, a
   node the registry cannot open, ``null``, garbage, a timeout, a crash: every one
   of these falls back to ``node_routing.route``. The agent can improve a route;
   it can never lose a message.
3. **The agent is injected.** ``route`` takes a ``Chooser``; production passes
   :func:`claude_chooser`, tests pass a fake. No test spawns ``claude``.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Awaitable, Callable

import claude_runner
import node_routing
import projects
import wake_routing

logger = logging.getLogger("coordinator.reparto_routing")

# A routing decision blocks the reply to a human: short, and overridable.
DEFAULT_TIMEOUT_SEC = 45.0
DEFAULT_MODEL = "haiku"
COORDINATOR_KEY = "coordinator"
# Declared in <istanza>/config/llm-routing.json (Life routing v2): model and fallback are the queue's.
SERVICE = "coordinator.instradaReparto"

# (agent name, message) -> raw agent output. Raises or returns garbage freely:
# ``route`` treats both as "no answer".
Chooser = Callable[[str, str], Awaitable[str]]

_JSON_RE = re.compile(r"\{[^{}]*\"nodo\"[^{}]*\}", re.S)


@dataclass(frozen=True)
class Reparto:
    """The ``reparto`` field of one cluster, validated."""

    cluster: str
    agente: str
    nodi: tuple[str, ...]


@dataclass(frozen=True)
class Choice:
    nodo: str | None
    motivo: str = ""


def reparto_of(cluster_id: str, node: dict | None) -> Reparto | None:
    """The cluster's department, or None when absent or malformed."""
    raw = (node or {}).get("reparto")
    if not isinstance(raw, dict):
        return None
    agente = raw.get("agente")
    nodi = raw.get("nodi")
    if not isinstance(agente, str) or not agente.strip() or not isinstance(nodi, dict):
        return None
    ids = tuple(k for k in nodi if isinstance(k, str) and k)
    if not ids:
        return None
    return Reparto(cluster=cluster_id, agente=agente.strip(), nodi=ids)


def reparto_for(
    root_id: str, nodes: dict[str, dict], deterministic: node_routing.Decision
) -> Reparto | None:
    """Which department a message in ``root_id``'s room belongs to.

    The root itself, when it is a department. Otherwise the department that holds
    every candidate the deterministic router came up with — if they disagree, or
    there are none, there is no department to ask and the caller keeps the
    deterministic answer.
    """
    own = reparto_of(root_id, nodes.get(root_id))
    if own is not None:
        return own
    departments = [
        rep
        for cid in node_routing.descendants(root_id, nodes)
        if (rep := reparto_of(cid, nodes.get(cid))) is not None
    ]
    if not departments:
        return None
    picked = {c.node_id for c in deterministic.candidates}
    if deterministic.kind == "unbuilt" and deterministic.planned:
        picked.add(deterministic.planned)
    if not picked:
        return None
    holders = [rep for rep in departments if picked <= set(rep.nodi)]
    return holders[0] if len(holders) == 1 else None


def parse_choice(raw: str) -> Choice | None:
    """``{"nodo": ..., "motivo": ...}`` out of the agent's output, or None.

    The agent is told to print one JSON line and nothing else; models still wrap
    it in prose or a code fence now and then, so the object is searched for, not
    assumed to be the whole output.
    """
    text = (raw or "").strip()
    candidates = [text, *_JSON_RE.findall(text)]
    for chunk in candidates:
        try:
            data = json.loads(chunk)
        except (ValueError, TypeError):
            continue
        if not isinstance(data, dict) or "nodo" not in data:
            continue
        nodo = data.get("nodo")
        motivo = str(data.get("motivo") or "").strip()
        if nodo is None:
            return Choice(nodo=None, motivo=motivo)
        if isinstance(nodo, str) and nodo.strip():
            return Choice(nodo=nodo.strip(), motivo=motivo)
    return None


def _timeout() -> float:
    try:
        return float(os.environ.get("REPARTO_TIMEOUT_SEC") or DEFAULT_TIMEOUT_SEC)
    except ValueError:
        return DEFAULT_TIMEOUT_SEC


def enabled() -> bool:
    """``REPARTO_ROUTING=off`` switches the agent off: pure deterministic routing."""
    return (os.environ.get("REPARTO_ROUTING") or "on").strip().lower() not in {
        "off", "0", "false", "no",
    }


def _agent_prompt(workspace: str | None, agent: str, text: str) -> str:
    """The department agent's instructions (its .md body) + the message to route."""
    path = Path(workspace or ".") / ".claude" / "agents" / f"{agent}.md"
    body = path.read_text(encoding="utf-8")
    if body.startswith("---"):
        body = body.split("---", 2)[-1]
    return f"{body.strip()}\n\nMessaggio da smistare: «{text}»"


async def service_chooser(agent: str, text: str) -> str | None:
    """Ask the instance's LLM queue (service ``coordinator.instradaReparto``).

    The queue and the fallback between models are the engine's (Life routing v2).
    ``None`` = no instance declares the service here (or no host exec): the
    caller keeps the legacy path. A failed queue raises, like any chooser error.
    """
    if not claude_runner.host_exec_enabled():
        return None
    inst = wake_routing.instance_with_service(SERVICE)
    project = projects.resolve_project(COORDINATOR_KEY)
    host_cwd = (getattr(project, "host_path", "") or "").strip()
    if inst is None or not host_cwd:
        return None
    prompt = _agent_prompt(project.workspace, agent, text)
    res = await wake_routing.run_service(
        inst, SERVICE, cwd=host_cwd, prompt=prompt, timeout_s=_timeout()
    )
    for a in res.attempts:
        logger.info("[llm-routing] %s %s=%s %s", SERVICE, a.get("voce"), a.get("esito"), a.get("motivo") or "")
    if wake_routing.nothing_runs_here(res):
        return None
    if not res.ok:
        raise RuntimeError(f"coda {SERVICE} fallita: {res.error or wake_routing.attempts_text(None, None, res)}")
    return res.text


async def claude_chooser(agent: str, text: str) -> str:
    """Route through the instance's LLM service; ``claude -p --agent`` if none here.

    The legacy path (boxes without a routing config or host exec) runs from the
    Coordinator's workspace: the agent definition lives in the repo's
    ``.claude/agents/``, and ``--agent`` resolves it relative to the project.
    """
    served = await service_chooser(agent, text)
    if served is not None:
        return served
    project = projects.resolve_project(COORDINATOR_KEY)
    cwd = project.workspace if project is not None else None
    model = os.environ.get("REPARTO_MODEL") or DEFAULT_MODEL
    proc = await asyncio.create_subprocess_exec(
        os.environ.get("CLAUDE_BIN") or "claude",
        "-p", "--agent", agent, "--model", model,
        f"Messaggio da smistare: «{text}»",
        cwd=cwd,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=_timeout())
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise
    if proc.returncode != 0:
        raise RuntimeError(
            f"claude exit {proc.returncode}: {err.decode(errors='replace')[-300:]}"
        )
    return out.decode(errors="replace")


async def route(
    text: str,
    root_id: str,
    nodes: dict[str, dict],
    chooser: Chooser | None = None,
) -> node_routing.Decision:
    """Deterministic routing, refined by the department agent when there is one."""
    deterministic = node_routing.route(text, root_id, nodes)
    if (
        deterministic.kind == "routed"
        and deterministic.candidates
        and deterministic.candidates[0].score >= node_routing.DOMAIN_WEIGHT
    ):
        return deterministic
    if not enabled() or not (text or "").strip():
        return deterministic
    rep = reparto_for(root_id, nodes, deterministic)
    if rep is None:
        return deterministic

    chooser = chooser or claude_chooser
    try:
        raw = await asyncio.wait_for(chooser(rep.agente, text), timeout=_timeout() + 5)
    except Exception as exc:  # timeout, missing binary, auth, anything
        logger.warning(
            "reparto %s: agente %s non ha risposto (%s), routing deterministico -> %s",
            rep.cluster, rep.agente, exc or type(exc).__name__, deterministic.node_id,
        )
        return deterministic

    choice = parse_choice(raw)
    if choice is None or choice.nodo is None:
        logger.info(
            "reparto %s: agente %s senza nodo (%s), routing deterministico -> %s",
            rep.cluster, rep.agente,
            choice.motivo if choice else f"risposta non valida: {raw[:120]!r}",
            deterministic.node_id,
        )
        return deterministic
    nodo = choice.nodo
    if nodo not in rep.nodi or nodo not in nodes:
        logger.warning(
            "reparto %s: agente %s ha scelto %r, fuori dal reparto: "
            "routing deterministico -> %s",
            rep.cluster, rep.agente, nodo, deterministic.node_id,
        )
        return deterministic

    if not (nodes.get(nodo) or {}).get("exists"):
        fallback = node_routing._nearest_built_ancestor(nodo, root_id, nodes)
        decision = node_routing.Decision(kind="unbuilt", node_id=fallback, planned=nodo)
    elif projects.resolve_project(nodo) is None:
        logger.warning(
            "reparto %s: nodo %r scelto dall'agente ma assente dal registro: "
            "routing deterministico -> %s",
            rep.cluster, nodo, deterministic.node_id,
        )
        return deterministic
    else:
        decision = node_routing.Decision(
            kind="routed",
            node_id=nodo,
            candidates=(node_routing.Candidate(node_id=nodo, score=0, reasons=(f"reparto:{rep.cluster}",)),),
        )
    logger.info(
        "reparto=%s agente=%s nodo=%s motivo=%s", rep.cluster, rep.agente, nodo, choice.motivo
    )
    return replace(decision, reparto=rep.cluster, motivo=choice.motivo)
