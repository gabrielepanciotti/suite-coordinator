# tests/test_functionalities.py
import os
import textwrap
from pathlib import Path

import pytest

import functionalities as fz
import projects


def _ws(tmp_path, toml: str, agents=("menu-planner", "recipe-editor")):
    claude = tmp_path / ".claude"
    (claude / "agents").mkdir(parents=True)
    for a in agents:
        (claude / "agents" / f"{a}.md").write_text(
            f"---\nname: {a}\ndescription: {a} agent.\n---\nbody\n", encoding="utf-8")
    (claude / "functionalities.toml").write_text(textwrap.dedent(toml), encoding="utf-8")
    return projects.Project("x", "X", "\U0001F539", "", str(tmp_path))


VALID = """
    [[functionality]]
    key = "menu-settimana"
    label = "\U0001F4C5 Menu della settimana"
    agent = "menu-planner"
    seed = "[CMD:/menu]"

    [[functionality]]
    key = "aggiungi-ricetta"
    label = "➕ Aggiungi una ricetta"
    agent = "recipe-editor"
    companions = ["recipe-sous-chef"]
"""


def test_functionalities_of_parses_in_order(tmp_path):
    p = _ws(tmp_path, VALID)
    fs = fz.functionalities_of(p)
    assert [f.key for f in fs] == ["menu-settimana", "aggiungi-ricetta"]
    assert fs[0].label == "\U0001F4C5 Menu della settimana"
    assert fs[0].agent == "menu-planner" and fs[0].seed == "[CMD:/menu]"
    assert fs[1].companions == ("recipe-sous-chef",)
    assert fz.has_functionalities(p) is True


def test_entry_missing_required_field_is_skipped(tmp_path):
    p = _ws(tmp_path, """
        [[functionality]]
        label = "no key"
        agent = "menu-planner"

        [[functionality]]
        key = "ok"
        label = "\U0001F37D Ok"
        agent = "menu-planner"
    """)
    assert [f.key for f in fz.functionalities_of(p)] == ["ok"]


def test_absent_file_returns_empty(tmp_path):
    (tmp_path / ".claude" / "agents").mkdir(parents=True)
    p = projects.Project("y", "Y", "\U0001F539", "", str(tmp_path))
    assert fz.functionalities_of(p) == []
    assert fz.has_functionalities(p) is False


def test_resolve_functionality_by_key(tmp_path):
    p = _ws(tmp_path, VALID)
    assert fz.resolve_functionality(p, "menu-settimana").agent == "menu-planner"
    assert fz.resolve_functionality(p, "MENU-SETTIMANA").key == "menu-settimana"
    assert fz.resolve_functionality(p, "nope") is None
    assert fz.resolve_functionality(p, None) is None


def test_cache_invalidated_on_mtime(tmp_path):
    import os, time
    p = _ws(tmp_path, VALID)
    assert len(fz.functionalities_of(p)) == 2
    toml = tmp_path / ".claude" / "functionalities.toml"
    toml.write_text('[[functionality]]\nkey="solo"\nlabel="S"\nagent="menu-planner"\n',
                    encoding="utf-8")
    os.utime(toml, (time.time() + 5, time.time() + 5))  # bump mtime
    assert [f.key for f in fz.functionalities_of(p)] == ["solo"]


def _personalchef_workspace():
    """Locate the personalChef checkout without hardcoding any device's layout.

    Every project lives under the same root as this repo, so derive it from our own
    location; SRV_ROOT (or PERSONALCHEF_DIR) overrides it when the layout differs.
    Returns None -> the test skips, which is what happens on a host without it.
    """
    explicit = os.getenv("PERSONALCHEF_DIR")
    srv_root = os.getenv("SRV_ROOT") or Path(__file__).resolve().parents[2]
    for path in (explicit, Path(srv_root) / "PersonalChef"):
        if path and os.path.exists(os.path.join(path, ".claude", "functionalities.toml")):
            return str(path)
    return None


def test_personalchef_functionalities_reference_existing_agents():
    """Every agent/companion in personalChef's real TOML must exist as an agent."""
    ws = _personalchef_workspace()
    if ws is None:
        pytest.skip("personalChef workspace not found on this host")
    p = projects.Project("personalChef", "personalChef", "\U0001F373", ws, ws)
    names = {a.name for a in projects.agents_of(p)}
    fs = fz.functionalities_of(p)
    assert fs, "personalChef functionalities.toml should declare at least one entry"
    referenced = set()
    for f in fs:
        referenced.add(f.agent)
        referenced.update(f.companions)
    missing = referenced - names
    assert not missing, f"functionalities.toml references unknown agents: {missing}"
