"""#83 F3: chi ascolta non è chi esegue."""

import asyncio
from types import SimpleNamespace

import pytest

import bot
import hub
import hub_store
import instance_acl
import remote_turn as rt
from project_model import Project

AGENTS = [
    {"name": "callRecorder-windows", "device": "windows", "listening": True},
    {"name": "callRecorder-prod", "device": "prod", "listening": False},
    {"name": "callRecorder-dev-linux", "device": "dev-linux", "listening": True},
    {"name": "hubOfferte-prod", "device": "prod", "listening": True},
    {"name": "spento-windows", "device": "windows", "listening": True, "is_active": False},
]


def test_mai_il_device_che_fa_da_poller():
    names = [e.name for e in rt.candidates("callRecorder", AGENTS, local_suffix="dev-linux")]
    assert names == ["callRecorder-windows", "callRecorder-prod"]


def test_senza_scala_vince_chi_ascolta():
    assert rt.pick("callrecorder", AGENTS, local_suffix="dev-linux").name == "callRecorder-windows"


def test_la_scala_ordina_fra_chi_ascolta_ma_non_preferisce_uno_spento():
    agents = AGENTS + [{"name": "callRecorder-integra", "device": "integra", "listening": True}]
    got = rt.pick("callRecorder", agents, local_suffix="dev-linux", ladder=["prod", "integra", "windows"])
    assert got.name == "callRecorder-integra"


def test_nessun_esecutore_e_nessun_ripiego_inventato():
    assert rt.pick("nessuno", AGENTS, local_suffix="dev-linux") is None
    assert rt.pick("spento", AGENTS, local_suffix="dev-linux") is None


def test_un_esecutore_spento_e_una_coda_non_un_errore():
    got = rt.pick("hubOfferte", [{"name": "hubOfferte-prod", "device": "prod", "listening": False}],
                  local_suffix="dev-linux")
    assert got.name == "hubOfferte-prod" and not got.listening


def test_testo_della_risposta():
    assert rt.reply_text("x-windows", " ciao ") == "\U0001f6f0 x-windows\nciao"
    assert rt.reply_text("x", "a" * 5000).endswith("…")


# --- la risposta torna in chat --------------------------------------------------------


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(hub_store, "HUB_TOPICS_PATH", str(tmp_path / "hub_topics.json"))
    sent = []

    class Bot:
        async def send_message(self, **k):
            sent.append(k)

    monkeypatch.setitem(hub._S, "bot", Bot())
    monkeypatch.setitem(hub._S, "config", SimpleNamespace(telegram_agent="owner-telegram"))
    return sent


def test_la_risposta_dell_esecutore_finisce_nella_chat(store):
    hub_store.set_conv_fields(9, remote_turn={"chat_id": -5, "topic_id": 3, "agent": "a-windows"})
    assert asyncio.run(hub._deliver_remote_turn_reply(9, {"from_agent": "a-windows", "body": "fatto"}))
    assert store == [{"chat_id": -5, "message_thread_id": 3, "text": "\U0001f6f0 a-windows\nfatto"}]


def test_la_nostra_eco_non_torna_in_chat(store):
    hub_store.set_conv_fields(9, remote_turn={"chat_id": -5, "topic_id": 0, "agent": "a"})
    assert asyncio.run(hub._deliver_remote_turn_reply(9, {"from_agent": "owner-telegram", "body": "x"}))
    assert store == []


def test_una_conversazione_normale_non_e_un_turno_remoto(store):
    hub_store.set_conv_fields(10, tenant="t")
    assert not asyncio.run(hub._deliver_remote_turn_reply(10, {"from_agent": "a", "body": "x"}))


def test_stessa_chat_stesso_esecutore_stessa_conversazione(store):
    hub_store.set_conv_fields(11, remote_turn={"chat_id": -5, "topic_id": 3, "agent": "a-windows"})
    assert hub._remote_conv_for(-5, 3, "a-windows") == 11
    assert hub._remote_conv_for(-5, 4, "a-windows") is None


# --- nel bot ---------------------------------------------------------------------------


@pytest.fixture
def remoto(monkeypatch):
    monkeypatch.setattr(bot, "_NODE_INDEX", {"cruscotto": ("acme",)})
    monkeypatch.setattr(bot, "_ROUTING_INDEX", {"cruscotto": ("acme",), "callrecorder": ("acme",)})
    monkeypatch.setattr(bot, "_CHAT_INDEX", instance_acl.ChatIndex())
    bot.TOPIC_INSTANCE.clear()
    import instances
    monkeypatch.setattr(instances, "get", lambda i, env=None: object())
    monkeypatch.setattr(instances, "channels_of", lambda inst, env=None: SimpleNamespace(bus_tenant="life-acme"))
    monkeypatch.setattr(bot, "_instance_ladder", lambda iid: ())
    calls = {}

    async def executor(tenant, node, ladder=()):
        calls["lookup"] = (tenant, node)
        return rt.Executor("callRecorder-windows", "windows", True)

    async def send(tenant, agent, text, **k):
        calls["send"] = (tenant, agent, text, k["chat_id"], k["topic_id"])
        return 77

    monkeypatch.setattr(hub, "remote_executor", executor)
    monkeypatch.setattr(hub, "send_remote_turn", send)
    yield calls
    bot.TOPIC_BINDING.clear()


class _Msg:
    message_thread_id = None

    def __init__(self):
        self.replies = []

    async def reply_text(self, t, **k):
        self.replies.append(t)


def _upd(chat_id):
    return SimpleNamespace(effective_chat=SimpleNamespace(id=chat_id, type="supergroup"),
                           effective_message=_Msg())


def test_un_nodo_che_non_sta_qui_va_al_suo_device(remoto):
    bot.TOPIC_BINDING[(-9, 0)] = ("callrecorder", "generale")
    u = _upd(-9)
    p = Project("callrecorder", "Call Recorder", "", "", "")
    assert asyncio.run(bot._maybe_remote_turn(u, p, "registra"))
    assert remoto["send"] == ("life-acme", "callRecorder-windows", "registra", -9, 0)
    assert "Passato a callRecorder-windows" in u.effective_message.replies[0]


def test_un_nodo_locale_resta_qui(remoto):
    bot.TOPIC_BINDING[(-9, 0)] = ("cruscotto", "generale")
    p = Project("cruscotto", "Cruscotto", "", "", "")
    assert not asyncio.run(bot._maybe_remote_turn(_upd(-9), p, "x"))
    assert "send" not in remoto


def test_un_agente_di_un_altra_istanza_non_riceve_mai_il_testo():
    agents = [{"name": "chef-minipc", "device": "minipc", "listening": True, "tenant": "life-personal"}]
    assert rt.pick("chef", agents, local_suffix="dev-linux", tenant="life-acme") is None
    assert rt.pick("chef", agents, local_suffix="dev-linux", tenant="life-personal").name == "chef-minipc"


def test_executors_tradotti_in_suffissi_del_bus():
    # conv 720: dev-linux is `pg1-vus-rmdevsrv` in the ladders, `dev-linux` on the bus.
    devices = {"pg1-vus-rmdevsrv": {"busSuffix": "dev-linux"}, "minipc": {"name": "minipc"}}
    ladder = rt.ladder_suffixes(["pg1-vus-rmdevsrv", "minipc", "NB-FO-GABPAN", "", 3], devices)
    assert ladder == ("dev-linux", "minipc", "NB-FO-GABPAN")
    agents = [{"name": "cosmos-minipc", "device": "minipc", "listening": True},
              {"name": "cosmos-dev-linux", "device": "dev-linux", "listening": True}]
    # Polling from the PC: dev-linux comes first in the ladder and is recognised.
    assert rt.pick("cosmos", agents, local_suffix="pc", ladder=ladder).name == "cosmos-dev-linux"
    assert rt.ladder_suffixes(["a", "a"], None) == ("a",)


def test_riserva_fredda_cede_a_chi_sta_prima_in_scala():
    # conv 723: personal on dev-linux is the reserve of minipc.
    ladder = ("minipc", "dev-linux", "ZenGab")
    vivo = rt.Executor("chef-minipc", "minipc", True)
    assert rt.outranks(vivo, "dev-linux", ladder)
    assert not rt.outranks(rt.Executor("chef-minipc", "minipc", False), "dev-linux", ladder)
    assert not rt.outranks(rt.Executor("chef-ZenGab", "ZenGab", True), "dev-linux", ladder)
    assert not rt.outranks(vivo, "dev-linux", ())
    assert not rt.outranks(None, "dev-linux", ladder)


def _locale_con_scala(monkeypatch, remoto, listening):
    monkeypatch.setattr(bot, "_instance_ladder", lambda iid: ("minipc", "dev-linux"))
    monkeypatch.setattr(hub, "local_suffix", lambda: "dev-linux")

    async def executor(tenant, node, ladder=()):
        return rt.Executor("cruscotto-minipc", "minipc", listening)

    monkeypatch.setattr(hub, "remote_executor", executor)
    bot.TOPIC_BINDING[(-9, 0)] = ("cruscotto", "generale")
    return asyncio.run(bot._maybe_remote_turn(_upd(-9), Project("cruscotto", "Cruscotto", "", "", ""), "x"))


def test_nodo_locale_va_al_primo_della_scala_finche_vive(monkeypatch, remoto):
    assert _locale_con_scala(monkeypatch, remoto, True)
    assert remoto["send"][1] == "cruscotto-minipc"


def test_nodo_locale_resta_qui_se_il_primo_della_scala_e_spento(monkeypatch, remoto):
    assert not _locale_con_scala(monkeypatch, remoto, False)
    assert "send" not in remoto
