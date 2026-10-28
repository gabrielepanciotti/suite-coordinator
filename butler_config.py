"""butler_config — pure dataclasses/config for the butler (maggiordomo) engine.

No I/O, no bot import (graph aciclico: butler* non importa mai bot.py).
Gemello di taskgen_config.py.
"""

from __future__ import annotations

from dataclasses import dataclass, field

_DEFAULT_HORIZON_HOURS = {
    "reorg": 48,
    "prep": 6,
}


@dataclass
class ButlerWatcher:
    """A configured butler watcher (reorg or prep) — kind + cadence + recipient."""

    key: str
    kind: str
    cadence: str
    horizon_hours: int
    enabled: bool
    recipient_profile: str
    node_key: str | None = None


@dataclass
class ButlerPolicy:
    """The mode-seam: default modes now, alternates wired later (#18-21).

    ``per_agent`` allows per-node_key overrides of any of the mode fields.
    """

    reorg_apply_mode: str = "propose"
    prep_output_mode: str = "readonly_artifact"
    trigger_mode: str = "autonomous"
    per_agent: dict = field(default_factory=dict)

    def resolve(self, field_name: str, node_key: str | None) -> str:
        """Resolve a mode field: per-agent override > global > default."""
        if node_key and node_key in self.per_agent:
            override = self.per_agent[node_key]
            if field_name in override:
                return override[field_name]
        return getattr(self, field_name)


@dataclass
class ButlerTrust:
    """Per-kind trust policy for reorg auto-apply, with a daily cap.

    Twin of ``taskgen_config.TrustPolicy``: the ``auto_apply_trusted`` mode
    (#19) may act on the person's schedule only for a ``kind`` explicitly
    marked "trusted" here, and only while the daily cap has room. Anything
    else stays a proposal — the default is deliberately the safe one.
    """

    kinds: dict[str, str] = field(default_factory=dict)
    daily_cap: int = 0

    def decision(self, kind: str) -> str:
        """Return "trusted" or "propose" for a reorg kind (default: propose)."""
        return self.kinds.get(kind, "propose")


def load_butler_watchers(cfg: dict) -> list[ButlerWatcher]:
    """Parse the ``watchers`` list of a butler config dict into ``ButlerWatcher``s.

    Pure: no I/O. Malformed entries (missing ``key``/``kind``/``cadence``) are
    skipped, best-effort, mirroring ``taskgen_config.load_watchers``. Missing
    ``horizon_hours`` defaults per ``kind`` (48 for "reorg", 6 otherwise);
    missing ``enabled`` defaults to True.
    """
    out: list[ButlerWatcher] = []
    for entry in cfg.get("watchers") or []:
        try:
            key = entry["key"]
            kind = entry["kind"]
            cadence = entry["cadence"]
        except (KeyError, TypeError):
            continue
        default_horizon = _DEFAULT_HORIZON_HOURS.get(kind, 6)
        try:
            out.append(
                ButlerWatcher(
                    key=key,
                    kind=kind,
                    cadence=cadence,
                    horizon_hours=int(entry.get("horizon_hours", default_horizon)),
                    enabled=bool(entry.get("enabled", True)),
                    recipient_profile=entry["recipient_profile"],
                    node_key=entry.get("node_key"),
                )
            )
        except (KeyError, TypeError, ValueError):
            continue
    return out


def watcher_for_kind(cfg: dict, kind: str) -> ButlerWatcher | None:
    """Return the first watcher of ``kind`` in a butler config dict, else None.

    Pure: no I/O. Used by the on-demand triggers (#21), which must work even on
    a watcher whose ``enabled`` is False — a manual command is an explicit
    request from the person, so the autonomous on/off switch does not gate it.
    """
    for watcher in load_butler_watchers(cfg):
        if watcher.kind == kind:
            return watcher
    return None


def load_butler_policy(cfg: dict) -> ButlerPolicy:
    """Parse the ``policy`` section of a butler config dict into a ``ButlerPolicy``."""
    policy = cfg.get("policy") or {}
    if not isinstance(policy, dict):
        policy = {}
    per_agent = policy.get("per_agent") or {}
    if not isinstance(per_agent, dict):
        per_agent = {}
    return ButlerPolicy(
        reorg_apply_mode=policy.get("reorg_apply_mode", "propose"),
        prep_output_mode=policy.get("prep_output_mode", "readonly_artifact"),
        trigger_mode=policy.get("trigger_mode", "autonomous"),
        per_agent=dict(per_agent),
    )


def _load_trust_section(cfg: dict, section: str) -> ButlerTrust:
    """Parse one ``ButlerTrust``-shaped section (``kinds`` + ``daily_cap``).

    Tolerant like ``load_butler_policy``: a missing or malformed section (or a
    malformed ``kinds``/``daily_cap``) degrades to the safe default — nothing
    trusted, no capacity — so a hand-edited config can never widen what the
    butler is allowed to do on its own.
    """
    trust = cfg.get(section) or {}
    if not isinstance(trust, dict):
        trust = {}
    kinds = trust.get("kinds") or {}
    if not isinstance(kinds, dict):
        kinds = {}
    try:
        daily_cap = int(trust.get("daily_cap", 0))
    except (TypeError, ValueError):
        daily_cap = 0
    return ButlerTrust(kinds=dict(kinds), daily_cap=daily_cap)


def load_butler_trust(cfg: dict) -> ButlerTrust:
    """Parse the ``trust`` section (reorg auto-apply, #19) into a ``ButlerTrust``."""
    return _load_trust_section(cfg, "trust")


def load_butler_action_trust(cfg: dict) -> ButlerTrust:
    """Parse the ``action_trust`` section (prep side effects, #20).

    Same shape and same tolerance as ``load_butler_trust``, deliberately reusing
    ``ButlerTrust`` rather than duplicating the dataclass. Note the DIFFERENT
    meaning: here "trusted" only decides whether an action is worth PROPOSING to
    the person — never whether it may be executed. Execution always requires an
    explicit tap (see ``butler_prep.run_prep_watcher``).
    """
    return _load_trust_section(cfg, "action_trust")
