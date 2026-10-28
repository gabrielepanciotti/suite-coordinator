# pipeline_stages.py
"""Pipeline stage declarations + the typed command/verdict bus protocol.

Pure logic — no I/O, no Telegram, no bus imports — so it unit-tests freely.
A stage either dispatches a typed `pipeline` command to its agent (implement/
test/deploy/verify) or is a human `gate`. Agents reply with a typed
`pipeline_verdict` in the message context (read via conv fetch, since the SSE
event omits context — same constraint the deployer handles).
"""
from __future__ import annotations

from dataclasses import dataclass, field

STAGE_TYPES = ("implement", "test", "gate", "deploy", "verify")
DEV_STAGES = frozenset({"implement", "test"})       # fail -> abort (no prod effect)
ROLLBACK_STAGES = frozenset({"deploy", "verify"})   # fail -> rollback


@dataclass(frozen=True)
class PipelineStageDecl:
    name: str
    type: str
    agent: str = ""                      # ignored for type == "gate"
    args: dict = field(default_factory=dict)

    @staticmethod
    def from_dict(d: dict) -> "PipelineStageDecl":
        return PipelineStageDecl(
            name=str(d["name"]), type=str(d["type"]),
            agent=str(d.get("agent", "")), args=dict(d.get("args") or {}),
        )


def to_dict(stage: PipelineStageDecl) -> dict:
    return {"name": stage.name, "type": stage.type,
            "agent": stage.agent, "args": dict(stage.args)}


def _target_suffix(agent: str) -> str:
    """Short label for a deploy target: `deployer-edge` -> `edge`."""
    return agent.split("-", 1)[1] if "-" in agent else agent


def default_stages(
    dev_agent: str, deploy_agent: str | list[str] | tuple[str, ...]
) -> list[PipelineStageDecl]:
    """Canonical pipeline used when a project declares no [[pipeline_stage]].

    `deploy_agent` may be several targets. A project that lives on one prod
    server (an internal product, or a public one) has one; the Coordinator has
    two, because its worker runs on BOTH prod servers and they must not drift
    apart — one approval, both updated. The extra targets become extra
    deploy/verify stages rather than a parallel fan-out inside one stage: the
    state machine already runs stages in sequence and awaits one verdict each,
    so nothing about it has to change, and a failure on the first server stops
    the pipeline before it touches the second.

    With a single target the stage names stay `deploy`/`verify` exactly as
    before — no existing pipeline or stored record changes shape.
    """
    targets = [deploy_agent] if isinstance(deploy_agent, str) else list(deploy_agent)
    stages = [
        PipelineStageDecl("implement", "implement", dev_agent),
        PipelineStageDecl("test", "test", dev_agent),
        PipelineStageDecl("gate", "gate"),
    ]
    solo = len(targets) == 1
    for agent in targets:
        sfx = "" if solo else f"-{_target_suffix(agent)}"
        stages.append(PipelineStageDecl(f"deploy{sfx}", "deploy", agent))
        stages.append(PipelineStageDecl(f"verify{sfx}", "verify", agent))
    return stages


def infra_command(stage: PipelineStageDecl) -> tuple[str, dict] | None:
    """Map a deploy/verify stage to a typed sidecar-deployer verb+args.

    Returns None for stage types the deployer has no verb for (implement/test/
    gate) — those keep going to a Claude agent via the plain `pipeline` block.
    """
    if stage.type == "deploy" and stage.args.get("rollback"):
        return "rollback", {"to_sha": str(stage.args.get("to_sha", ""))}
    if stage.type == "deploy":
        return "deploy", {}
    if stage.type == "verify":
        return "verify", {}
    return None


def build_command(pipeline_id: int, stage: PipelineStageDecl, task: str) -> dict:
    """The typed `context` carried on the dispatch message to a stage agent.

    The `pipeline` block is always present (correlation for the verdict echo,
    and the body a fallback Claude agent would follow). When the stage maps to
    a sidecar-deployer verb, a `deployer` block is added alongside it; the
    deployer executes that verb and ignores `pipeline` except to echo it back
    as correlation in its `pipeline_verdict` reply.
    """
    pipeline_block = {
        "id": pipeline_id, "stage": stage.name, "type": stage.type,
        "task": task, "args": dict(stage.args),
    }
    infra = infra_command(stage)
    if infra is not None:
        verb, iargs = infra
        return {"deployer": {"verb": verb, "args": iargs}, "pipeline": pipeline_block}
    return {"pipeline": pipeline_block}


def parse_verdict(context, pipeline_id: int, stage_name: str) -> dict | None:
    """Extract a `pipeline_verdict` matching (id, stage) from a message context.

    Returns a dict always including a ``blocked`` key. A blocked verdict has
    ``ok=False`` and carries ``decision_needed`` (the agent needs a human
    go/no-go before it can proceed). Returns None when absent/malformed/
    mismatched — meaning 'no usable verdict' — the orchestrator treats it as
    a failure, never a pass.
    """
    if not isinstance(context, dict):
        return None
    v = context.get("pipeline_verdict")
    if not isinstance(v, dict):
        return None
    try:
        vid = int(v.get("id", -1))
    except (TypeError, ValueError):
        return None
    if vid != pipeline_id or v.get("stage") != stage_name:
        return None
    if v.get("blocked"):
        return {"ok": False, "blocked": True, "summary": str(v.get("summary", "")),
                "decision_needed": str(v.get("decision_needed", "")), "sha": v.get("sha")}
    return {"ok": bool(v.get("ok")), "blocked": False, "summary": str(v.get("summary", "")),
            "sha": v.get("sha")}
