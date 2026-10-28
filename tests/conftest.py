"""Set minimal env BEFORE importing bot, so module import never raises."""

import os
import sys

import pytest

# Set required env vars before bot/agents are imported anywhere.
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "t")
os.environ.setdefault("ALLOWED_CHAT_IDS", "111,222")

# The suite runs on its OWN locale/ (tests/fixtures/locale), never on the
# installation's: a checkout whose locale/ is someone else's, or absent, must
# pass the same (#43, "second set" test). Forced, not setdefault — an env
# inherited from a configured shell would silently point back at the real one.
# The fixture registry keeps the project keys the tests assert on; its
# defaults.json is empty, so every engine default is the neutral one.
os.environ["COORDINATOR_LOCALE_FILE"] = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "fixtures", "locale", "defaults.json"
)

# Make the project root importable (bot.py, agents.py live one level up).
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.fixture(autouse=True)
def _pin_default_registry():
    """Pin the project registry to the bundled defaults for every test.

    On a real device a gitignored ``projects.local.json`` overrides ``PROJECTS``
    with a per-machine subset, which makes the registry-integrity tests
    (test_groups/test_profiles/test_projects) fail spuriously when run there.
    The suite validates the canonical default registry, so pin it — the tests
    then behave identically on a fresh checkout (CI) and on a configured device.
    Tests that call ``load_projects()`` directly are unaffected (they read the
    returned value, not this global).

    Set through ``vars()`` rather than ``monkeypatch.setattr``: since #35 the
    registry is lazy, and ``PROJECTS`` is served by a module ``__getattr__``
    until something loads it. ``monkeypatch.setattr`` *reads* the attribute
    first to remember the old value — which would trigger a real load (and a
    real HTTP call to life_backend) in every single test. Reading nothing and
    writing directly keeps the suite offline; the restore below is the same
    undo monkeypatch would have done.
    """
    import projects

    had = "PROJECTS" in vars(projects)
    previous = vars(projects).get("PROJECTS")
    projects.PROJECTS = projects._DEFAULT_PROJECTS
    yield
    if had:
        projects.PROJECTS = previous
    else:
        del projects.PROJECTS


@pytest.fixture(autouse=True)
def _empty_life_home(tmp_path_factory, monkeypatch):
    """The device registry (~/.life) is device state: never let it leak in.

    Since #82 ``hub_config.load_config`` adds the bus tenant of every registered
    instance, so on dev-linux the real ``~/.life`` would add life-energypass to
    every test that asserts on ``api_keys``. Tests that need a registry write
    one and set ``LIFE_HOME`` themselves.
    """
    monkeypatch.setenv("LIFE_HOME", str(tmp_path_factory.mktemp("life-home-vuota")))


@pytest.fixture(autouse=True)
def _host_exec_off(monkeypatch):
    """Host execution (#46) is device config: never let a device .env turn it on.

    Some tests load the real ``.env`` into ``os.environ``; on a device where
    ``HOST_EXEC_SSH`` is set every bus wake would then take the ssh path. Tests
    that exercise it switch it on themselves.
    """
    monkeypatch.delenv("HOST_EXEC_SSH", raising=False)
    runner = sys.modules.get("claude_runner")
    if runner is not None:
        monkeypatch.setattr(runner, "HOST_EXEC_SSH", "")


@pytest.fixture
def locale_defaults(tmp_path, monkeypatch):
    """Point ``local_defaults`` at a throwaway locale file (issue #43).

    Tests must not depend on — or name — the values of the real installation in
    ``locale/defaults.json``. Call the fixture with the map the test needs; it
    writes it, points ``COORDINATOR_LOCALE_FILE`` at it and drops the cache, so
    code that reads the defaults at call time sees exactly that map. Code that
    reads them at import time needs a reload on top (see test_graph_mailer).
    """
    import json

    import local_defaults

    def write(data: dict) -> None:
        path = tmp_path / "locale_defaults.json"
        path.write_text(json.dumps(data), encoding="utf-8")
        monkeypatch.setenv(local_defaults.LOCALE_FILE_ENV, str(path))
        local_defaults.clear_cache()

    yield write
    local_defaults.clear_cache()


@pytest.fixture(autouse=True)
def _reset_bot_dedup_state():
    """Module-level dedup/lock state in bot.py must not leak between tests:
    the same message ids recur across tests, and an asyncio.Lock is bound to
    the loop of the asyncio.run() that first contended it."""
    yield
    bot = sys.modules.get("bot")
    if bot is None:
        return
    for name in ("_PHOTOS_SEEN", "_ARCHIVED", "_TURN_LOCKS"):
        state = getattr(bot, name, None)
        if state is not None:
            state.clear()


API_TEST_TOKEN = "test-api-token"


@pytest.fixture(autouse=True)
def _api_token(monkeypatch):
    """The notifications API is closed by default (#88): give every TestClient the token.

    The suite tests what the routes do; the gate itself is tested in
    tests/test_api_auth.py, which drops the header where it needs to.
    """
    from starlette.testclient import TestClient

    monkeypatch.setenv("COORDINATOR_API_TOKEN", API_TEST_TOKEN)
    original = TestClient.__init__

    def _init(self, *args, headers=None, **kwargs):
        original(self, *args, headers={"X-Api-Token": API_TEST_TOKEN, **(headers or {})},
                 **kwargs)

    monkeypatch.setattr(TestClient, "__init__", _init)
