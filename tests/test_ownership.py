"""Unit tests for ownership.py (dynamic Telegram polling ownership)."""

import asyncio
import dataclasses
import json

import httpx

import pytest

import ownership


@pytest.fixture(autouse=True)
def _no_instance_ladder(monkeypatch):
    # #83 F4: these tests pin the .env priorities; the device's real instance
    # registry (which may declare `pollers`) must not leak into them.
    monkeypatch.setenv("OWNERSHIP_LADDER", "off")
    monkeypatch.delenv("DEVICE_NAME", raising=False)
    monkeypatch.delenv("LIFE_DEVICE", raising=False)


def test_config_disabled_without_peer_url(monkeypatch):
    monkeypatch.delenv("PEER_OWNERSHIP_URL", raising=False)
    cfg = ownership.load_config()
    assert cfg.enabled is False


def test_config_parses_env(monkeypatch, tmp_path):
    monkeypatch.setenv("DEVICE_NAME", "pc")
    monkeypatch.setenv("OWNER_PRIORITY", "100")
    monkeypatch.setenv("OWNERSHIP_PORT", "8013")
    monkeypatch.setenv("PEER_OWNERSHIP_URL", "http://100.64.0.2:8013/")
    monkeypatch.setenv("OWNERSHIP_PROBE_INTERVAL", "15")
    monkeypatch.setenv("OWNERSHIP_FAIL_THRESHOLD", "3")
    monkeypatch.setenv("OWNERSHIP_BIND_HOST", "127.0.0.1")
    monkeypatch.setenv("STATE_PATH", str(tmp_path / "state.json"))
    cfg = ownership.load_config()
    assert cfg.enabled is True
    assert cfg.device == "pc"
    assert cfg.priority == 100
    assert cfg.port == 8013
    assert cfg.peer_url == "http://100.64.0.2:8013"  # trailing slash stripped
    assert cfg.probe_interval == 15.0
    assert cfg.fail_threshold == 3
    assert cfg.bind_host == "127.0.0.1"
    assert cfg.state_path == str(tmp_path / "ownership-pc.json")


def test_load_config_clamps_threshold(monkeypatch):
    monkeypatch.setenv("OWNERSHIP_FAIL_THRESHOLD", "0")
    monkeypatch.setenv("PEER_OWNERSHIP_URL", "http://x:1")
    assert ownership.load_config().fail_threshold == 1


def _cfg(tmp_path, device="pc", priority=100, threshold=3):
    return ownership.OwnershipConfig(
        device=device,
        priority=priority,
        port=8013,
        peer_url="http://peer:8013",
        probe_interval=0.01,
        fail_threshold=threshold,
        state_dir=str(tmp_path),
        bind_host="127.0.0.1",
    )


class Harness:
    """Manager with recorded acquire/release and a scriptable peer probe."""

    def __init__(self, tmp_path, **kw):
        self.calls: list[str] = []
        self.peer: dict | None = None

        async def acquire():
            self.calls.append("acquire")

        async def release():
            self.calls.append("release")

        async def probe():
            return self.peer

        self.mgr = ownership.OwnershipManager(
            _cfg(tmp_path, **kw), acquire=acquire, release=release, probe=probe
        )


def test_standby_stays_when_stronger_peer_claims(tmp_path):
    h = Harness(tmp_path, priority=10)  # I am the minipc
    h.peer = {"device": "pc", "priority": 100, "wants_ownership": True, "is_polling": True}
    asyncio.run(h.mgr.tick())
    assert h.calls == []
    assert h.mgr.is_polling is False


def test_takeover_only_after_fail_threshold(tmp_path):
    h = Harness(tmp_path, priority=10, threshold=3)
    h.peer = None  # peer unreachable
    asyncio.run(h.mgr.tick())
    asyncio.run(h.mgr.tick())
    assert h.calls == []  # hysteresis: 2 < 3
    asyncio.run(h.mgr.tick())
    assert h.calls == ["acquire"]
    assert h.mgr.is_polling is True


def test_acquire_immediately_over_weaker_peer(tmp_path):
    h = Harness(tmp_path, priority=100)  # I am the PC
    h.peer = {"device": "minipc", "priority": 10, "wants_ownership": True, "is_polling": True}
    asyncio.run(h.mgr.tick())
    assert h.calls == ["acquire"]  # no hysteresis when the peer is alive but weaker


def test_acquire_when_peer_yields(tmp_path):
    h = Harness(tmp_path, priority=10)
    h.peer = {"device": "pc", "priority": 100, "wants_ownership": False, "is_polling": False}
    asyncio.run(h.mgr.tick())
    assert h.calls == ["acquire"]


def test_owner_releases_when_stronger_peer_claims(tmp_path):
    h = Harness(tmp_path, priority=10)
    h.peer = None
    for _ in range(3):
        asyncio.run(h.mgr.tick())
    assert h.mgr.is_polling is True
    h.peer = {"device": "pc", "priority": 100, "wants_ownership": True, "is_polling": False}
    asyncio.run(h.mgr.tick())
    assert h.calls == ["acquire", "release"]
    assert h.mgr.is_polling is False


def test_no_acquire_when_i_dont_want_ownership(tmp_path):
    h = Harness(tmp_path, priority=100)
    asyncio.run(h.mgr.set_wants(False))
    h.peer = None
    for _ in range(5):
        asyncio.run(h.mgr.tick())
    assert h.calls == []


def test_set_wants_false_releases_owner(tmp_path):
    h = Harness(tmp_path, priority=100)
    h.peer = {"device": "minipc", "priority": 10, "wants_ownership": True, "is_polling": True}
    asyncio.run(h.mgr.tick())
    assert h.mgr.is_polling is True
    asyncio.run(h.mgr.set_wants(False))
    assert h.calls == ["acquire", "release"]


def test_persistent_conflict_yields(tmp_path):
    h = Harness(tmp_path, priority=10, threshold=3)
    h.peer = None
    for _ in range(3):
        asyncio.run(h.mgr.tick())
    assert h.mgr.is_polling is True
    for _ in range(3):
        asyncio.run(h.mgr.on_conflict())
    assert h.calls == ["acquire", "release"]


def test_conflict_ignored_when_not_polling(tmp_path):
    h = Harness(tmp_path, priority=10, threshold=3)
    for _ in range(5):
        asyncio.run(h.mgr.on_conflict())
    assert h.calls == []  # guard: 409 in standby must not release anything


def test_conflict_counter_resets_after_release(tmp_path):
    h = Harness(tmp_path, priority=10, threshold=3)
    h.peer = None
    for _ in range(3):
        asyncio.run(h.mgr.tick())  # acquire
    asyncio.run(h.mgr.on_conflict())
    asyncio.run(h.mgr.on_conflict())  # 2 conflicts, below threshold
    h.peer = {"device": "pc", "priority": 100, "wants_ownership": True, "is_polling": True}
    asyncio.run(h.mgr.tick())  # priority-driven release
    h.peer = None
    for _ in range(3):
        asyncio.run(h.mgr.tick())  # re-acquire
    asyncio.run(h.mgr.on_conflict())  # single fresh 409 must NOT trigger release
    assert h.calls == ["acquire", "release", "acquire"]


def test_wants_persisted_per_device(tmp_path):
    h = Harness(tmp_path, device="pc")
    asyncio.run(h.mgr.set_wants(False))
    raw = json.loads((tmp_path / "ownership-pc.json").read_text())
    assert raw == {"wants_ownership": False, "epoch": 0}
    # A fresh manager on the same state file reloads the flag.
    h2 = Harness(tmp_path, device="pc")
    assert h2.mgr.wants_ownership is False


def test_apply_handoff_sets_wants_by_target(tmp_path):
    h = Harness(tmp_path, device="pc")
    st = asyncio.run(h.mgr.apply_handoff("minipc"))
    assert h.mgr.wants_ownership is False
    assert st["wants_ownership"] is False
    asyncio.run(h.mgr.apply_handoff("pc"))
    assert h.mgr.wants_ownership is True


def test_http_status_and_handoff(tmp_path, monkeypatch):
    monkeypatch.setenv("OWNERSHIP_TOKEN", "peer-secret")

    async def scenario():
        h = Harness(tmp_path, device="pc")
        server = await ownership.serve_status(h.mgr, port=0)
        try:
            port = server.sockets[0].getsockname()[1]
            base = f"http://127.0.0.1:{port}"
            async with httpx.AsyncClient(timeout=4) as client:
                r = await client.get(base + "/ownership")
                assert r.status_code == 200
                assert r.json()["device"] == "pc"
                assert r.json()["wants_ownership"] is True
                r = await client.post(
                    base + "/ownership/handoff", json={"target_device": "minipc"},
                    headers={"X-Ownership-Token": "peer-secret"},
                )
                assert r.status_code == 200
                assert r.json()["wants_ownership"] is False
                r = await client.get(base + "/nope")
                assert r.status_code == 404
        finally:
            server.close()
            await server.wait_closed()

    asyncio.run(scenario())


def test_handoff_needs_the_shared_token(tmp_path, monkeypatch):
    """#88: whoever reaches the port must not move the poller without the secret."""
    monkeypatch.setenv("OWNERSHIP_TOKEN", "peer-secret")

    async def scenario():
        h = Harness(tmp_path, device="pc")
        server = await ownership.serve_status(h.mgr, port=0)
        try:
            base = f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}"
            body = {"target_device": "minipc"}
            async with httpx.AsyncClient(timeout=4) as client:
                r = await client.post(base + "/ownership/handoff", json=body)
                assert r.status_code == 401
                r = await client.post(base + "/ownership/handoff", json=body,
                                      headers={"X-Ownership-Token": "wrong"})
                assert r.status_code == 401
                assert h.mgr.wants_ownership is True  # nothing moved
                # The status GET stays open: peers probe it without a secret.
                assert (await client.get(base + "/ownership")).status_code == 200
        finally:
            server.close()
            await server.wait_closed()

    asyncio.run(scenario())


def test_handoff_refused_when_no_token_is_configured(tmp_path, monkeypatch):
    """Unset OWNERSHIP_TOKEN closes the handoff (503), it does not open it."""
    monkeypatch.delenv("OWNERSHIP_TOKEN", raising=False)

    async def scenario():
        h = Harness(tmp_path, device="pc")
        server = await ownership.serve_status(h.mgr, port=0)
        try:
            base = f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}"
            async with httpx.AsyncClient(timeout=4) as client:
                r = await client.post(base + "/ownership/handoff",
                                      json={"target_device": "minipc"},
                                      headers={"X-Ownership-Token": ""})
                assert r.status_code == 503
                assert h.mgr.wants_ownership is True
        finally:
            server.close()
            await server.wait_closed()

    asyncio.run(scenario())


def test_forward_handoff_sends_the_token(tmp_path, monkeypatch):
    """The peer that receives a forwarded handoff accepts it: same secret on both."""
    monkeypatch.setenv("OWNERSHIP_TOKEN", "peer-secret")

    async def scenario():
        (tmp_path / "peer").mkdir()
        peer = Harness(tmp_path / "peer", device="minipc")
        server = await ownership.serve_status(peer.mgr, port=0)
        try:
            port = server.sockets[0].getsockname()[1]
            cfg = dataclasses.replace(_cfg(tmp_path), peer_url=f"http://127.0.0.1:{port}")
            st = await ownership.forward_handoff(cfg, "pc")
            assert st is not None and st["wants_ownership"] is False
            assert peer.mgr.wants_ownership is False
        finally:
            server.close()
            await server.wait_closed()

    asyncio.run(scenario())


def test_forward_handoff_unreachable_returns_none(tmp_path):
    cfg = _cfg(tmp_path)  # peer_url http://peer:8013 — not resolvable
    result = asyncio.run(ownership.forward_handoff(cfg, "minipc"))
    assert result is None


# --- several peers, and per-bot domains ----------------------------------------


class MultiHarness:
    """Manager with N scriptable peers (the probe returns one answer per peer)."""

    def __init__(self, tmp_path, priority, threshold=3, domain=""):
        self.calls: list[str] = []
        self.peers: list[dict | None] = []

        async def acquire():
            self.calls.append("acquire")

        async def release():
            self.calls.append("release")

        async def probe():
            return list(self.peers)

        cfg = ownership.OwnershipConfig(
            device="d",
            priority=priority,
            port=8013,
            peer_url="http://a:8013,http://b:8013",
            probe_interval=0.01,
            fail_threshold=threshold,
            state_dir=str(tmp_path),
            bind_host="127.0.0.1",
            domain=domain,
        )
        self.mgr = ownership.OwnershipManager(
            cfg, acquire=acquire, release=release, probe=probe
        )

    def tick(self):
        asyncio.run(self.mgr.tick())


def _peer(priority, wants=True, polling=False):
    return {"device": "x", "priority": priority, "wants_ownership": wants,
            "is_polling": polling}


def test_config_splits_several_peers(monkeypatch):
    monkeypatch.setenv("PEER_OWNERSHIP_URL", "http://a:1/, http://b:2  http://a:1")
    cfg = ownership.load_config()
    assert cfg.peers == ("http://a:1", "http://b:2")
    assert cfg.enabled is True


def test_domain_config_overrides_priority_and_peers(monkeypatch, tmp_path):
    monkeypatch.setenv("OWNER_PRIORITY", "7")
    monkeypatch.setenv("OWNER_PRIORITY_COMUNITY", "10")
    monkeypatch.setenv("PEER_OWNERSHIP_URL", "http://a:1")
    monkeypatch.setenv("STATE_PATH", str(tmp_path / "state.json"))
    main, dom = ownership.load_config(), ownership.load_config("comunity")
    assert (main.priority, dom.priority) == (7, 10)
    assert dom.peers == ("http://a:1",)  # no specific peers: shares the general list
    assert dom.status_path == "/ownership/comunity"
    assert dom.state_path.endswith("ownership-device-comunity.json")
    assert main.state_path.endswith("ownership-device.json")


def test_server_waits_for_both_stronger_and_weaker_peers_to_be_dead(tmp_path):
    # Server (7) with minipc (10) and pc (5): minipc blips for one probe only.
    h = MultiHarness(tmp_path, priority=7)
    h.peers = [_peer(10), _peer(5)]
    h.tick()
    assert h.calls == []  # the stronger peer claims
    h.peers = [None, _peer(5)]
    h.tick()
    h.tick()
    assert h.calls == []  # 2 < 3: minipc may just be slow
    h.tick()
    assert h.calls == ["acquire"]  # declared dead, the weaker pc does not matter


def test_weaker_peer_never_delays_the_takeover(tmp_path):
    h = MultiHarness(tmp_path, priority=10)
    h.peers = [_peer(7), _peer(5)]
    h.tick()
    assert h.calls == ["acquire"]


def test_known_weaker_dead_peer_is_ignored(tmp_path):
    h = MultiHarness(tmp_path, priority=7)
    h.peers = [_peer(10, wants=False), _peer(5)]
    h.tick()
    h.calls.clear()
    h.mgr.is_polling = False
    # pc (known weaker) disappears while minipc keeps yielding: no wait for pc.
    h.peers = [_peer(10, wants=False), None]
    h.tick()
    assert h.calls == ["acquire"]


def test_owner_yields_to_stronger_peer_coming_back(tmp_path):
    h = MultiHarness(tmp_path, priority=7)
    h.peers = [None, None]
    for _ in range(3):
        h.tick()
    assert h.mgr.is_polling is True
    h.peers = [_peer(10), None]
    h.tick()
    assert h.calls == ["acquire", "release"]


def test_http_domain_routes(tmp_path, monkeypatch):
    monkeypatch.setenv("OWNERSHIP_TOKEN", "peer-secret")

    async def scenario():
        main = Harness(tmp_path, device="pc").mgr
        dom = ownership.OwnershipManager(
            ownership.OwnershipConfig(
                device="pc", priority=9, port=0, peer_url="", probe_interval=1.0,
                fail_threshold=1, state_dir=str(tmp_path), domain="comunity",
            ),
            acquire=main._acquire, release=main._release,
        )
        server = await ownership.serve_status(main, port=0, domains={"comunity": dom})
        try:
            port = server.sockets[0].getsockname()[1]
            base = f"http://127.0.0.1:{port}"
            async with httpx.AsyncClient(timeout=4) as client:
                assert (await client.get(base + "/ownership")).json()["priority"] == 100
                assert (await client.get(base + "/ownership/comunity")).json()["priority"] == 9
                r = await client.post(
                    base + "/ownership/comunity/handoff", json={"target_device": "other"},
                    headers={"X-Ownership-Token": "peer-secret"},
                )
                assert r.json()["wants_ownership"] is False
                assert main.wants_ownership is True  # the domains do not leak
                assert (await client.get(base + "/ownership/nope")).status_code == 404
        finally:
            server.close()
            await server.wait_closed()

    asyncio.run(scenario())


def test_device_name_falls_back_to_life_device(monkeypatch):
    # #83 F5: one name for ownership and the poller ladder.
    assert ownership.load_config().device == "device"
    monkeypatch.setenv("LIFE_DEVICE", "pg1-vus-rmdevsrv")
    assert ownership.load_config().device == "pg1-vus-rmdevsrv"
    monkeypatch.setenv("DEVICE_NAME", "  ")
    assert ownership.load_config().device == "pg1-vus-rmdevsrv"
    monkeypatch.setenv("DEVICE_NAME", "pc")
    assert ownership.load_config().device == "pc"
