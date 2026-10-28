"""What a run cost, shipped to Life's ledger (``POST /api/llm/runs``).

Both engines already declare their own spend on every run — claude in the
``result`` event, opencode in each ``step-finish`` — and until issue #28 the
Coordinator read the reply out of those events and dropped the rest. This module
is the other half: a value object for the measurement (:class:`RunUsage`, filled
by ``claude_runner``) and a fire-and-forget emitter that posts it.

Why the ledger lives in Life and not here
-----------------------------------------
Every node owns its own data, but *the sum over the network belongs to no node* —
the same argument as the port registry. The Coordinator is the only process that
can observe a run, so it is the only one that can emit it; the totalling, and the
question "which model does this node actually run", belong upstream.

Two rules inherited from the ledger (see ``Life/backend/life_backend/llm_usage_routes.py``)
-------------------------------------------------------------------------------------------
* **A killed run does not cost zero.** When the watchdog fires, the money was
  spent but is not knowable: emit ``outcome="timeout"`` with ``cost_usd=None``.
  A 0 there would make the most expensive runs look like the cheapest.
* **The cost is never recomputed.** ``cost_usd`` is what the engine reported. A
  price table copied into this repo would go stale in silence and then misreport
  the whole history.

Emission never blocks and never raises: a run's *purpose* is the reply, and an
unreachable ledger must not cost the user their answer. Failures are logged once
per condition and the measurement is dropped.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass, field
from datetime import UTC, datetime

import httpx

logger = logging.getLogger("coordinator.llm_usage")

LIFE_BACKEND_URL = os.environ.get("LIFE_BACKEND_URL", "http://localhost:9999").rstrip(
    "/"
)
LIFE_ADMIN_TOKEN = os.environ.get("LIFE_ADMIN_TOKEN", "").strip()
LLM_USAGE_TIMEOUT_S = float(os.environ.get("LLM_USAGE_TIMEOUT_SEC", "5"))

OUTCOME_OK = "ok"
OUTCOME_TIMEOUT = "timeout"
OUTCOME_ERROR = "error"


@dataclass
class RunUsage:
    """One measured run, engine-agnostic and already in the ledger's vocabulary.

    ``cost_usd is None`` is meaningful: *spent, unknowable*. It is never
    coerced to 0 — see the module docstring.
    """

    engine: str
    model: str
    outcome: str = OUTCOME_OK
    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    duration_ms: int | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    reasoning_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None
    cost_usd: float | None = None

    def payload(self, *, node: str, caller: str) -> dict:
        """The ledger row for this run, attributed to ``node`` / ``caller``.

        ``node`` is lower-cased because the aggregates are grouped on the raw
        string: ``homeResearcher`` and ``homeresearcher`` arriving from two
        different emitters would silently split one node's spend in two.
        """
        return {
            "started_at": self.started_at.astimezone(UTC).isoformat(),
            "node": node.strip().lower(),
            "caller": caller.strip(),
            "engine": self.engine,
            "model": self.model,
            "outcome": self.outcome,
            "duration_ms": self.duration_ms,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "reasoning_tokens": self.reasoning_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_write_tokens": self.cache_write_tokens,
            "cost_usd": self.cost_usd,
        }


def usage_of(result) -> RunUsage | None:
    """The usage riding along on a runner result, or ``None``.

    Deliberately duck-typed: ``run_claude`` is injected (hub, providers,
    scheduler) and every test fake returns a plain 2-tuple. Reading the
    attribute off whatever came back keeps those call sites working untouched —
    they just measure nothing, which is the truth.
    """
    usage = getattr(result, "usage", None)
    return usage if isinstance(usage, RunUsage) else None


# Strong refs to in-flight emissions: asyncio only weakly references tasks, so
# without this a fire-and-forget POST can be garbage-collected mid-flight.
_pending: set[asyncio.Task] = set()

# Cause of the last failed emission, so a durable outage is reported once
# instead of once per run (see emit()).
_last_failure: str | None = None


def record(result_or_usage, *, node: str, caller: str) -> None:
    """Ship the measurement, if there is one, without blocking the caller.

    Accepts either a runner result or a :class:`RunUsage` so call sites can pass
    whatever they have. A result with no usage attached (an injected fake, a
    backend that reported nothing) is a silent no-op: nothing was measured, so
    there is nothing to claim.
    """
    usage = (
        result_or_usage
        if isinstance(result_or_usage, RunUsage)
        else usage_of(result_or_usage)
    )
    if usage is None:
        return
    if not LIFE_ADMIN_TOKEN:
        logger.debug("llm usage: LIFE_ADMIN_TOKEN unset, run not recorded")
        return
    try:
        task = asyncio.get_running_loop().create_task(
            emit(usage, node=node, caller=caller)
        )
    except RuntimeError:  # no loop (sync context): drop rather than block
        logger.debug("llm usage: no running loop, run not recorded")
        return
    _pending.add(task)
    task.add_done_callback(_pending.discard)


async def emit(usage: RunUsage, *, node: str, caller: str) -> bool:
    """POST one row to the ledger. Returns whether it landed; never raises."""
    global _last_failure
    if not LIFE_ADMIN_TOKEN:
        return False
    payload = usage.payload(node=node, caller=caller)
    try:
        async with httpx.AsyncClient(timeout=LLM_USAGE_TIMEOUT_S) as client:
            resp = await client.post(
                f"{LIFE_BACKEND_URL}/api/llm/runs",
                json=payload,
                headers={"X-Life-Token": LIFE_ADMIN_TOKEN},
            )
            resp.raise_for_status()
    except Exception as exc:
        # Never louder than a warning: the reply already reached the user, and a
        # ledger outage must not read like a failed run. And only the FIRST of a
        # run of identical failures is a warning: a ledger that is down (or a
        # Life whose 0006 migration was never applied) fails on every single run
        # for days, and one line per run would bury the logs it shares with the
        # bot. The next distinct cause, or the next success, re-arms it.
        cause = f"{type(exc).__name__}:{exc}"[:200]
        if cause == _last_failure:
            logger.debug("llm usage: POST failed again (%s)", cause)
        else:
            _last_failure = cause
            logger.warning(
                "llm usage: POST failed for %s/%s (%s): %s — further identical "
                "failures logged at DEBUG",
                payload["node"],
                payload["caller"],
                usage.model,
                exc,
            )
        return False
    _last_failure = None
    logger.info(
        "llm usage: %s/%s %s %s outcome=%s cost=%s",
        payload["node"],
        payload["caller"],
        usage.engine,
        usage.model,
        usage.outcome,
        "?" if usage.cost_usd is None else f"${usage.cost_usd:.4f}",
    )
    return True
