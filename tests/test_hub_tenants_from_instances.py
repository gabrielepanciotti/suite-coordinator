"""#82: the bus tenants of registered instances join HUB_TENANTS on their own."""

import json

import hub_config


def _instance(home, root, iid, channels, *, band=1):
    path = root / f"life-{iid}"
    path.mkdir(parents=True)
    (path / "instance.config.json").write_text(
        json.dumps({"profile": iid, "channels": channels}), encoding="utf-8"
    )
    reg_file = home / "instances.json"
    reg = json.loads(reg_file.read_text()) if reg_file.exists() else {"version": 1, "instances": {}}
    reg["instances"][iid] = {"path": str(path), "profile": iid, "band": band}
    reg_file.write_text(json.dumps(reg), encoding="utf-8")
    return path


def _home(tmp_path, monkeypatch):
    home = tmp_path / "life-home"
    home.mkdir()
    monkeypatch.setenv("LIFE_HOME", str(home))
    return home


def _base_env(monkeypatch):
    monkeypatch.setenv("HUB_URL", "http://h:10002")
    monkeypatch.setenv("HUB_TELEGRAM_CHAT_ID", "-100")
    monkeypatch.setenv("HUB_LOCAL_AGENTS", "")
    monkeypatch.delenv("HUB_AGENT_KEYS", raising=False)


def test_instance_tenant_heard_with_key_from_secret_file(tmp_path, monkeypatch):
    _base_env(monkeypatch)
    home = _home(tmp_path, monkeypatch)
    _instance(home, tmp_path, "energypass", {"bus": {"tenant": "life-energypass"}}, band=5)
    (home / "secrets").mkdir()
    (home / "secrets" / "life-energypass.key").write_text("k-ep\n", encoding="utf-8")
    monkeypatch.setenv("HUB_TENANTS", "life-acme")
    monkeypatch.setenv("HUB_API_KEY_LIFE_ACME", "k-vus")
    monkeypatch.delenv("HUB_API_KEY_LIFE_ENERGYPASS", raising=False)

    cfg = hub_config.load_config()

    # .env tenants first (the first is the default tenant), then the instance's.
    assert list(cfg.api_keys) == ["life-acme", "life-energypass"]
    assert cfg.api_keys["life-energypass"] == "k-ep"


def test_env_key_overrides_secret_file(tmp_path, monkeypatch):
    _base_env(monkeypatch)
    home = _home(tmp_path, monkeypatch)
    _instance(home, tmp_path, "energypass", {"bus": {"tenant": "life-energypass"}})
    (home / "secrets").mkdir()
    (home / "secrets" / "life-energypass.key").write_text("dal-file", encoding="utf-8")
    monkeypatch.delenv("HUB_TENANTS", raising=False)
    monkeypatch.setenv("HUB_API_KEY_LIFE_ENERGYPASS", "dal-env")

    assert hub_config.load_config().api_keys == {"life-energypass": "dal-env"}


def test_instance_tenant_already_in_env_is_not_duplicated(tmp_path, monkeypatch):
    _base_env(monkeypatch)
    home = _home(tmp_path, monkeypatch)
    _instance(home, tmp_path, "acme", {"bus": {"tenant": "life-acme"}})
    monkeypatch.setenv("HUB_TENANTS", "life-acme")
    monkeypatch.setenv("HUB_API_KEY_LIFE_ACME", "k")

    assert hub_config.load_tenants() == ["life-acme"]


def test_instance_tenant_without_any_key_is_not_heard(tmp_path, monkeypatch, caplog):
    _base_env(monkeypatch)
    home = _home(tmp_path, monkeypatch)
    _instance(home, tmp_path, "energypass", {"bus": {"tenant": "life-energypass"}})
    monkeypatch.delenv("HUB_TENANTS", raising=False)
    monkeypatch.delenv("HUB_API_KEY_LIFE_ENERGYPASS", raising=False)
    # Even with per-agent keys, a keyless instance tenant would only collect 401s.
    monkeypatch.setenv("HUB_AGENT_KEYS", "a:b")

    assert hub_config.load_config().api_keys == {}
    assert "life-energypass" in caplog.text


def test_instance_without_bus_channel_adds_nothing(tmp_path, monkeypatch):
    home = _home(tmp_path, monkeypatch)
    _instance(home, tmp_path, "nuova", {"telegram": {}})
    monkeypatch.setenv("HUB_TENANTS", "life-acme")

    assert hub_config.load_tenants() == ["life-acme"]


def test_broken_registry_keeps_env_tenants(tmp_path, monkeypatch):
    home = _home(tmp_path, monkeypatch)
    (home / "instances.json").write_text("{non json", encoding="utf-8")
    monkeypatch.setenv("HUB_TENANTS", "life-acme")

    assert hub_config.load_tenants() == ["life-acme"]
