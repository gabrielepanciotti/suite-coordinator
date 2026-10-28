"""Issue #47: the container must never share a `.venv` with the host."""
from __future__ import annotations

import importlib.util
import os
import pathlib

REPO = pathlib.Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("gen_venv_masks", REPO / "scripts" / "gen_venv_masks.py")
gvm = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gvm)


def _repo(tmp_path, name, *venvs):
    root = tmp_path / name
    root.mkdir()
    for v in venvs:
        (root / v / "bin").mkdir(parents=True)
    return root


def test_workspace_mounts_reads_every_workspace_line_of_the_real_compose():
    compose = REPO / "docker-compose.yml"
    if not compose.is_file():
        import pytest
        pytest.skip("workspace mounts are installation-specific and the production compose is excluded")
    mounts = gvm.workspace_mounts(compose.read_text(encoding="utf-8"))
    targets = {t for _, t in mounts}
    assert ("WS_NETWORK", "/workspaces/network") in mounts
    assert "/workspaces/home/children/homeResearcher" in targets
    # read-only and non-workspace mounts are not venv carriers
    assert not any(t.startswith("/life") for t in targets)


def test_masks_only_venvs_that_exist_including_backend(tmp_path):
    net = _repo(tmp_path, "Network", ".venv")
    off = _repo(tmp_path, "Offerte", ".venv", "backend/.venv", "frontend/node_modules/x/.venv")
    bare = _repo(tmp_path, "Launcher")
    mounts = [("WS_NETWORK", "/workspaces/network"), ("WS_OFFERTE", "/workspaces/offerte"),
              ("WS_LAUNCHER", "/workspaces/launcher"), ("WS_UNSET", "/workspaces/ghost")]
    env = {"WS_NETWORK": str(net), "WS_OFFERTE": str(off), "WS_LAUNCHER": str(bare)}

    dests = [d for _, d in gvm.plan(mounts, env, tmp_path / "masks")]

    # A repo without .venv gets no mask: Docker would create a root-owned
    # mount point inside the host repo.
    assert dests == [
        "/workspaces/network/.venv",
        "/workspaces/offerte/.venv",
        "/workspaces/offerte/backend/.venv",
    ]


def test_masks_live_outside_the_repo_and_are_shared_by_double_mounts(tmp_path):
    launcher = _repo(tmp_path, "Launcher", "backend/.venv")
    mounts = [("WS_LAUNCHER", "/workspaces/launcher"), ("WS_LAUNCHER", "/workspaces/dashboard")]
    entries = gvm.plan(mounts, {"WS_LAUNCHER": str(launcher)}, tmp_path / "masks")

    srcs = {src for src, _ in entries}
    assert len(entries) == 2 and len(srcs) == 1
    (src,) = srcs
    assert src.parent == tmp_path / "masks"
    assert launcher not in src.parents


def test_main_writes_override_creates_sources_and_check_detects_drift(tmp_path, monkeypatch):
    net = _repo(tmp_path, "Network", ".venv")
    (tmp_path / "docker-compose.yml").write_text(
        "      - ${WS_NETWORK:-./.ws-placeholder}:/workspaces/network\n", encoding="utf-8"
    )
    (tmp_path / ".env").write_text(
        f"WS_NETWORK={net}\nVENV_MASK_ROOT={tmp_path / 'masks'}\n", encoding="utf-8"
    )
    monkeypatch.delenv("VENV_MASK_ROOT", raising=False)
    # The shell environment wins over .env (compose semantics), and other tests
    # load the real .env into os.environ: keep only this test's WS_* values.
    for key in [k for k in os.environ if k.startswith("WS_")]:
        monkeypatch.delenv(key)
    args = ["--compose-dir", str(tmp_path)]

    assert gvm.main(args + ["--check"]) == 1
    assert gvm.main(args) == 0
    override = (tmp_path / "docker-compose.override.yml").read_text(encoding="utf-8")
    assert override.startswith(gvm.MARKER)
    assert ":/workspaces/network/.venv" in override
    # The source exists before Docker sees it, so it is not created root-owned.
    assert len(list((tmp_path / "masks").iterdir())) == 1
    assert gvm.main(args + ["--check"]) == 0

    (net / "backend" / ".venv").mkdir(parents=True)
    assert gvm.main(args + ["--check"]) == 1


def test_main_never_overwrites_a_hand_written_override(tmp_path):
    (tmp_path / "docker-compose.yml").write_text("", encoding="utf-8")
    hand = tmp_path / "docker-compose.override.yml"
    hand.write_text("services: {}\n", encoding="utf-8")

    assert gvm.main(["--compose-dir", str(tmp_path)]) == 2
    assert hand.read_text(encoding="utf-8") == "services: {}\n"
