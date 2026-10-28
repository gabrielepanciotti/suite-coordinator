import pytest
"""Bus names come from the node slug, not from the historic project key (#79).

Network#42 renamed every identity to ``<slug>-<suffix>`` and kept the old name
only as an alias to retire. These tests pin each road the Coordinator uses to
name a recipient, so none of them keeps addressing an alias.
"""

import asyncio
import json
from pathlib import Path

import hub
from hub_config import HubConfig
from project_model import Project


def _cfg(**kw):
    base = dict(hub_url="u", api_keys={"t": "k"}, telegram_agent="telegram-owner",
                telegram_chat_id=-100, local_agents={}, dev_device="dev-linux",
                prod_device="prod")
    base.update(kw)
    return HubConfig(**base)


def _project(key, slug="", aliases=()):
    return Project(key=key, label=key, emoji="", host_path="/x", workspace="/w",
                   aliases=tuple(aliases), slug=slug)


def test_static_key_derives_the_slug_not_the_alias(monkeypatch):
    # Static registry (prod workers, LIFE_BACKEND_URL=off): key `offerte`, slug
    # `hubOfferte`. `offerte-dev-linux` is only an alias on the bus now.
    monkeypatch.setitem(hub._S, "config", _cfg())
    p = _project("offerte", slug="hubOfferte", aliases=("hub",))
    assert hub.dev_agent_of(p) == "hubOfferte-dev-linux"
    assert hub.prod_agents_of(p) == ["hubOfferte-prod"]


def test_live_key_already_is_the_slug(monkeypatch):
    monkeypatch.setitem(hub._S, "config", _cfg())
    p = _project("etlV2", aliases=("etl",))
    assert p.bus_slug == "etlV2"
    assert hub.dev_agent_of(p) == "etlV2-dev-linux"


def test_override_keyed_by_old_name_still_applies(monkeypatch):
    # HUB_*_DISPATCH say `segnalatori:`; with the live registry the key is
    # `portaleSegnalazioni` and an exact lookup silently missed the override.
    monkeypatch.setitem(hub._S, "config", _cfg(
        prod_dispatch={"segnalatori": "portaleSegnalazioni-prod"},
        dev_dispatch={"segnalatori": "portaleSegnalazioni-dev-linux"},
    ))
    p = _project("portaleSegnalazioni", aliases=("segnalatori", "portale"))
    assert hub.prod_agents_of(p) == ["portaleSegnalazioni-prod"]
    assert hub.dev_agent_of(p) == "portaleSegnalazioni-dev-linux"


def test_identity_heard_on_this_device_beats_derivation(monkeypatch):
    # Exceptions to the taxonomy (`acme-auth-dev-linux` for `acmeAuth`)
    # exist only in the roster: derivation would name an agent nobody holds.
    monkeypatch.setitem(hub._S, "config", _cfg(local_agents={
        "acme-auth-dev-linux": "acmeAuth",
        "hubOfferte-dev-linux": "offerte",
        "hubOfferte-prod": "offerte",
    }))
    assert hub.dev_agent_of(_project("acmeAuth")) == "acme-auth-dev-linux"
    # The roster entry is matched by any name of the project, and only with
    # this device's suffix (a prod identity in the same roster is not "dev").
    assert hub.dev_agent_of(_project("hubOfferte", aliases=("offerte",))) == (
        "hubOfferte-dev-linux"
    )


def test_dm_reply_node_resolves_to_the_device_identity(monkeypatch):
    monkeypatch.setitem(hub._S, "config", _cfg())
    import projects

    monkeypatch.setattr(projects, "resolve_project", lambda t: (
        _project("hubOfferte", aliases=("offerte",)) if t == "offerte" else None
    ))
    assert hub.bus_agent_for_node("offerte") == "hubOfferte-dev-linux"
    # Not a project: already an agent name, passed through.
    assert hub.bus_agent_for_node("ticketManager-dev-linux") == "ticketManager-dev-linux"


def test_prod_prefix_finds_override_by_alias(monkeypatch):
    sent = {}

    class FakeClient:
        async def create_conversation(self, to, subject, body, priority="normal",
                                      context=None):
            sent.update(to=to)
            return {"id": 1}

    monkeypatch.setitem(hub._S, "config", _cfg(
        prod_dispatch={"network": "network-prod-private|network-prod-public"}))
    monkeypatch.setattr(hub, "_client", lambda tenant, agent: FakeClient())
    handled, _ = asyncio.run(hub.maybe_dispatch_prefix(
        _project("network", aliases=("bus",)), "prod: riavvia"))
    assert handled is True
    assert sent["to"] == ["network-prod-private"]


@pytest.mark.skipif(not (Path(__file__).resolve().parents[1] / "locale" / "projects.json").exists(),
                    reason="needs the private locale/projects.json (not published)")
def test_static_list_carries_slugs_for_renamed_nodes():
    # The prod workers only ever see this list: every node whose key is not
    # its graph slug must say so, or their names fall back to the alias.
    entries = {e["key"]: e for e in json.loads(
        (Path(__file__).resolve().parents[1] / "locale" / "projects.json").read_text())}
    expected = {"offerte": "hubOfferte", "etl": "etlV2", "db": "dbAcme",
                "segnalatori": "portaleSegnalazioni",
                "offerte-pubbliche": "offertePubbliche", "tabella": "tabellaOfferte"}
    for key, slug in expected.items():
        assert entries[key].get("slug") == slug, key
