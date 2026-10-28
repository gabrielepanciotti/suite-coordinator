"""Conv #658: a local agent qualified with a tenant that is not in HUB_TENANTS
must be reported (ERROR + Telegram), never dropped into a silent 0 listeners."""
import asyncio
import logging

import hub
import hub_config


def _cfg(monkeypatch, tenants, agents):
    monkeypatch.setenv("HUB_URL", "http://h:10002")
    monkeypatch.setenv("HUB_TENANTS", tenants)
    for t in tenants.split(","):
        monkeypatch.setenv(hub_config.api_key_env(t), "k")
    monkeypatch.setenv("HUB_LOCAL_AGENTS", agents)
    return hub_config.load_config()


def test_stale_prefix_is_orphan(monkeypatch):
    cfg = _cfg(
        monkeypatch,
        "life-work",
        "acme/hubOfferte-dev-linux:offerte,life-work/cruscotto-dev-linux:cruscotto,"
        "bare-dev-linux:bare",
    )
    assert cfg.orphan_tenant_agents() == [("acme", "hubOfferte-dev-linux")]
    assert sorted(cfg.agents_for("life-work")) == ["bare-dev-linux", "cruscotto-dev-linux"]


def test_no_orphans_when_prefixes_match(monkeypatch):
    cfg = _cfg(monkeypatch, "life-work,life-personal", "life-work/a:a,life-personal/b:b")
    assert cfg.orphan_tenant_agents() == []


def test_report_logs_error_and_alerts_once(monkeypatch, caplog):
    cfg = _cfg(monkeypatch, "life-work", "acme/a-dev-linux:a,acme/b-dev-linux:b")
    sent = []

    async def send_to(uid, text):
        sent.append((uid, text))

    import profiles

    monkeypatch.setattr(profiles, "admin_user_ids", lambda: [42])
    monkeypatch.setitem(hub._S, "send_to", send_to)
    monkeypatch.setitem(hub._S, "orphan_tenants_announced", False)

    async def run():
        with caplog.at_level(logging.ERROR, logger=hub.logger.name):
            hub._report_orphan_tenant_agents(cfg)
            hub._report_orphan_tenant_agents(cfg)  # second boot path: log, no 2nd alarm
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    asyncio.run(run())
    errors = [r for r in caplog.records if "IGNORED" in r.getMessage()]
    assert len(errors) == 2
    assert "acme/a-dev-linux" in errors[0].getMessage()
    assert len(sent) == 1 and sent[0][0] == 42
    assert "acme" in sent[0][1] and "life-work" in sent[0][1]


def test_report_silent_without_orphans(monkeypatch, caplog):
    cfg = _cfg(monkeypatch, "life-work", "life-work/a:a")
    with caplog.at_level(logging.ERROR, logger=hub.logger.name):
        hub._report_orphan_tenant_agents(cfg)
    assert not [r for r in caplog.records if "IGNORED" in r.getMessage()]
