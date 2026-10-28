import hub_config


def test_parses_tenants_and_keys(monkeypatch):
    monkeypatch.setenv("HUB_URL", "http://host.docker.internal:8030")
    monkeypatch.setenv("HUB_TENANTS", "personale")
    monkeypatch.setenv("HUB_API_KEY_PERSONALE", "k-pers")
    monkeypatch.setenv("HUB_TELEGRAM_AGENT", "owner-telegram")
    monkeypatch.setenv("HUB_TELEGRAM_CHAT_ID", "-1003709297218")
    monkeypatch.setenv(
        "HUB_LOCAL_AGENTS", "personalChef-local:personalChef,mycorise-local:mycorise"
    )
    cfg = hub_config.load_config()
    assert cfg.hub_url == "http://host.docker.internal:8030"
    assert cfg.api_keys == {"personale": "k-pers"}
    assert cfg.telegram_agent == "owner-telegram"
    assert cfg.telegram_chat_id == -1003709297218
    assert cfg.local_agents == {
        "personalChef-local": "personalChef",
        "mycorise-local": "mycorise",
    }
    assert cfg.turn_cap == 12  # default


def test_disabled_when_no_tenants(monkeypatch):
    monkeypatch.delenv("HUB_TENANTS", raising=False)
    cfg = hub_config.load_config()
    assert cfg.enabled is False


def test_enabled_requires_url_key_and_chat(monkeypatch):
    monkeypatch.setenv("HUB_TENANTS", "personale")
    monkeypatch.setenv("HUB_API_KEY_PERSONALE", "k")
    monkeypatch.setenv("HUB_URL", "http://h:8030")
    monkeypatch.setenv("HUB_TELEGRAM_CHAT_ID", "-100")
    assert hub_config.load_config().enabled is True


def test_role_defaults_to_hub(monkeypatch):
    monkeypatch.delenv("COORDINATOR_ROLE", raising=False)
    assert hub_config.load_config().role == "hub"


def test_worker_role_parsed(monkeypatch):
    monkeypatch.setenv("COORDINATOR_ROLE", "worker")
    assert hub_config.load_config().role == "worker"


def test_worker_enabled_without_chat_id(monkeypatch):
    # A worker needs url + api_keys + local_agents, NOT telegram_chat_id.
    monkeypatch.setenv("COORDINATOR_ROLE", "worker")
    monkeypatch.setenv("HUB_URL", "http://h:8031")
    monkeypatch.setenv("HUB_TENANTS", "acme")
    monkeypatch.setenv("HUB_API_KEY_ACME", "k")
    monkeypatch.setenv("HUB_LOCAL_AGENTS", "offerte-prod:offerte")
    monkeypatch.delenv("HUB_TELEGRAM_CHAT_ID", raising=False)
    assert hub_config.load_config().enabled is True


def test_worker_disabled_without_local_agents(monkeypatch):
    monkeypatch.setenv("COORDINATOR_ROLE", "worker")
    monkeypatch.setenv("HUB_URL", "http://h:8031")
    monkeypatch.setenv("HUB_TENANTS", "acme")
    monkeypatch.setenv("HUB_API_KEY_ACME", "k")
    monkeypatch.delenv("HUB_LOCAL_AGENTS", raising=False)
    assert hub_config.load_config().enabled is False


def test_tenant_qualified_local_agents(monkeypatch):
    monkeypatch.setenv("HUB_TENANTS", "acme,personale")
    monkeypatch.setenv("HUB_API_KEY_ACME", "k1")
    monkeypatch.setenv("HUB_API_KEY_PERSONALE", "k2")
    monkeypatch.setenv(
        "HUB_LOCAL_AGENTS",
        "acme/offerte-dev-linux:offerte,personale/personalChef-local:personalChef",
    )
    cfg = hub_config.load_config()
    assert cfg.local_agents == {
        "offerte-dev-linux": "offerte",
        "personalChef-local": "personalChef",
    }
    assert cfg.agents_for("acme") == ["offerte-dev-linux"]
    assert cfg.agents_for("personale") == ["personalChef-local"]


def test_unqualified_agents_listen_on_every_tenant(monkeypatch):
    # Legacy flat entries keep today's behavior: one listener per tenant.
    monkeypatch.setenv("HUB_TENANTS", "acme,personale")
    monkeypatch.setenv("HUB_API_KEY_ACME", "k1")
    monkeypatch.setenv("HUB_API_KEY_PERSONALE", "k2")
    monkeypatch.setenv("HUB_LOCAL_AGENTS", "db-dev:db")
    cfg = hub_config.load_config()
    assert cfg.agents_for("acme") == ["db-dev"]
    assert cfg.agents_for("personale") == ["db-dev"]


def test_prod_dispatch_map(monkeypatch):
    monkeypatch.setenv(
        "HUB_PROD_DISPATCH", "offerte:offerte-prod,segnalatori:portale-prod"
    )
    cfg = hub_config.load_config()
    assert cfg.prod_dispatch == {
        "offerte": "offerte-prod",
        "segnalatori": "portale-prod",
    }


def test_recovery_settings_defaults_and_override(monkeypatch):
    import importlib
    import hub_config as hc
    importlib.reload(hc)

    # defaults
    for k in ("HUB_RECOVERY_WINDOW_H", "HUB_RECOVERY_MAX"):
        monkeypatch.delenv(k, raising=False)
    cfg = hc.load_config()
    assert cfg.recovery_window_h == 24
    assert cfg.recovery_max == 20

    # overrides
    monkeypatch.setenv("HUB_RECOVERY_WINDOW_H", "6")
    monkeypatch.setenv("HUB_RECOVERY_MAX", "5")
    cfg = hc.load_config()
    assert cfg.recovery_window_h == 6
    assert cfg.recovery_max == 5


# --- tenant rinominato: le conversazioni salvate col nome vecchio (conv 672) ---

def test_old_tenant_name_resolves_to_the_configured_one(monkeypatch):
    monkeypatch.setenv("HUB_TENANTS", "life-acme,life-personal")
    monkeypatch.setenv("HUB_API_KEY_LIFE_ACME", "k1")
    monkeypatch.setenv("HUB_API_KEY_LIFE_PERSONAL", "k2")
    monkeypatch.delenv("HUB_TENANT_ALIASES", raising=False)
    cfg = hub_config.load_config()
    assert cfg.canonical_tenant("life-work") == "life-acme"
    assert cfg.canonical_tenant("life-acme") == "life-acme"
    assert cfg.canonical_tenant("life-personal") == "life-personal"
    assert cfg.canonical_tenant("ignoto") == "ignoto"


def test_alias_never_hides_a_tenant_still_configured(monkeypatch):
    # A device not yet migrated keeps listening on life-work: the alias is unused.
    monkeypatch.setenv("HUB_TENANTS", "life-work")
    monkeypatch.setenv("HUB_API_KEY_LIFE_WORK", "k1")
    monkeypatch.delenv("HUB_TENANT_ALIASES", raising=False)
    cfg = hub_config.load_config()
    assert cfg.canonical_tenant("life-work") == "life-work"
