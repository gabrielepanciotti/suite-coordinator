# tests/test_hub_agent_keys.py
"""Per-agent bus keys (X-Agent-Key) + the logged fallback to the tenant key.

The Coordinator speaks for several bus identities at once, so a single shared
tenant key cannot be replaced by a single agent key: the key must be selected
per acting agent. These tests pin that selection and, just as important, that a
missing per-agent key degrades LOUDLY instead of silently.
"""
import asyncio
import logging

import hub
import hub_client
import hub_config
import overview_snapshot
import status_report


# --------------------------------------------------------------------------
# auth_headers — strong path
# --------------------------------------------------------------------------
def test_agent_key_replaces_tenant_key():
    h = hub_client.auth_headers("tenant-key", "cosmos-minipc", "agent-key")
    assert h["X-Agent-Key"] == "agent-key"
    assert h["X-Agent-Id"] == "cosmos-minipc"
    # The whole point: the shared tenant key is not sent at all.
    assert "X-Api-Key" not in h


def test_hub_client_uses_agent_key():
    c = hub_client.HubClient("http://h:10002", "tenant-key", "travel-minipc", "ak")
    assert c.headers == {"X-Agent-Key": "ak", "X-Agent-Id": "travel-minipc"}


# --------------------------------------------------------------------------
# auth_headers — legacy fallback, must be loud
# --------------------------------------------------------------------------
def test_missing_agent_key_falls_back_to_tenant_key_and_warns(caplog):
    hub_client._DEGRADED_LOGGED.discard("legacy-agent")
    with caplog.at_level(logging.WARNING, logger="coordinator.hub_client"):
        h = hub_client.auth_headers("tenant-key", "legacy-agent")
    assert h == {"X-Api-Key": "tenant-key", "X-Agent-Id": "legacy-agent"}
    assert any(
        "DEGRADED" in r.getMessage() and "legacy-agent" in (r.getMessage())
        for r in caplog.records
    )


def test_degradation_warning_is_logged_once_per_agent(caplog):
    hub_client._DEGRADED_LOGGED.discard("noisy-agent")
    with caplog.at_level(logging.WARNING, logger="coordinator.hub_client"):
        for _ in range(5):
            hub_client.auth_headers("tenant-key", "noisy-agent")
    assert len([r for r in caplog.records if "noisy-agent" in (r.getMessage())]) == 1


def test_empty_agent_key_is_not_treated_as_configured(caplog):
    hub_client._DEGRADED_LOGGED.discard("blank-agent")
    with caplog.at_level(logging.WARNING, logger="coordinator.hub_client"):
        h = hub_client.auth_headers("tenant-key", "blank-agent", "")
    assert "X-Api-Key" in h
    assert caplog.records  # degradation is never silent


# --------------------------------------------------------------------------
# config — one map, exact agent names
# --------------------------------------------------------------------------
def _base_env(monkeypatch):
    monkeypatch.setenv("HUB_URL", "http://h:10002")
    monkeypatch.setenv("HUB_TENANTS", "personale")
    monkeypatch.setenv("HUB_TELEGRAM_CHAT_ID", "-100")
    monkeypatch.delenv("HUB_AGENT_KEYS", raising=False)
    monkeypatch.delenv("HUB_API_KEY_PERSONALE", raising=False)


def test_parses_agent_keys_map(monkeypatch):
    _base_env(monkeypatch)
    monkeypatch.setenv("HUB_API_KEY_PERSONALE", "k-pers")
    monkeypatch.setenv(
        "HUB_AGENT_KEYS", "cosmos-minipc:ak1, telegram-owner:ak2 ,travel-minipc:ak3"
    )
    cfg = hub_config.load_config()
    assert cfg.agent_key("cosmos-minipc") == "ak1"
    assert cfg.agent_key("telegram-owner") == "ak2"
    assert cfg.agent_key("travel-minipc") == "ak3"
    assert cfg.agent_key("not-configured") == ""


def test_agent_names_keep_their_exact_case(monkeypatch):
    """`homeResearcher-minipc` and `homeresearcher-minipc` are two different,
    coexisting agents on this bus — the reason the keys live in a map and not in
    one env var per agent."""
    _base_env(monkeypatch)
    monkeypatch.setenv(
        "HUB_AGENT_KEYS", "homeResearcher-minipc:camel,homeresearcher-minipc:lower"
    )
    cfg = hub_config.load_config()
    assert cfg.agent_key("homeResearcher-minipc") == "camel"
    assert cfg.agent_key("homeresearcher-minipc") == "lower"


def test_stays_enabled_after_the_tenant_key_is_revoked(monkeypatch):
    """Revoking the tenant key means emptying HUB_API_KEY_<TENANT>. The hub must
    stay ON off the per-agent keys, not quietly switch itself off."""
    _base_env(monkeypatch)
    monkeypatch.setenv("HUB_AGENT_KEYS", "telegram-owner:ak")
    cfg = hub_config.load_config()
    assert cfg.api_keys == {"personale": ""}
    assert cfg.enabled is True


def test_disabled_without_any_credential(monkeypatch):
    _base_env(monkeypatch)
    assert hub_config.load_config().enabled is False


# --------------------------------------------------------------------------
# wiring — the right key for the right acting agent
# --------------------------------------------------------------------------
def _cfg(**kw):
    base = dict(
        hub_url="http://h:10002",
        api_keys={"personale": "tenant-key"},
        telegram_agent="telegram-owner",
        telegram_chat_id=-100,
        local_agents={"cosmos-minipc": "cosmos", "travel-minipc": "travel"},
        agent_keys={"cosmos-minipc": "ak-cosmos", "telegram-owner": "ak-owner"},
    )
    base.update(kw)
    return hub_config.HubConfig(**base)


def test_client_cache_picks_the_acting_agents_own_key():
    hub._S["config"] = _cfg()
    hub._S["clients"] = {}
    assert hub._client("personale", "cosmos-minipc").headers == {
        "X-Agent-Key": "ak-cosmos",
        "X-Agent-Id": "cosmos-minipc",
    }
    assert hub._client("personale", "telegram-owner").headers == {
        "X-Agent-Key": "ak-owner",
        "X-Agent-Id": "telegram-owner",
    }
    # travel-minipc has no key yet -> legacy path, and it must not borrow
    # another agent's key.
    assert hub._client("personale", "travel-minipc").headers == {
        "X-Api-Key": "tenant-key",
        "X-Agent-Id": "travel-minipc",
    }


# --------------------------------------------------------------------------
# the one endpoint that must NOT move to a per-agent key
# --------------------------------------------------------------------------
class _FakePost:
    def __init__(self, captured):
        self.captured = captured

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, headers=None, json=None):
        self.captured.update(url=url, headers=headers)

        class _R:
            status_code = 200

            def raise_for_status(self):
                pass

            def json(self):
                return {"projects": []}

        return _R()


def test_overview_snapshot_stays_on_the_tenant_key(monkeypatch, tmp_path):
    """Network's schedule_snapshot depends on get_tenant_context: it resolves the
    TENANT from X-Api-Key, so an agent key there is not a credential and the call
    422s. Regression guard — this was shipped once and failed every 15 minutes."""
    monkeypatch.setenv("OVERVIEW_SNAPSHOT_ENABLED", "1")
    monkeypatch.setenv("OVERVIEW_PAUSES_PATH", str(tmp_path / "p.json"))
    monkeypatch.setattr(overview_snapshot, "_snapshot_enabled", lambda: True)
    monkeypatch.setattr(overview_snapshot.notif_store, "load", lambda: [])
    captured: dict = {}
    monkeypatch.setattr(
        overview_snapshot.httpx, "AsyncClient", lambda *a, **k: _FakePost(captured)
    )
    assert asyncio.run(
        overview_snapshot.push_snapshot("http://h:10002", "tenant-key", "telegram-owner")
    ) is True
    assert captured["headers"]["X-Api-Key"] == "tenant-key"
    assert "X-Agent-Key" not in captured["headers"]


def test_overview_snapshot_prefers_the_agents_own_key(monkeypatch, tmp_path):
    """The snapshot is no longer the one endpoint stuck on the shared key.

    It used to depend on the bus's get_tenant_context, which read a TENANT key
    and nothing else, so an agent key 422'd every 15 minutes and this call had
    to keep using the shared credential. The bus moved it to get_tenant_scope
    (Network 1c54bfa), which resolves the tenant from whichever credential it
    receives — so the exception is gone and this call authenticates as itself.
    """
    monkeypatch.setenv("OVERVIEW_PAUSES_PATH", str(tmp_path / "p.json"))
    monkeypatch.setattr(overview_snapshot, "_snapshot_enabled", lambda: True)
    monkeypatch.setattr(overview_snapshot.notif_store, "load", lambda: [])
    captured: dict = {}
    monkeypatch.setattr(
        overview_snapshot.httpx, "AsyncClient", lambda *a, **k: _FakePost(captured)
    )

    ok = asyncio.run(
        overview_snapshot.push_snapshot(
            "http://h:10002", "tenant-key", "telegram-owner", None, "own-agent-key"
        )
    )

    assert ok is True
    assert captured["headers"]["X-Agent-Key"] == "own-agent-key"
    assert captured["headers"]["X-Agent-Id"] == "telegram-owner"
    # The shared key must not ride along: revoking it must not break this call.
    assert "X-Api-Key" not in captured["headers"]


def test_overview_snapshot_says_so_when_it_has_no_credential_at_all(
    monkeypatch, tmp_path, caplog
):
    """No key of either kind: refuse loudly rather than push unauthenticated.

    A snapshot that silently stops arriving looks exactly like a Coordinator
    with nothing to report, which is why this path has to name itself.
    """
    monkeypatch.setenv("OVERVIEW_PAUSES_PATH", str(tmp_path / "p.json"))
    monkeypatch.setattr(overview_snapshot, "_snapshot_enabled", lambda: True)
    monkeypatch.setattr(overview_snapshot.notif_store, "load", lambda: [])
    with caplog.at_level(logging.WARNING):
        ok = asyncio.run(
            overview_snapshot.push_snapshot("http://h:10002", "", "telegram-owner")
        )
    assert ok is False
    assert any(
        "no credential configured" in r.getMessage() for r in caplog.records
    )


def test_status_report_uses_the_agent_key(monkeypatch):
    """POST /api/status-reports goes through get_auth_context, so it DOES accept
    a per-agent key — the asymmetry with the snapshot above is the bus's, not
    ours."""
    monkeypatch.setattr(status_report, "_enabled", lambda: True)
    monkeypatch.setattr(
        status_report,
        "build_status_report",
        lambda keys: {"device": "minipc", "projects": [{"key": "x"}]},
    )
    captured: dict = {}
    monkeypatch.setattr(
        status_report.httpx, "AsyncClient", lambda *a, **k: _FakePost(captured)
    )
    assert asyncio.run(
        status_report.push_status_report(
            "http://h:10002", "tenant-key", "cosmos-minipc", ["cosmos"],
            agent_key="ak-cosmos",
        )
    ) is True
    assert captured["headers"] == {
        "X-Agent-Key": "ak-cosmos",
        "X-Agent-Id": "cosmos-minipc",
    }


def test_owner_stream_opens_once_per_identity_not_once_per_tenant():
    cfg = _cfg(api_keys={"acme": "tk-v", "personale": "tk-p"})
    # Both tenants would authenticate with the same agent key: one identity.
    assert hub.owner_listen_tenants(cfg) == ["acme"]


def test_owner_stream_still_opens_on_every_tenant_without_an_agent_key():
    cfg = _cfg(api_keys={"acme": "tk-v", "personale": "tk-p"}, agent_keys={})
    assert hub.owner_listen_tenants(cfg) == ["acme", "personale"]


def test_una_identita_in_due_tenant_ha_una_chiave_per_tenant(monkeypatch):
    """#83: owner-telegram vive in life-acme e in life-personal, con chiavi diverse."""
    import hub
    import hub_config

    cfg = hub_config.HubConfig(
        hub_url="http://bus", api_keys={"life-acme": "t1", "life-personal": "t2"},
        telegram_agent="owner-telegram", telegram_chat_id=-100, local_agents={},
        agent_keys={"owner-telegram": "kv", "life-personal/owner-telegram": "kp"},
    )
    assert cfg.agent_key("owner-telegram", "life-acme") == "kv"
    assert cfg.agent_key("owner-telegram", "life-personal") == "kp"
    assert cfg.agent_key("owner-telegram") == "kv"
    assert hub.owner_listen_tenants(cfg) == ["life-acme", "life-personal"]
