# tests/test_deployer_config.py
import deployer_config as dc


def test_defaults_whitelist_and_disabled_without_env(monkeypatch):
    for k in list(__import__("os").environ):
        if k.startswith(("HUB_", "DEPLOYER_")):
            monkeypatch.delenv(k, raising=False)
    cfg = dc.load_deployer_config()
    assert cfg.enabled is False  # no hub_url / api_key / agent_id / senders
    # default operational whitelist
    assert "HUB_LOCAL_AGENTS" in cfg.env_keys
    assert "CLAUDE_MAX_CONCURRENCY" in cfg.env_keys
    assert cfg.worker_service == "coordinator-worker"
    assert cfg.branch == "master"


def test_loads_from_env_and_enabled(monkeypatch):
    monkeypatch.setenv("HUB_URL", "http://h:8031/")
    monkeypatch.setenv("DEPLOYER_TENANT", "acme")
    monkeypatch.setenv("HUB_API_KEY_ACME", "k-secret")
    monkeypatch.setenv("DEPLOYER_AGENT_ID", "deployer-core")
    monkeypatch.setenv("DEPLOYER_ALLOWED_SENDERS", "network-dev-linux, owner-telegram")
    cfg = dc.load_deployer_config()
    assert cfg.enabled is True
    assert cfg.hub_url == "http://h:8031"  # trailing slash stripped
    assert cfg.api_key == "k-secret"
    assert cfg.agent_id == "deployer-core"
    assert "network-dev-linux" in cfg.allowed_senders
    assert "owner-telegram" in cfg.allowed_senders


def test_env_key_allowed_rejects_secrets_even_if_whitelisted(monkeypatch):
    monkeypatch.setenv("DEPLOYER_ENV_KEYS", "HUB_LOCAL_AGENTS,HUB_API_KEY_ACME,MY_TOKEN")
    cfg = dc.load_deployer_config()
    assert cfg.env_key_allowed("HUB_LOCAL_AGENTS") is True
    assert cfg.env_key_allowed("HUB_API_KEY_ACME") is False  # KEY pattern
    assert cfg.env_key_allowed("MY_TOKEN") is False            # TOKEN pattern
    assert cfg.env_key_allowed("NOT_LISTED") is False          # not in whitelist
