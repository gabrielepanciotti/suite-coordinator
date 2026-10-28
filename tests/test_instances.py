"""Instance registry resolution + per-instance channels (Coordinator#78, Life §4).

Two fake instances on a throwaway ``LIFE_HOME``: the operator must enumerate the
device registry, never pick one path, and never fall back to the default for an
unknown id (R-1). Mirrors the normative contract in Life's
``docs/instance-resolution.md``.
"""

from __future__ import annotations

import json

import pytest

import instances


def _write_home(tmp_path, entries: dict, *, default: str | None = None,
                make_dirs: bool = True) -> None:
    home = tmp_path / "life-home"
    home.mkdir(exist_ok=True)
    registry = {"version": 1, "instances": {}}
    for iid, entry in entries.items():
        if make_dirs and isinstance(entry, dict) and entry.get("path"):
            from pathlib import Path
            Path(entry["path"]).mkdir(parents=True, exist_ok=True)
        registry["instances"][iid] = entry
    (home / "instances.json").write_text(json.dumps(registry), encoding="utf-8")
    if default is not None:
        (home / "state.json").write_text(
            json.dumps({"version": 1, "default": default, "nextBand": 5}),
            encoding="utf-8",
        )


@pytest.fixture
def life_home(tmp_path, monkeypatch):
    home = tmp_path / "life-home"
    home.mkdir()
    monkeypatch.setenv("LIFE_HOME", str(home))
    monkeypatch.delenv("LIFE_INSTANCE", raising=False)
    monkeypatch.delenv("LIFE_INSTANCE_DIR", raising=False)
    monkeypatch.delenv("LIFE_PROFILE", raising=False)
    return home


def _two_instances(tmp_path) -> tuple[str, str]:
    personal = tmp_path / "life-personal"
    work = tmp_path / "life-work"
    return str(personal), str(work)


def test_load_registry_skips_malformed_entry(life_home, tmp_path):
    p, w = _two_instances(tmp_path)
    _write_home(tmp_path, {
        "personal": {"path": p, "profile": "personal", "band": 0},
        "work": {"path": w, "profile": "work", "band": 1},
        "broken": {"path": p, "band": "1"},  # band non int, profile assente
    })
    reg, warnings = instances.load_registry()
    assert set(reg) == {"personal", "work"}
    assert any("broken" in msg for msg in warnings)


def test_resolve_explicit_beats_default(life_home, tmp_path):
    p, w = _two_instances(tmp_path)
    _write_home(tmp_path, {
        "personal": {"path": p, "profile": "personal", "band": 0},
        "work": {"path": w, "profile": "work", "band": 1},
    }, default="personal")
    res = instances.resolve("work")
    assert res.id == "work"
    assert res.level == "explicit"
    assert res.path == w.replace("\\", "/")
    assert res.offset == instances.BAND_SIZE


def test_resolve_env_beats_default_with_warning(life_home, tmp_path, monkeypatch):
    p, w = _two_instances(tmp_path)
    _write_home(tmp_path, {
        "personal": {"path": p, "profile": "personal", "band": 0},
        "work": {"path": w, "profile": "work", "band": 1},
    }, default="personal")
    monkeypatch.setenv("LIFE_INSTANCE", "work")
    res = instances.resolve()
    assert res.id == "work"
    assert res.level == "env"
    assert any("ombreggia" in msg for msg in res.warnings)


def test_resolve_unknown_id_names_valid_ids(life_home, tmp_path):
    p, w = _two_instances(tmp_path)
    _write_home(tmp_path, {
        "personal": {"path": p, "profile": "personal", "band": 0},
        "work": {"path": w, "profile": "work", "band": 1},
    }, default="personal")
    with pytest.raises(instances.UnknownInstance) as exc:
        instances.resolve("ghost")
    assert "personal" in str(exc.value) and "work" in str(exc.value)


def test_registry_without_default_is_an_error(life_home, tmp_path):
    p, w = _two_instances(tmp_path)
    _write_home(tmp_path, {
        "personal": {"path": p, "profile": "personal", "band": 0},
        "work": {"path": w, "profile": "work", "band": 1},
    })
    with pytest.raises(instances.InstanceError):
        instances.resolve()


def test_empty_registry_with_legacy_dir(life_home, tmp_path, monkeypatch):
    monkeypatch.setenv("LIFE_INSTANCE_DIR", str(tmp_path / "life-personal"))
    res = instances.resolve()
    assert res.level == "legacy"
    assert res.id is None
    assert res.path == str(tmp_path / "life-personal").replace("\\", "/")


def test_describe_all_names_unreachable_instance(life_home, tmp_path):
    p, _ = _two_instances(tmp_path)
    missing = tmp_path / "life-comunity"  # registrata ma assente
    _write_home(tmp_path, {
        "personal": {"path": p, "profile": "personal", "band": 0},
        "comunity": {"path": str(missing), "profile": "comunity", "band": 2},
    }, default="personal", make_dirs=False)
    # personal path non esiste (make_dirs=False): crealo a mano.
    (tmp_path / "life-personal").mkdir(parents=True, exist_ok=True)
    view = instances.describe_all()
    by_id = {row["id"]: row for row in view["instances"]}
    assert by_id["personal"]["status"] == "ok"
    assert by_id["comunity"]["status"] == "unreachable"
    assert "life-comunity" in by_id["comunity"]["reason"]


def test_channels_read_from_instance_config(life_home, tmp_path):
    p, w = _two_instances(tmp_path)
    _write_home(tmp_path, {
        "personal": {"path": p, "profile": "personal", "band": 0},
        "work": {"path": w, "profile": "work", "band": 1},
    }, default="personal")
    (tmp_path / "life-work" / "instance.config.json").write_text(json.dumps({
        "profile": "work",
        "channels": {
            "busTenant": "acme",
            "telegramChatId": -100,
            "telegramAgent": "telegram-work",
            "clickup": {"workspace": "VUS"},
            "drive": {"folder": "VUS/2026"},
        },
    }), encoding="utf-8")
    inst = instances.get("work")
    ch = instances.channels_of(inst)
    assert ch.source == "instance"
    assert ch.bus_tenant == "acme"
    assert ch.telegram_chat_id == -100
    assert ch.clickup_workspace == "VUS"


def test_channels_fall_back_to_global_only_for_default(life_home, tmp_path, monkeypatch):
    p, w = _two_instances(tmp_path)
    _write_home(tmp_path, {
        "personal": {"path": p, "profile": "personal", "band": 0},
        "work": {"path": w, "profile": "work", "band": 1},
    }, default="personal")
    monkeypatch.setenv("HUB_TENANTS", "personale")
    monkeypatch.setenv("HUB_TELEGRAM_CHAT_ID", "-1003709297218")
    default_ch = instances.channels_of(instances.get("personal"))
    assert default_ch.source == "global"
    assert default_ch.bus_tenant == "personale"
    # A registered, non-default instance with no channels gets NONE, not the global.
    other = instances.channels_of(instances.get("work"))
    assert other.source == "none"
    assert other.bus_tenant == ""


# --- canali: schema annidato di Life, lettore unico (conv 672) --------------------

def test_channels_read_the_nested_life_schema(life_home, tmp_path, monkeypatch):
    p, w = _two_instances(tmp_path)
    _write_home(tmp_path, {
        "personal": {"path": p, "profile": "personal", "band": 0},
        "work": {"path": w, "profile": "work", "band": 1},
    }, default="personal")
    (tmp_path / "life-work" / "instance.config.json").write_text(json.dumps({
        "profile": "work",
        "channels": {
            "clickup": {"workspace": "90152063849"},
            "drive": {},
            "telegram": {"chatId": -1003899308208, "agent": "owner-telegram"},
            "bus": {"tenant": "life-acme"},
        },
    }), encoding="utf-8")
    monkeypatch.setenv("NETWORK_UI_URL", "http://ui:3336/")
    ch = instances.channels_of(instances.get("work"))
    assert ch.source == "instance"
    assert ch.bus_tenant == "life-acme"
    assert ch.telegram_chat_id == -1003899308208
    assert ch.telegram_agent == "owner-telegram"
    assert ch.clickup_workspace == "90152063849"
    assert ch.network_ui_url == "http://ui:3336"


def test_life_channels_copy_matches_life():
    """The copied Life channel reader must not drift in executable behaviour.

    The public export anonymises the illustrative identities in the module
    docstring, so compare the module body rather than those examples.
    """
    import ast
    from pathlib import Path
    import pytest

    def implementation(path: Path) -> str:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        if ast.get_docstring(tree, clean=False) is not None:
            tree.body.pop(0)
        return ast.dump(tree, include_attributes=False)

    repo = Path(__file__).resolve().parents[1]
    life = repo.parent / "Life" / "packages" / "shared" / "suite_core" / "instances" / "channels.py"
    if not life.is_file():
        pytest.skip("Life checkout not next to the repo")
    assert implementation(repo / "life_channels.py") == implementation(life), (
        "life_channels.py implementation differs from Life; sync it from "
        "../Life/packages/shared/suite_core/instances/channels.py"
    )
