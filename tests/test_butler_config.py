"""Tests for butler_config — pure watcher/policy parsing (no I/O, no bot import)."""

from butler_config import (
    ButlerPolicy,
    ButlerTrust,
    ButlerWatcher,
    load_butler_action_trust,
    load_butler_policy,
    load_butler_trust,
    load_butler_watchers,
    watcher_for_kind,
)


def _kind_cfg(**overrides):
    reorg = {
        "key": "reorg-owner",
        "kind": "reorg",
        "cadence": "cron:0 8 * * *",
        "recipient_profile": "owner",
    }
    reorg.update(overrides)
    prep = {
        "key": "prep-owner",
        "kind": "prep",
        "cadence": "cron:0 */3 * * *",
        "recipient_profile": "owner",
    }
    return {"watchers": [reorg, prep]}


def test_watcher_for_kind_finds_by_kind():
    assert watcher_for_kind(_kind_cfg(), "reorg").key == "reorg-owner"
    assert watcher_for_kind(_kind_cfg(), "prep").key == "prep-owner"


def test_watcher_for_kind_finds_disabled_watcher():
    # A manual command must work on a watcher whose autonomous job is off.
    watcher = watcher_for_kind(_kind_cfg(enabled=False), "reorg")
    assert watcher is not None
    assert watcher.enabled is False


def test_watcher_for_kind_returns_none_when_absent():
    assert watcher_for_kind({"watchers": []}, "reorg") is None
    assert watcher_for_kind(_kind_cfg(), "nonesuch") is None


def test_watcher_for_kind_survives_malformed_config():
    assert watcher_for_kind({}, "reorg") is None
    assert watcher_for_kind({"watchers": None}, "reorg") is None
    assert watcher_for_kind({"watchers": [None, 42, {"key": "x"}]}, "reorg") is None
    cfg = {"watchers": [{"kind": "reorg"}, _kind_cfg()["watchers"][0]]}
    assert watcher_for_kind(cfg, "reorg").key == "reorg-owner"


def test_watcher_for_kind_returns_first_match():
    cfg = _kind_cfg()
    cfg["watchers"].append(
        {
            "key": "reorg-second",
            "kind": "reorg",
            "cadence": "cron:0 9 * * *",
            "recipient_profile": "owner",
        }
    )
    assert watcher_for_kind(cfg, "reorg").key == "reorg-owner"


def test_load_butler_watchers_parses_valid_entries():
    cfg = {
        "watchers": [
            {
                "key": "reorg-owner",
                "kind": "reorg",
                "cadence": "cron:0 * * * *",
                "recipient_profile": "owner",
                "node_key": "mycorise",
            },
            {
                "key": "prep-owner",
                "kind": "prep",
                "cadence": "interval:600s",
                "recipient_profile": "owner",
            },
        ]
    }
    out = load_butler_watchers(cfg)
    assert len(out) == 2
    assert out[0] == ButlerWatcher(
        key="reorg-owner",
        kind="reorg",
        cadence="cron:0 * * * *",
        horizon_hours=48,
        enabled=True,
        recipient_profile="owner",
        node_key="mycorise",
    )
    assert out[1] == ButlerWatcher(
        key="prep-owner",
        kind="prep",
        cadence="interval:600s",
        horizon_hours=6,
        enabled=True,
        recipient_profile="owner",
        node_key=None,
    )


def test_load_butler_watchers_default_horizon_by_kind():
    cfg = {
        "watchers": [
            {"key": "r", "kind": "reorg", "cadence": "cron:*", "recipient_profile": "owner"},
            {"key": "p", "kind": "prep", "cadence": "cron:*", "recipient_profile": "owner"},
            {"key": "x", "kind": "other", "cadence": "cron:*", "recipient_profile": "owner"},
        ]
    }
    out = load_butler_watchers(cfg)
    horizons = {w.key: w.horizon_hours for w in out}
    assert horizons["r"] == 48
    assert horizons["p"] == 6
    assert horizons["x"] == 6


def test_load_butler_watchers_explicit_overrides_default_and_enabled():
    cfg = {
        "watchers": [
            {
                "key": "r",
                "kind": "reorg",
                "cadence": "cron:*",
                "horizon_hours": 12,
                "enabled": False,
                "recipient_profile": "owner",
            }
        ]
    }
    out = load_butler_watchers(cfg)
    assert out[0].horizon_hours == 12
    assert out[0].enabled is False


def test_load_butler_watchers_skips_malformed_entries():
    cfg = {
        "watchers": [
            {"kind": "reorg", "cadence": "cron:*"},  # missing key
            {"key": "r", "cadence": "cron:*"},  # missing kind
            {"key": "r", "kind": "reorg"},  # missing cadence
            "not-a-dict",
            {"key": "ok", "kind": "prep", "cadence": "cron:*", "recipient_profile": "owner"},
        ]
    }
    out = load_butler_watchers(cfg)
    assert len(out) == 1
    assert out[0].key == "ok"


def test_load_butler_watchers_empty_config():
    assert load_butler_watchers({}) == []
    assert load_butler_watchers({"watchers": []}) == []
    assert load_butler_watchers({"watchers": None}) == []


def test_load_butler_policy_defaults():
    policy = load_butler_policy({})
    assert policy.reorg_apply_mode == "propose"
    assert policy.prep_output_mode == "readonly_artifact"
    assert policy.trigger_mode == "autonomous"
    assert policy.per_agent == {}


def test_load_butler_policy_parses_overrides():
    cfg = {
        "policy": {
            "reorg_apply_mode": "auto_apply_trusted",
            "per_agent": {"mycorise": {"reorg_apply_mode": "auto_apply_trusted"}},
        }
    }
    policy = load_butler_policy(cfg)
    assert policy.reorg_apply_mode == "auto_apply_trusted"
    assert policy.per_agent == {"mycorise": {"reorg_apply_mode": "auto_apply_trusted"}}


def test_policy_resolve_per_agent_overrides_global():
    policy = ButlerPolicy(
        reorg_apply_mode="propose",
        per_agent={"mycorise": {"reorg_apply_mode": "auto_apply_trusted"}},
    )
    assert policy.resolve("reorg_apply_mode", "mycorise") == "auto_apply_trusted"


def test_policy_resolve_falls_back_to_global_for_other_node():
    policy = ButlerPolicy(
        reorg_apply_mode="propose",
        per_agent={"mycorise": {"reorg_apply_mode": "auto_apply_trusted"}},
    )
    assert policy.resolve("reorg_apply_mode", "personalChef") == "propose"


def test_policy_resolve_none_node_key_uses_global():
    policy = ButlerPolicy(
        reorg_apply_mode="propose",
        per_agent={"mycorise": {"reorg_apply_mode": "auto_apply_trusted"}},
    )
    assert policy.resolve("reorg_apply_mode", None) == "propose"


def test_policy_resolve_default_construction():
    policy = ButlerPolicy()
    assert policy.resolve("prep_output_mode", None) == "readonly_artifact"
    assert policy.resolve("trigger_mode", "anything") == "autonomous"


def test_load_butler_watchers_skips_missing_recipient_profile():
    # A watcher without recipient_profile is malformed -> skipped (not built with None).
    cfg = {"watchers": [
        {"key": "w1", "kind": "reorg", "cadence": "cron:0 8 * * *"},  # no recipient_profile
        {"key": "w2", "kind": "prep", "cadence": "cron:0 9 * * *", "recipient_profile": "owner"},
    ]}
    ws = load_butler_watchers(cfg)
    keys = [w.key for w in ws]
    assert keys == ["w2"]
    assert all(w.recipient_profile is not None for w in ws)


# --- ButlerTrust / load_butler_trust (#19) ------------------------------------


def test_butler_trust_defaults_to_propose():
    trust = ButlerTrust()
    assert trust.decision("accorpa") == "propose"
    assert trust.decision("sposta") == "propose"
    assert trust.daily_cap == 0


def test_butler_trust_returns_trusted_for_listed_kind():
    trust = ButlerTrust(kinds={"accorpa": "trusted"}, daily_cap=2)
    assert trust.decision("accorpa") == "trusted"
    assert trust.decision("sposta") == "propose"


def test_load_butler_trust_reads_section():
    trust = load_butler_trust(
        {"trust": {"kinds": {"accorpa": "trusted"}, "daily_cap": 3}}
    )
    assert isinstance(trust, ButlerTrust)
    assert trust.decision("accorpa") == "trusted"
    assert trust.daily_cap == 3


def test_load_butler_trust_missing_section_is_safe_default():
    trust = load_butler_trust({})
    assert trust.kinds == {}
    assert trust.daily_cap == 0
    assert trust.decision("accorpa") == "propose"


def test_load_butler_trust_malformed_section_is_safe_default():
    for cfg in (
        {"trust": "nonsense"},
        {"trust": {"kinds": ["accorpa"], "daily_cap": "molte"}},
        {"trust": None},
    ):
        trust = load_butler_trust(cfg)
        assert trust.kinds == {}
        assert trust.daily_cap == 0


# --- action_trust (#20) --------------------------------------------------------


def test_load_butler_action_trust_reads_section():
    trust = load_butler_action_trust(
        {"action_trust": {"kinds": {"email_draft": "trusted"}, "daily_cap": 2}}
    )
    assert isinstance(trust, ButlerTrust)
    assert trust.decision("email_draft") == "trusted"
    assert trust.daily_cap == 2


def test_load_butler_action_trust_missing_section_is_safe_default():
    trust = load_butler_action_trust({})
    assert trust.kinds == {}
    assert trust.daily_cap == 0
    assert trust.decision("email_draft") == "propose"


def test_load_butler_action_trust_malformed_section_is_safe_default():
    for cfg in (
        {"action_trust": "nonsense"},
        {"action_trust": {"kinds": ["email_draft"], "daily_cap": "tante"}},
        {"action_trust": None},
    ):
        trust = load_butler_action_trust(cfg)
        assert trust.kinds == {}
        assert trust.daily_cap == 0


def test_action_trust_and_reorg_trust_are_independent_sections():
    cfg = {
        "trust": {"kinds": {"accorpa": "trusted"}, "daily_cap": 5},
        "action_trust": {"kinds": {"email_draft": "trusted"}, "daily_cap": 1},
    }
    assert load_butler_trust(cfg).decision("email_draft") == "propose"
    assert load_butler_action_trust(cfg).decision("accorpa") == "propose"
    assert load_butler_action_trust(cfg).daily_cap == 1
