"""Nodes of type ``agent`` served by Life's common runtime (Life#157, contract §3).

A competence of type ``agent`` is a folder with a ``node.config`` (``type: "agent"``
plus an ``agent`` block) and its instructions: no app, no port, no listener. The
Coordinator stays the only conductor — it picks the department, the department
agent picks the node — and only what happens *after* the choice changes:

1. **Recognition**: the node's own ``node.config`` says ``type: "agent"``. Nothing
   new in the graph: the type is a fact of the node, not of the topology.
2. **Dry message** (bus wake, first Telegram contact): ``python -m suite_core.nodes
   agent run <dir> --json`` with the message on stdin; ``risposta`` is forwarded.
   Exit != 0 or ``esito: "errore"`` is said in the room and the caller falls back.
3. **Conversation**: the thread is bound to the node with agent ``<slug>`` (the file
   ``suite-apply`` materialises), not to Generale.
4. **Permissions**: never ``bypassPermissions`` — the tools are the manifest's.
5. **No bus presence**: no listener, no key (``bus_agents`` drops these nodes).

Leaf module: reads files and runs one subprocess, imports nothing of the bot.
The executor is injected, so no test spawns the runtime (nor ``claude``).
Spec: Life ``docs/superpowers/specs/2026-10-19-agent-node-design.md``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable

logger = logging.getLogger("coordinator.agent_nodes")

AGENT_TYPE = "agent"
# Mirrors the runtime's defaults (suite_core.runtime.agent): read-only, haiku.
DEFAULT_TOOLS = ("Read", "Glob", "Grep")
DEFAULT_MODEL = "haiku"
# The runtime's own claude timeout is 180s; a little more for interpreter start-up.
RUN_TIMEOUT_SEC = float(os.environ.get("AGENT_NODE_TIMEOUT_SEC", "240"))


@dataclass(frozen=True)
class AgentNode:
    """What the Coordinator needs of a manifest: who it is and what it may use."""

    node_dir: str
    slug: str
    tools: tuple[str, ...]
    model: str


@dataclass(frozen=True)
class AgentAnswer:
    """Outcome of one ``agent run``: ``ok`` with ``risposta``, or why not."""

    ok: bool
    risposta: str = ""
    dettaglio: str = ""


# (argv, stdin text, cwd, timeout) -> (exit code, stdout, stderr)
Executor = Callable[[list[str], str, str, float], Awaitable[tuple[int, str, str]]]


def _read_config(node_dir: str) -> dict | None:
    try:
        with open(os.path.join(node_dir, "node.config"), encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _is_agent_config(config: dict | None) -> bool:
    """Same rule as the runtime: says so, or carries the block and no other type."""
    if not isinstance(config, dict):
        return False
    if config.get("type") == AGENT_TYPE:
        return True
    return config.get("type") in (None, "") and isinstance(config.get("agent"), dict)


def is_agent_dir(node_dir: str | None) -> bool:
    return bool(node_dir) and _is_agent_config(_read_config(node_dir))


def node_of(node_dir: str | None) -> AgentNode | None:
    """The agent manifest at ``node_dir``, or None for any other kind of node.

    Never raises: a folder that cannot be read is simply not an agent here, and
    the runtime itself will say what is wrong with a broken manifest.
    """
    if not node_dir:
        return None
    config = _read_config(node_dir)
    if not _is_agent_config(config):
        return None
    block = config.get("agent") if isinstance(config.get("agent"), dict) else {}
    slug = str(config.get("slug") or Path(node_dir).name).strip().lower()
    raw_tools = block.get("strumenti")
    tools = (
        tuple(str(t).strip() for t in raw_tools if str(t).strip())
        if isinstance(raw_tools, list) and raw_tools
        else DEFAULT_TOOLS
    )
    model = str(block.get("modello") or DEFAULT_MODEL).strip() or DEFAULT_MODEL
    return AgentNode(node_dir=node_dir, slug=slug, tools=tools, model=model)


def run_command(node_dir: str) -> list[str]:
    return [sys.executable, "-m", "suite_core.nodes", "agent", "run", node_dir, "--json"]


async def _subprocess_executor(
    argv: list[str], stdin: str, cwd: str, timeout: float
) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        *argv,
        cwd=cwd,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(stdin.encode()), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise
    return proc.returncode or 0, out.decode(errors="replace"), err.decode(errors="replace")


async def answer(
    node: AgentNode, message: str, *, executor: Executor | None = None
) -> AgentAnswer:
    """One dry answer from the runtime. Every failure comes back as ``ok=False``."""
    run = executor or _subprocess_executor
    try:
        code, out, err = await run(run_command(node.node_dir), message, node.node_dir, RUN_TIMEOUT_SEC)
    except asyncio.TimeoutError:
        return AgentAnswer(False, dettaglio=f"nessuna risposta entro {int(RUN_TIMEOUT_SEC)}s")
    except Exception as exc:  # the room must hear about it, whatever it was
        return AgentAnswer(False, dettaglio=f"{type(exc).__name__}: {exc}"[:300])
    reply: dict = {}
    try:
        parsed = json.loads((out or "").strip() or "{}")
        reply = parsed if isinstance(parsed, dict) else {}
    except ValueError:
        pass
    risposta = str(reply.get("risposta") or "").strip()
    if code != 0 or reply.get("esito") != "ok" or not risposta:
        dettaglio = str(reply.get("dettaglio") or "").strip() or (err or out or "").strip()[-300:]
        logger.warning(
            "agent node %s: run fallita (exit %s, esito %r): %s",
            node.slug, code, reply.get("esito"), dettaglio,
        )
        return AgentAnswer(False, dettaglio=dettaglio or f"exit {code}")
    return AgentAnswer(True, risposta=risposta)


def failure_text(node: AgentNode, ans: AgentAnswer) -> str:
    """What the room reads when the runtime did not answer — never silence."""
    return (
        f"⚠️ La competenza «{node.slug}» non ha risposto ({ans.dettaglio or 'motivo ignoto'}). "
        "Passo il messaggio alla sessione del nodo."
    )
