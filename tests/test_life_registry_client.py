import json

import httpx
import pytest

import life_registry_client as lrc
from pathlib import Path
from projects import Project

@pytest.fixture(autouse=True)
def _no_retry_delay(monkeypatch):
    """Nobody waits for the transient-failure backoff in a test."""
    monkeypatch.setattr(lrc, "_FETCH_RETRY_DELAY_S", 0)


# shell/src/data/life.graph.default.json, spelled once
_CATALOG_REL_PARTS = Path("shell") / "src" / "data" / "life.graph.default.json"


FALLBACK = (
    Project("stale", "Stale", "", "/old/path", "/workspaces/stale", kind="personal"),
)

# Captured before any test monkeypatches httpx.Client: `httpx` is a single shared
# module object, so `monkeypatch.setattr(lrc.httpx, "Client", fake_client)` mutates
# it globally. Each fake_client below must build its httpx.Client via this saved
# reference, not via the (by-then-patched) `httpx.Client` name, or it recurses into
# itself.
_RealClient = httpx.Client


def _mock_transport(graph: dict, ports: dict, *, status: int = 200) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/graph":
            return httpx.Response(status, json=graph)
        if request.url.path == "/api/ports":
            return httpx.Response(status, json=ports)
        return httpx.Response(404)
    return httpx.MockTransport(handler)


def test_fetch_nodes_merges_graph_and_ports(monkeypatch):
    graph = {"nodes": {
        "travel": {"id": "travel", "label": "Travel", "kind": "cluster",
                   "exists": True, "commercial": False, "children": [],
                   "paths": {"owner": "/nodes/Travel"}},
    }}
    ports = {"travel": {"front": 3353, "back": 10019, "db": 11131}}

    def fake_client(*args, **kwargs):
        return _RealClient(transport=_mock_transport(graph, ports), *args, **kwargs)

    monkeypatch.setattr(lrc.httpx, "Client", fake_client)
    nodes = lrc.fetch_nodes("http://life.test")
    assert len(nodes) == 1
    assert nodes[0]["id"] == "travel"
    assert nodes[0]["ports"] == {"front": 3353, "back": 10019, "db": 11131}
    assert nodes[0]["paths"] == {"owner": "/nodes/Travel"}


def test_fetch_nodes_raises_on_http_error(monkeypatch):
    def fake_client(*args, **kwargs):
        return _RealClient(transport=_mock_transport({}, {}, status=500), *args, **kwargs)

    monkeypatch.setattr(lrc.httpx, "Client", fake_client)
    with pytest.raises(httpx.HTTPError):
        lrc.fetch_nodes("http://life.test")


def test_load_projects_from_life_happy_path(monkeypatch, tmp_path):
    graph = {"nodes": {
        "travel": {"id": "travel", "label": "Travel", "kind": "cluster",
                   "exists": True, "commercial": False, "children": [],
                   "paths": {"testdevice": "/nodes/Travel"}},
        "cosmos": {"id": "cosmos", "label": "Cosmos", "kind": "product",
                   "exists": True, "commercial": True, "children": [],
                   "paths": {"testdevice": "/nodes/Cosmos"}},
    }}
    ports = {"travel": {"front": 3353, "back": 10019, "db": 11131}}

    def fake_client(*args, **kwargs):
        return _RealClient(transport=_mock_transport(graph, ports), *args, **kwargs)

    monkeypatch.setattr(lrc.httpx, "Client", fake_client)
    monkeypatch.setenv("LIFE_DEVICE", "testdevice")
    cache_path = tmp_path / "projects.cache.json"
    overrides_path = tmp_path / "projects.overrides.json"

    result = lrc.load_projects_from_life(
        base_url="http://life.test", cache_path=cache_path,
        overrides_path=overrides_path, fallback=FALLBACK,
    )
    assert [p.key for p in result] == ["travel", "cosmos"]
    assert result[0].host_path == "/nodes/Travel"
    assert cache_path.exists()  # cache written on success


def test_load_projects_from_life_keeps_commercial_nodes(monkeypatch, tmp_path):
    """A product is still one of my nodes, and the picker must offer it.

    `commercial` says what a node is for, not whether it can be opened: Cosmos,
    Mycorise and comunity are products *and* personal repos sitting next to the
    others. Dropping them left the live registry visibly poorer than the
    hand-kept projects.local.json, which is why that file survived.
    """
    graph = {"nodes": {
        "mycorise": {"id": "mycorise", "label": "Mycorise", "kind": "product",
                     "exists": True, "commercial": True, "children": [],
                     "paths": {"testdevice": "/nodes/Mycorise"}},
    }}

    def fake_client(*args, **kwargs):
        return _RealClient(transport=_mock_transport(graph, {}), *args, **kwargs)

    monkeypatch.setattr(lrc.httpx, "Client", fake_client)
    monkeypatch.setenv("LIFE_DEVICE", "testdevice")
    result = lrc.load_projects_from_life(
        base_url="http://life.test",
        cache_path=tmp_path / "c.json",
        overrides_path=tmp_path / "o.json",
        fallback=FALLBACK,
    )
    assert [p.key for p in result] == ["mycorise"]


def test_load_projects_from_life_falls_back_to_cache_on_api_failure(monkeypatch, tmp_path):
    def fake_client(*args, **kwargs):
        return _RealClient(transport=_mock_transport({}, {}, status=500), *args, **kwargs)

    monkeypatch.setattr(lrc.httpx, "Client", fake_client)
    monkeypatch.setenv("LIFE_DEVICE", "testdevice")
    cache_path = tmp_path / "projects.cache.json"
    cache_path.write_text(json.dumps([
        {"id": "cached", "label": "Cached", "kind": "agent", "exists": True,
         "commercial": False, "children": [], "paths": {"testdevice": "/nodes/Cached"},
         "ports": None},
    ]), encoding="utf-8")
    overrides_path = tmp_path / "projects.overrides.json"

    result = lrc.load_projects_from_life(
        base_url="http://life.test", cache_path=cache_path,
        overrides_path=overrides_path, fallback=FALLBACK,
    )
    assert len(result) == 1
    assert result[0].key == "cached"


def test_load_projects_from_life_falls_back_to_static_when_no_cache(monkeypatch, tmp_path):
    def fake_client(*args, **kwargs):
        return _RealClient(transport=_mock_transport({}, {}, status=500), *args, **kwargs)

    monkeypatch.setattr(lrc.httpx, "Client", fake_client)
    cache_path = tmp_path / "projects.cache.json"  # does not exist
    overrides_path = tmp_path / "projects.overrides.json"

    result = lrc.load_projects_from_life(
        base_url="http://life.test", cache_path=cache_path,
        overrides_path=overrides_path, fallback=FALLBACK,
    )
    assert result == FALLBACK


def test_load_projects_from_life_applies_overrides(monkeypatch, tmp_path):
    graph = {"nodes": {
        "travel": {"id": "travel", "label": "Travel", "kind": "cluster",
                   "exists": True, "commercial": False, "children": [],
                   "paths": {"testdevice": "/nodes/Travel"}},
    }}
    ports = {}

    def fake_client(*args, **kwargs):
        return _RealClient(transport=_mock_transport(graph, ports), *args, **kwargs)

    monkeypatch.setattr(lrc.httpx, "Client", fake_client)
    monkeypatch.setenv("LIFE_DEVICE", "testdevice")
    cache_path = tmp_path / "projects.cache.json"
    overrides_path = tmp_path / "projects.overrides.json"
    overrides_path.write_text(json.dumps({
        "travel": {"emoji": "\U0001f9f3", "aliases": ["viaggi"], "kind": "personal"},
    }), encoding="utf-8")

    result = lrc.load_projects_from_life(
        base_url="http://life.test", cache_path=cache_path,
        overrides_path=overrides_path, fallback=FALLBACK,
    )
    assert result[0].emoji == "\U0001f9f3"
    assert result[0].aliases == ("viaggi",)


def test_load_projects_from_life_node_missing_path_for_device(monkeypatch, tmp_path):
    """A real node cloned on ANOTHER device is kept, with an empty host_path.

    Distinct from a virtual cluster (no `paths` at all), which is dropped —
    see test_load_projects_from_life_drops_virtual_clusters.
    """
    graph = {"nodes": {
        "orphan": {"id": "orphan", "label": "Orphan", "kind": "agent",
                   "exists": True, "commercial": False, "children": [],
                   "paths": {"anotherdevice": "/elsewhere/Orphan"}},
    }}

    def fake_client(*args, **kwargs):
        return _RealClient(transport=_mock_transport(graph, {}), *args, **kwargs)

    monkeypatch.setattr(lrc.httpx, "Client", fake_client)
    monkeypatch.setenv("LIFE_DEVICE", "testdevice")
    cache_path = tmp_path / "projects.cache.json"
    overrides_path = tmp_path / "projects.overrides.json"

    result = lrc.load_projects_from_life(
        base_url="http://life.test", cache_path=cache_path,
        overrides_path=overrides_path, fallback=FALLBACK,
    )
    assert len(result) == 1
    assert result[0].host_path == ""  # unresolvable on this device, not dropped


def test_load_projects_from_life_drops_virtual_clusters(monkeypatch, tmp_path, caplog):
    """Grouping nodes (`system`, `food`, …) have no repo anywhere and must not
    reach the picker: they are `exists: true` because they exist as *nodes*,
    not as code, and a button for them opens nothing. On the minipc that was 23
    dead entries out of 39.
    """
    graph = {"nodes": {
        "system": {"id": "system", "label": "System", "kind": "cluster",
                   "exists": True, "commercial": False, "children": ["real"]},
        "real": {"id": "real", "label": "Real", "kind": "agent",
                 "exists": True, "commercial": False, "children": [],
                 "paths": {"testdevice": "/srv/Real"}},
    }}

    def fake_client(*args, **kwargs):
        return _RealClient(transport=_mock_transport(graph, {}), *args, **kwargs)

    monkeypatch.setattr(lrc.httpx, "Client", fake_client)
    monkeypatch.setenv("LIFE_DEVICE", "testdevice")
    with caplog.at_level("INFO"):
        result = lrc.load_projects_from_life(
            base_url="http://life.test",
            cache_path=tmp_path / "projects.cache.json",
            overrides_path=tmp_path / "projects.overrides.json",
            fallback=FALLBACK,
        )
    assert [p.key for p in result] == ["real"]
    # ...and it says which one it left out, so a missing node is never a mystery.
    assert "system" in caplog.text


def test_fetch_nodes_retries_a_transient_failure(monkeypatch):
    """The first call after an idle spell is the one that fails.

    life_backend answers /api/ports with a 500 when the pooled Postgres
    connection has gone stale, then serves the next request fine — and that
    lands on our boot, where the registry is built once at import. Two boots in
    a row lost the whole project list to it.
    """
    graph = {"nodes": {"real": {"id": "real", "label": "Real", "exists": True,
                                "children": [], "paths": {"testdevice": "/srv/Real"}}}}
    attempts = []

    def fake_client(*args, **kwargs):
        attempts.append(1)
        status = 500 if len(attempts) == 1 else 200
        return _RealClient(transport=_mock_transport(graph, {}, status=status),
                           *args, **kwargs)

    monkeypatch.setattr(lrc.httpx, "Client", fake_client)
    nodes = lrc.fetch_nodes("http://life.test")
    assert [n["id"] for n in nodes] == ["real"]
    assert len(attempts) == 2


def test_cache_lands_where_the_container_can_write(monkeypatch, tmp_path):
    """The cache is only a fallback rung if it can actually be written.

    Next to the module means a read-only mount in the container, so every write
    lost to EACCES and one transient 500 at boot dropped the registry to the
    static list with nothing in between.
    """
    state = tmp_path / "data"
    state.mkdir()
    monkeypatch.setenv("STATE_PATH", str(state / "state.json"))
    assert lrc._default_cache_path() == state / "projects.cache.json"

    monkeypatch.setenv("STATE_PATH", str(tmp_path / "nope" / "state.json"))
    assert lrc._default_cache_path().parent == Path(lrc.__file__).parent


def test_servable_rejects_an_empty_or_placeholder_workspace(monkeypatch, tmp_path):
    """Two ways a mounted workspace holds no repo, both of which said "yes".

    A bind mount whose host side does not exist gets an empty directory from
    Docker — that is how a WS_* left pointing at another device's path fails.
    And a node with no workspace here is mounted from `.ws-placeholder`, which
    is *not* empty: it holds a .gitkeep. Both used to reach the picker as
    buttons that open nothing.
    """
    monkeypatch.setattr(lrc, "_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setattr(lrc, "_in_container", lambda: True)
    (tmp_path / "empty").mkdir()
    placeholder = tmp_path / "placeholder"
    placeholder.mkdir()
    (placeholder / ".gitkeep").write_text("", encoding="utf-8")
    cloned = tmp_path / "cloned"
    cloned.mkdir()
    (cloned / "node.config").write_text("{}", encoding="utf-8")
    nodes = [
        {"id": "empty", "paths": {"testdevice": "/srv/Empty"}},
        {"id": "placeholder", "paths": {"testdevice": "/srv/Placeholder"}},
        {"id": "cloned", "paths": {"testdevice": "/srv/Cloned"}},
    ]
    servable, dropped = lrc._servable(nodes)
    assert [n["id"] for n in servable] == ["cloned"]
    assert dropped == [
        ("empty", "nessun repo in questo workspace"),
        ("placeholder", "nessun repo in questo workspace"),
    ]


def test_the_shared_placeholder_stays_a_placeholder_once_docker_nests_children(
    monkeypatch, tmp_path
):
    """The cluster mounts nest `<parent>/children/<child>`, and when the parent
    resolves to `.ws-placeholder` Docker creates that `children/` inside it. The
    placeholder then stopped looking empty and every node without a WS_* came
    back as servable — with photos and inbox files landing in the shared dir."""
    monkeypatch.setattr(lrc, "_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setattr(lrc, "_in_container", lambda: True)
    placeholder = tmp_path / "placeholder"
    (placeholder / "children" / "someChild").mkdir(parents=True)
    (placeholder / ".gitkeep").write_text("", encoding="utf-8")
    # A real cluster root carries `children/` too — plus its own repo.
    cluster = tmp_path / "cluster"
    (cluster / "children" / "someChild").mkdir(parents=True)
    (cluster / "node.config").write_text("{}", encoding="utf-8")
    nodes = [
        {"id": "placeholder", "paths": {"testdevice": "/srv/Placeholder"}},
        {"id": "cluster", "paths": {"testdevice": "/srv/Cluster"}},
    ]
    servable, dropped = lrc._servable(nodes)
    assert [n["id"] for n in servable] == ["cluster"]
    assert dropped == [("placeholder", "nessun repo in questo workspace")]


def test_the_mount_filter_does_not_run_outside_the_container(monkeypatch, tmp_path):
    """A host with a `/workspaces` directory of its own is not the container.

    Gating the filter on that path merely existing made every node unservable
    host-side (tests, tooling) and collapsed the registry to the static
    fallback — silently, since a fallback is a plausible-looking list.
    """
    monkeypatch.setattr(lrc, "_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setattr(lrc, "_in_container", lambda: False)
    (tmp_path / "empty").mkdir()
    nodes = [{"id": "empty", "paths": {"testdevice": "/srv/Empty"}}]
    servable, dropped = lrc._servable(nodes)
    assert [n["id"] for n in servable] == ["empty"]
    assert dropped == []


def test_resolve_device_id_uses_env(monkeypatch):
    monkeypatch.setenv("LIFE_DEVICE", "mydevice")
    assert lrc.resolve_device_id() == "mydevice"


def test_load_projects_from_life_tolerates_cache_write_failure(monkeypatch, tmp_path, caplog):
    """API fetch succeeds, but cache write fails — should still return fetched data."""
    graph = {"nodes": {
        "travel": {"id": "travel", "label": "Travel", "kind": "cluster",
                   "exists": True, "commercial": False, "children": [],
                   "paths": {"testdevice": "/nodes/Travel"}},
    }}
    ports = {"travel": {"front": 3353, "back": 10019, "db": 11131}}

    def fake_client(*args, **kwargs):
        return _RealClient(transport=_mock_transport(graph, ports), *args, **kwargs)

    monkeypatch.setattr(lrc.httpx, "Client", fake_client)
    monkeypatch.setenv("LIFE_DEVICE", "testdevice")
    cache_path = tmp_path / "projects.cache.json"
    overrides_path = tmp_path / "projects.overrides.json"

    # Monkeypatch Path.write_text to raise OSError (simulating disk full, permission denied, etc.)
    original_write_text = lrc.Path.write_text
    def failing_write_text(self, data, **kwargs):
        if self == cache_path:
            raise OSError("Disk full")
        return original_write_text(self, data, **kwargs)

    monkeypatch.setattr(lrc.Path, "write_text", failing_write_text)

    result = lrc.load_projects_from_life(
        base_url="http://life.test", cache_path=cache_path,
        overrides_path=overrides_path, fallback=FALLBACK,
    )

    # Should return the fetched data, not the fallback
    assert len(result) == 1
    assert result[0].key == "travel"
    assert result[0].host_path == "/nodes/Travel"

    # Cache file should NOT exist (write failed)
    assert not cache_path.exists()

    # Warning should be logged
    assert "cache write failed" in caplog.text
    assert "Disk full" in caplog.text


# ---------------------------------------------------------------------------
# On-disk catalog: the third rung of the graph fallback chain
#
# The menu is the only way to reach the ~43 nodes the graph marks
# `reachVia: "menu"`. Without this rung the chain ended at `{}` on any machine
# with no life_backend — a blank menu that logs a warning and looks like
# "nothing to show" rather than "I cannot see the graph".
# ---------------------------------------------------------------------------

_CATALOG = {
    "nodes": {
        "life": {"label": "Life", "kind": "root", "children": ["health"], "exists": True},
        "health": {"label": "Health", "kind": "cluster", "children": [], "exists": True},
    }
}


def _write_catalog(root, payload=None):
    """Lay out a Life checkout deeply enough for `catalog_path()` to find it."""
    path = root / "shell" / "src" / "data" / "life.graph.default.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(_CATALOG if payload is None else payload), encoding="utf-8")
    return path


def _no_network(monkeypatch):
    def refused(*args, **kwargs):
        raise httpx.ConnectError("connection refused")
    monkeypatch.setattr(lrc, "fetch_nodes", refused)


def test_catalog_path_is_derived_from_the_mounted_framework_root(monkeypatch, tmp_path):
    monkeypatch.delenv("LIFE_CATALOG_PATH", raising=False)
    monkeypatch.setenv("LIFE_FRAMEWORK_ROOT", str(tmp_path))
    assert lrc.catalog_path() == tmp_path / "shell" / "src" / "data" / "life.graph.default.json"


def test_an_explicit_catalog_path_wins_over_the_framework_root(monkeypatch, tmp_path):
    monkeypatch.setenv("LIFE_FRAMEWORK_ROOT", str(tmp_path / "ignored"))
    monkeypatch.setenv("LIFE_CATALOG_PATH", str(tmp_path / "explicit.json"))
    assert lrc.catalog_path() == tmp_path / "explicit.json"


def test_no_framework_root_means_no_catalog(monkeypatch):
    monkeypatch.delenv("LIFE_CATALOG_PATH", raising=False)
    monkeypatch.delenv("LIFE_FRAMEWORK_ROOT", raising=False)
    assert lrc.catalog_path() is None
    assert lrc.load_catalog_nodes() == {}


def test_the_catalog_keys_the_id_into_each_node(monkeypatch, tmp_path):
    """The catalog is keyed by id; the menu reads `node["id"]`. Injecting it
    here is what lets the same node dicts serve both shapes."""
    monkeypatch.delenv("LIFE_CATALOG_PATH", raising=False)
    monkeypatch.setenv("LIFE_FRAMEWORK_ROOT", str(tmp_path))
    _write_catalog(tmp_path)
    nodes = lrc.load_catalog_nodes()
    assert set(nodes) == {"life", "health"}
    assert nodes["health"]["id"] == "health"
    assert nodes["life"]["children"] == ["health"]


def test_a_malformed_catalog_behaves_like_no_catalog(monkeypatch, tmp_path):
    """Last resort of a fallback chain: it may return nothing, never raise."""
    monkeypatch.delenv("LIFE_CATALOG_PATH", raising=False)
    monkeypatch.setenv("LIFE_FRAMEWORK_ROOT", str(tmp_path))
    path = _write_catalog(tmp_path)
    path.write_text("{ not json", encoding="utf-8")
    assert lrc.load_catalog_nodes() == {}


def test_graph_falls_back_to_the_catalog_when_api_and_cache_are_both_gone(
    monkeypatch, tmp_path
):
    """The exact production failure this rung exists for: no reachable backend,
    no cache on disk. Before, the menu got `{}` and showed nothing."""
    _no_network(monkeypatch)
    monkeypatch.delenv("LIFE_CATALOG_PATH", raising=False)
    monkeypatch.setenv("LIFE_FRAMEWORK_ROOT", str(tmp_path))
    _write_catalog(tmp_path)

    nodes = lrc.load_graph_nodes(
        base_url="http://life.test", cache_path=tmp_path / "absent.json"
    )
    assert set(nodes) == {"life", "health"}


def test_the_disk_cache_still_wins_over_the_catalog(monkeypatch, tmp_path):
    """The cache is a snapshot of the *live* graph (it carries ports and the
    per-device paths the catalog deliberately omits), so it must stay ahead of
    the catalog in the chain."""
    _no_network(monkeypatch)
    monkeypatch.setenv("LIFE_FRAMEWORK_ROOT", str(tmp_path))
    _write_catalog(tmp_path)
    cache = tmp_path / "projects.cache.json"
    cache.write_text(
        json.dumps([{"id": "fromcache", "label": "FromCache", "exists": True}]),
        encoding="utf-8",
    )

    nodes = lrc.load_graph_nodes(base_url="http://life.test", cache_path=cache)
    assert set(nodes) == {"fromcache"}


def test_the_live_api_still_wins_over_everything(monkeypatch, tmp_path):
    monkeypatch.setattr(
        lrc, "fetch_nodes", lambda *a, **k: [{"id": "fromapi", "exists": True}]
    )
    monkeypatch.setenv("LIFE_FRAMEWORK_ROOT", str(tmp_path))
    _write_catalog(tmp_path)
    nodes = lrc.load_graph_nodes(base_url="http://life.test", cache_path=tmp_path / "x.json")
    assert set(nodes) == {"fromapi"}


def test_no_source_at_all_still_returns_an_empty_dict(monkeypatch, tmp_path):
    _no_network(monkeypatch)
    monkeypatch.delenv("LIFE_CATALOG_PATH", raising=False)
    monkeypatch.delenv("LIFE_FRAMEWORK_ROOT", raising=False)
    assert lrc.load_graph_nodes(
        base_url="http://life.test", cache_path=tmp_path / "absent.json"
    ) == {}


def test_the_real_catalog_shape_carries_every_field_the_menu_reads(monkeypatch):
    """Guards the coupling between this repo and Life's catalog format.

    `node_menu` filters on `exists`/`commercial` and renders `label`/`kind`/
    `children`. If Life ever stops shipping `exists`, every node silently turns
    invisible and the menu empties out — the same class of silent blank this
    whole fallback was added to remove. Skipped where Life is not checked out
    beside this repo, so the suite stays green on a machine without it.
    """
    catalog = Path(__file__).resolve().parents[2] / "Life" / _CATALOG_REL_PARTS
    if not catalog.is_file():
        pytest.skip("no Life checkout beside this repo")
    nodes = lrc.load_catalog_nodes(catalog)
    assert nodes, "the real catalog resolved to no nodes"
    for node_id, node in nodes.items():
        assert isinstance(node.get("label"), str), node_id
        assert isinstance(node.get("kind"), str), node_id
        assert isinstance(node.get("children"), list), node_id
        assert "exists" in node, node_id


# --- LIFE_BACKEND_URL=off: a device with no live registry on purpose ---


def _fetch_forbidden(monkeypatch):
    def boom(*args, **kwargs):
        raise AssertionError("fetch_nodes called with the live registry off")
    monkeypatch.setattr(lrc, "fetch_nodes", boom)


@pytest.mark.parametrize("value", ["off", "OFF", " none ", "disabled"])
def test_registry_disabled_values(value):
    assert lrc.registry_disabled(value)


@pytest.mark.parametrize("value", ["", "http://life.test", "http://localhost:9999"])
def test_registry_enabled_values(value):
    assert not lrc.registry_disabled(value)


def test_off_serves_the_static_registry_without_touching_the_network(
    monkeypatch, tmp_path, caplog
):
    """The prod workers: no API call, no cache read, and no ERROR at boot."""
    _fetch_forbidden(monkeypatch)
    cache_path = tmp_path / "projects.cache.json"
    cache_path.write_text("[]", encoding="utf-8")  # must not be read
    with caplog.at_level("INFO", logger="coordinator.life_registry_client"):
        result = lrc.load_projects_from_life(
            base_url="off", cache_path=cache_path,
            overrides_path=tmp_path / "o.json", fallback=FALLBACK,
        )
    assert result == FALLBACK
    assert not [r for r in caplog.records if r.levelname in ("WARNING", "ERROR")]


def test_off_graph_goes_straight_to_the_catalog(monkeypatch, tmp_path):
    _fetch_forbidden(monkeypatch)
    monkeypatch.delenv("LIFE_CATALOG_PATH", raising=False)
    monkeypatch.setenv("LIFE_FRAMEWORK_ROOT", str(tmp_path))
    _write_catalog(tmp_path)
    nodes = lrc.load_graph_nodes(base_url="off", cache_path=tmp_path / "absent.json")
    assert set(nodes) == {"life", "health"}


# --- per-instance graph (#78) -------------------------------------------------


def test_an_instance_graph_comes_from_its_own_composition(monkeypatch):
    import bus_agents

    calls: list[str] = []

    def _fake(instance_id, env=None):
        calls.append(instance_id)
        return {"comunity": {"id": "comunity", "children": []}}

    monkeypatch.setattr(bus_agents, "instance_graph", _fake)
    assert set(lrc.load_graph_nodes(instance="comunity")) == {"comunity"}
    assert calls == ["comunity"]


def test_an_unreadable_non_default_instance_gets_no_graph(monkeypatch):
    """Serving another instance's graph would be worse than an empty menu."""
    import bus_agents
    import instances

    monkeypatch.setattr(bus_agents, "instance_graph", lambda *a, **k: None)
    monkeypatch.setattr(instances, "default_instance_id", lambda env=None: "personal")
    assert lrc.load_graph_nodes(instance="ghost") == {}
