"""taskgen_config — pure dataclasses/config for the taskgen engine.

No I/O, no bot import (graph aciclico: taskgen* non importa mai bot.py).
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Suggestion:
    """A single suggestion emitted by a watcher's LLM run."""

    title: str
    category: str
    when_iso: str
    duration_min: int
    description: str | None
    why: str | None


@dataclass
class Watcher:
    """A configured taskgen watcher (node + cadence + gate + recipient)."""

    key: str
    node_key: str
    cadence: str
    pre_gate: str
    enabled: bool
    recipient_profile: str
    gate_cmd: str | None = None


@dataclass
class TrustPolicy:
    """Per-category trust policy with a daily emission cap."""

    categories: dict[str, str] = field(default_factory=dict)
    daily_cap: int = 0
    default_category_id: int | None = None

    def decision(self, category: str) -> str:
        """Return "trusted" or "propose" for a category (default: propose)."""
        return self.categories.get(category, "propose")


def load_watchers(cfg: dict) -> list[Watcher]:
    """Parse the ``watchers`` list of a taskgen config dict into ``Watcher``s.

    Pure: no I/O, no defaults magic beyond dataclass field types. Missing
    optional-ish keys (``enabled``) default sensibly; malformed entries are
    skipped with the rest of the list still returned (best-effort, mirrors
    ``parse_suggestions``' "never raises" posture for a config file that a
    human might hand-edit).
    """
    out: list[Watcher] = []
    for entry in cfg.get("watchers") or []:
        try:
            out.append(
                Watcher(
                    key=entry["key"],
                    node_key=entry["node_key"],
                    cadence=entry["cadence"],
                    pre_gate=entry.get("pre_gate", "temporal"),
                    enabled=bool(entry.get("enabled", True)),
                    recipient_profile=entry["recipient_profile"],
                    gate_cmd=entry.get("gate_cmd"),
                )
            )
        except (KeyError, TypeError):
            continue
    return out


def load_trust_policy(cfg: dict) -> TrustPolicy:
    """Parse the ``trust`` section of a taskgen config dict into a ``TrustPolicy``."""
    trust = cfg.get("trust") or {}
    categories = trust.get("categories") or {}
    daily_cap = trust.get("daily_cap", 0)
    default_category_id = trust.get("default_category_id")
    return TrustPolicy(
        categories=dict(categories),
        daily_cap=int(daily_cap),
        default_category_id=(
            int(default_category_id) if default_category_id is not None else None
        ),
    )
