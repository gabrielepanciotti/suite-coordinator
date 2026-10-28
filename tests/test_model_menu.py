"""/modello multi-level menu (#68): provider -> model -> effort, in place."""

import asyncio
from types import SimpleNamespace

import pytest

import bot
import model_menu
import model_prefs

CHAT, TOPIC = -100123, 7


# --- model_prefs: an OpenCode model must survive a restart --------------------


def test_from_json_keeps_provider_model_ids():
    p = model_prefs.Prefs.from_json(["opencode/glm-5.3", None, "opencode"])
    assert p.model == "opencode/glm-5.3"
    assert p.choice_id == "opencode"


def test_from_json_still_normalizes_aliases_and_drops_garbage():
    assert model_prefs.Prefs.from_json(["O", "alto", None]).model == "opus"
    assert model_prefs.Prefs.from_json(["not a model", None]).model is None


def test_roundtrip_opencode_prefs():
    p = model_prefs.Prefs("deepseek/deepseek-v4-pro", None, "opencode")
    assert model_prefs.Prefs.from_json(p.to_json()) == p


# --- model_menu: pure pieces ---------------------------------------------------


def test_parse_opencode_models_keeps_only_opencode_provider():
    out = "opencode/big-pickle\ndeepseek/deepseek-v4-flash\n  opencode/hy3-free \nrubbish line\nopencode/big-pickle\n"
    ids = [m.id for m in model_menu.parse_opencode_models(out)]
    assert ids == ["opencode/big-pickle", "opencode/hy3-free"]


VERBOSE = """opencode/claude-opus-5
{"family": "claude-opus", "status": "active", "release_date": "2026-07-24",
 "capabilities": {"toolcall": true}}
opencode/claude-opus-5-5
{"family": "claude-opus", "status": "active", "release_date": "2026-09-22",
 "capabilities": {"toolcall": true}}
opencode/glm-5.3
{"family": "glm", "status": "active", "release_date": "2026-08-14",
 "capabilities": {"toolcall": true}}
opencode/glm-6-beta
{"family": "glm", "status": "deprecated", "release_date": "2026-10-01",
 "capabilities": {"toolcall": true}}
opencode/odd-free
{"family": null, "status": "active", "release_date": "2026-10-06",
 "capabilities": {"toolcall": true}}
"""


def test_top_models_newest_of_each_family_in_family_order():
    models = model_menu.parse_opencode_models(VERBOSE)
    assert len(models) == 5
    top = model_menu.top_models(models, ["glm", "claude-opus", "missing"])
    # newest usable per family; deprecated and family-less models are out
    assert top == ["opencode/glm-5.3", "opencode/claude-opus-5-5"]


def test_catalog_all_is_newest_first(monkeypatch):
    monkeypatch.setitem(model_menu._OC_CACHE, "models", [])
    monkeypatch.setitem(model_menu._OC_CACHE, "ts", 0.0)

    async def live(_bin):
        return VERBOSE

    monkeypatch.setattr(model_menu, "_run_opencode_models", live)
    monkeypatch.setattr(model_menu, "_maybe_refresh", lambda *_: None)
    monkeypatch.setattr(model_menu, "TOP_FAMILIES", ["claude-opus", "glm"])
    top, everything = asyncio.run(model_menu.opencode_catalog("opencode", []))
    assert top == ["opencode/claude-opus-5-5", "opencode/glm-5.3"]
    assert everything[0] == "opencode/odd-free"


def test_top_keyboard_keeps_current_pick_and_offers_more():
    rows = model_menu.kb_opencode_top(["opencode/a"], "opencode/z")
    labels = [label for row in rows for label, _ in row]
    assert labels[:2] == ["✓ z", "a"]
    assert ("altri modelli…", "mdl:oca:0") in [b for row in rows for b in row]


def test_every_callback_fits_telegram_cap():
    long_ids = [f"opencode/{'x' * 80}-{i}" for i in range(30)]
    keyboards = [
        model_menu.kb_providers(model_menu.PROVIDERS, "oc"),
        model_menu.kb_claude_models("capi", "opus"),
        model_menu.kb_efforts("capi", "opus", "xhigh"),
        model_menu.kb_deepseek("deepseek-pro"),
        model_menu.kb_opencode(long_ids, long_ids[0], 2),
        model_menu.kb_opencode(long_ids, None, 0, pick="mdl:od", pager="mdl:odp"),
    ]
    for rows in keyboards:
        for row in rows:
            for _label, data in row:
                assert len(data.encode()) <= 64, data


def test_opencode_keyboard_paginates_and_marks_current():
    models = [f"opencode/m{i}" for i in range(20)]
    rows = model_menu.kb_opencode(models, "opencode/m9", 1)
    labels = [label for row in rows for label, _ in row]
    assert "✓ m9" in labels
    assert "m0" not in labels  # page 0 not shown
    assert "2/3" in labels


def test_resolve_hash_rejects_unknown():
    models = ["opencode/a", "opencode/b"]
    assert model_menu.resolve_hash(model_menu.model_hash("opencode/b"), models) == "opencode/b"
    assert model_menu.resolve_hash("deadbeef00", models) is None


def test_subscription_detection(monkeypatch, tmp_path):
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    assert model_menu.claude_subscription_connected() is False
    (tmp_path / ".credentials.json").write_text("{}")
    assert model_menu.claude_subscription_connected() is True
    (tmp_path / ".credentials.json").unlink()
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "tok")
    assert model_menu.claude_subscription_connected() is True


def test_opencode_models_falls_back_and_caches(monkeypatch):
    monkeypatch.setitem(model_menu._OC_CACHE, "models", [])
    monkeypatch.setitem(model_menu._OC_CACHE, "ts", 0.0)

    async def broken(_bin):
        raise OSError("no opencode here")

    monkeypatch.setattr(model_menu, "_maybe_refresh", lambda *_: None)
    monkeypatch.setattr(model_menu, "_run_opencode_models", broken)
    got = asyncio.run(model_menu.opencode_models("opencode", ["opencode/fallback"]))
    assert got == ["opencode/fallback"]

    calls = []

    async def live(_bin):
        calls.append(1)
        return "opencode/live-1\nopencode/live-2\n"

    monkeypatch.setattr(model_menu, "_run_opencode_models", live)
    assert asyncio.run(model_menu.opencode_models("opencode", [])) == [
        "opencode/live-1",
        "opencode/live-2",
    ]
    assert asyncio.run(model_menu.opencode_models("opencode", [])) == [
        "opencode/live-1",
        "opencode/live-2",
    ]
    assert len(calls) == 1  # second open served from cache


# --- bot: callback navigation --------------------------------------------------


class _Query:
    def __init__(self):
        self.message = SimpleNamespace(message_thread_id=TOPIC)
        self.edits = []

    async def edit_message_text(self, text, parse_mode=None, reply_markup=None):
        self.edits.append((text, reply_markup))


def _update(q):
    return SimpleNamespace(callback_query=q, effective_chat=SimpleNamespace(id=CHAT))


def _datas(markup):
    return [b.callback_data for row in markup.inline_keyboard for b in row]


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setattr(bot, "_save_state", lambda: None)
    monkeypatch.setattr(bot, "TOPIC_PREFS", {})
    monkeypatch.setattr(bot, "TOPIC_BINDING", {(CHAT, TOPIC): ("demo", "main")})
    monkeypatch.setattr(bot, "PROJECT_API_KEY_OVERRIDES", {})
    for k in ("ANTHROPIC_API_KEY", "DEEPSEEK_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("OPENCODE_API_KEY", "oc-key")
    monkeypatch.setattr(model_menu, "claude_subscription_connected", lambda: True)

    async def catalog():
        return ["opencode/glm-5.3"], ["opencode/glm-5.3", "opencode/big-pickle"]

    monkeypatch.setattr(bot, "_opencode_catalog", catalog)
    return monkeypatch


def _tap(data):
    q = _Query()
    asyncio.run(bot._handle_mdl_callback(_update(q), data.split(":")))
    return q.edits[-1]


def test_home_lists_only_available_providers(env):
    _text, markup = _tap("mdl:home")
    datas = _datas(markup)
    assert "mdl:pv:csub:0" in datas and "mdl:pv:oc:0" in datas
    assert "mdl:pv:capi:0" not in datas and "mdl:pv:ds:0" not in datas


def test_subscription_hidden_without_credentials(env):
    env.setattr(model_menu, "claude_subscription_connected", lambda: False)
    _text, markup = _tap("mdl:home")
    assert "mdl:pv:csub:0" not in _datas(markup)
    text, _ = _tap("mdl:set:claude-sub")
    assert "non e' disponibile" in text
    assert bot._get_prefs(CHAT, TOPIC) == model_prefs.Prefs()


def test_claude_three_levels_pin_model_and_effort(env):
    _text, markup = _tap("mdl:pv:csub:0")
    assert "mdl:cm:csub:opus" in _datas(markup)
    _text, markup = _tap("mdl:cm:csub:opus")
    assert "mdl:ce:csub:opus:max" in _datas(markup)
    text, _ = _tap("mdl:ce:csub:opus:max")
    assert bot._get_prefs(CHAT, TOPIC) == model_prefs.Prefs("opus", "max", "claude-sub")
    assert "Claude (abbonamento) · opus" in text


def test_effort_auto_leaves_effort_unset(env):
    _tap("mdl:ce:csub:sonnet:auto")
    assert bot._get_prefs(CHAT, TOPIC) == model_prefs.Prefs("sonnet", None, "claude-sub")


def test_opencode_pick_by_hash_runs_on_opencode(env):
    _text, markup = _tap("mdl:pv:oc:0")
    h = model_menu.model_hash("opencode/glm-5.3")
    assert f"mdl:oc:{h}" in _datas(markup)
    _tap(f"mdl:oc:{h}")
    prefs = bot._get_prefs(CHAT, TOPIC)
    assert prefs == model_prefs.Prefs(model="opencode/glm-5.3", choice_id="opencode")
    backend, model, extra_env = bot._resolve_turn("demo", prefs)
    assert (backend, model, extra_env) == (
        "opencode",
        "opencode/glm-5.3",
        {"OPENCODE_API_KEY": "oc-key"},
    )


def test_more_lists_every_opencode_model(env):
    _text, markup = _tap("mdl:pv:oc:0")
    assert f"mdl:oc:{model_menu.model_hash('opencode/big-pickle')}" not in _datas(markup)
    _text, markup = _tap("mdl:oca:0")
    assert f"mdl:oc:{model_menu.model_hash('opencode/big-pickle')}" in _datas(markup)


def test_stale_or_forged_taps_change_nothing(env):
    for data in (
        "mdl:oc:0000000000",
        "mdl:pv:capi:0",
        "mdl:ce:csub:gpt:max",
        "mdl:ce:capi:opus:max",
    ):
        text, _ = _tap(data)
        assert bot._get_prefs(CHAT, TOPIC) == model_prefs.Prefs(), data


def test_auto_resets_card(env):
    _tap("mdl:ce:csub:opus:max")
    _tap("mdl:auto")
    assert bot._get_prefs(CHAT, TOPIC) == model_prefs.Prefs()


def test_opencodemodel_buttons_set_deployment_default(env):
    env.setattr(bot, "_RUNTIME_SETTINGS", {})
    q = _Query()
    h = model_menu.model_hash("opencode/big-pickle")
    asyncio.run(bot._handle_ocdefault_callback(_update(q), ["mdl", "od", h]))
    assert bot._default_opencode_model() == "opencode/big-pickle"
    asyncio.run(bot._handle_ocdefault_callback(_update(q), ["mdl", "odauto"]))
    assert bot._default_opencode_model() == bot.claude_runner.OPENCODE_MODEL


def test_models_dev_refresh_runs_in_background_once_per_ttl(monkeypatch):
    monkeypatch.setattr(model_menu, "_REFRESH", {"task": None, "ts": 0.0})
    monkeypatch.setitem(model_menu._OC_CACHE, "ts", 123.0)
    runs = []

    async def fake_refresh(_bin):
        runs.append(1)
        model_menu._OC_CACHE["ts"] = 0.0

    monkeypatch.setattr(model_menu, "_refresh_models_dev", fake_refresh)

    async def scenario():
        model_menu._maybe_refresh("opencode", 1000.0)
        model_menu._maybe_refresh("opencode", 1001.0)  # still running / within TTL
        await model_menu._REFRESH["task"]
        model_menu._maybe_refresh("opencode", 1002.0)  # done, but within TTL

    asyncio.run(scenario())
    assert runs == [1]
    assert model_menu._OC_CACHE["ts"] == 0.0  # next open re-reads the fresh catalog


def test_labels_carry_price_per_million_tokens(monkeypatch):
    sample = (
        'opencode/paid\n{"family": "x", "release_date": "2026-01-01",'
        ' "cost": {"input": 0.14, "output": 2.5}}\n'
        'opencode/free\n{"family": "y", "release_date": "2026-01-01",'
        ' "cost": {"input": 0, "output": 0}}\n'
    )
    costs = {m.id: m.cost for m in model_menu.parse_opencode_models(sample)}
    monkeypatch.setitem(model_menu._OC_CACHE, "costs", costs)
    assert model_menu.model_label("opencode/paid") == "paid · $0.14/$2.5"
    assert model_menu.model_label("opencode/free") == "free · gratis"
    assert model_menu.model_label("opencode/unknown") == "unknown"
