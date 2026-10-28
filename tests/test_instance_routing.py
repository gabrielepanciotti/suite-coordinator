"""#83 F1: l'istanza di un legame, il menu istanza -> nodo, il varco nel bot."""

from types import SimpleNamespace

import pytest

import bot
import instance_acl
import instance_routing as ir
import profiles
from project_model import Project


def _agent(iid, gid):
    return SimpleNamespace(instance_id=iid, graph_id=gid)


NODES = ir.node_index([_agent("acme", "hubOfferte"), _agent("personal", "personalChef"),
                       _agent("acme", "scheduler"), _agent("personal", "scheduler")])


def test_indice_dei_nodi_dal_grafo():
    assert NODES["hubofferte"] == ("acme",)
    assert NODES["scheduler"] == ("acme", "personal")


def test_ordine_di_risoluzione():
    kw = dict(nodes=NODES)
    assert ir.instance_of_binding(1, "scheduler", explicit="personal", chat_owner="acme", **kw) == "personal"
    assert ir.instance_of_binding(1, "personalChef", explicit=None, chat_owner="acme", **kw) == "acme"
    assert ir.instance_of_binding(1, "personalChef", explicit=None, chat_owner=None, **kw) == "personal"
    # in due istanze e nessuna scelta: ambiguo, mai un'ipotesi
    assert ir.instance_of_binding(1, "scheduler", explicit=None, chat_owner=None, **kw) is None
    assert ir.instance_of_binding(1, "ignoto", explicit=None, chat_owner=None, **kw) is None


def test_scelta_esplicita_solo_quando_serve():
    assert ir.needs_explicit("scheduler", NODES)
    assert not ir.needs_explicit("hubOfferte", NODES)


def test_raggruppa_per_istanza():
    by, unmapped = ir.group_by_instance(
        [("offerte", "Offerte", "huboffeRte"), ("sched", "Scheduler", "scheduler"),
         ("x", "X", "fuori")], NODES)
    assert by == {"acme": [("offerte", "Offerte"), ("sched", "Scheduler")],
                  "personal": [("sched", "Scheduler")]}
    assert unmapped == [("x", "X")]


def test_callback_a_due_livelli():
    assert ir.read_choice("gb:in:acme") == ("apri_istanza", "acme")
    assert ir.read_choice("gb:ip:acme:2") == ("pagina_istanza", "acme:2")
    assert ir.read_choice("gb:n:personal:sched") == ("nodo", "personal:sched")
    assert ir.read_choice("gb:offerte") is None
    assert ir.split_node("personal:sched") == ("personal", "sched")
    assert ir.split_node("rotto") is None


def test_pagina_di_un_istanza_ha_forum_ritorno_e_uscita():
    entries = ir.node_entries("acme", [(f"k{i}", f"L{i}") for i in range(10)])
    righe = ir.node_page("acme", entries, 0, channel_key="i:acme")
    dati = [b.dato for r in righe for b in r]
    assert dati[0] == "gb:i:acme"
    assert "gb:n:acme:k0" in dati and "gb:ip:acme:1" in dati
    assert dati[-2:] == ["gb:p:0", "gb:skip"]


# --- il varco nel bot -------------------------------------------------------------

MEMBER_ID, OWNER_ID = 222, 111


@pytest.fixture
def ambiente(monkeypatch):
    monkeypatch.setenv("PROFILE_OWNER_IDS", str(OWNER_ID))
    monkeypatch.setenv("PROFILE_MEMBER_IDS", str(MEMBER_ID))
    bot.TOPIC_BINDING.clear()
    bot.TOPIC_INSTANCE.clear()
    monkeypatch.setattr(bot, "_NODE_INDEX", dict(NODES))
    monkeypatch.setattr(bot, "_ROUTING_INDEX", dict(NODES))
    monkeypatch.setattr(bot, "_CHAT_INDEX", instance_acl.build_chat_index([("acme", -100)]))
    progetti = {"offerte": Project("offerte", "Offerte", "", "", "", slug="hubOfferte"),
                "personalChef": Project("personalChef", "Chef", "", "", "")}
    monkeypatch.setattr(bot, "resolve_project", progetti.get)
    yield
    bot.TOPIC_BINDING.clear()
    bot.TOPIC_INSTANCE.clear()


def _update(uid, chat_id, topic=None):
    msg = SimpleNamespace(message_thread_id=topic, is_topic_message=topic is not None)
    return SimpleNamespace(effective_user=SimpleNamespace(id=uid),
                           effective_chat=SimpleNamespace(id=chat_id, type="supergroup"),
                           effective_message=msg)


def test_member_non_entra_nel_forum_di_acme(ambiente):
    bot.TOPIC_BINDING[(-100, 0)] = ("offerte", "generale")
    assert bot._profile(_update(MEMBER_ID, -100)) is None
    assert bot._profile(_update(OWNER_ID, -100)).key == "owner"


def test_member_entra_nel_gruppo_di_un_nodo_personal(ambiente):
    bot.TOPIC_BINDING[(-200, 0)] = ("personalChef", "generale")
    assert bot._profile(_update(MEMBER_ID, -200)).key == "member"


def test_un_gruppo_senza_istanza_resta_ai_controlli_di_oggi(ambiente):
    assert bot._profile(_update(MEMBER_ID, -300)).key == "member"


def test_scelta_esplicita_vince(ambiente):
    bot.TOPIC_BINDING[(-400, 0)] = ("personalChef", "generale")
    bot.TOPIC_INSTANCE[(-400, 0)] = "acme"
    assert bot._binding_instance(-400, 0) == "acme"
    assert bot._profile(_update(MEMBER_ID, -400)) is None


def test_il_legame_dell_istanza_sopravvive_al_riavvio(ambiente, tmp_path, monkeypatch):
    monkeypatch.setattr(bot, "STATE_PATH", str(tmp_path / "state.json"))
    bot.TOPIC_BINDING[(-500, 0)] = ("personalChef", "generale")
    bot.TOPIC_INSTANCE[(-500, 0)] = "personal"
    bot._save_state()
    bot.TOPIC_INSTANCE.clear()
    bot._load_state()
    assert bot.TOPIC_INSTANCE[(-500, 0)] == "personal"


def test_riga_di_avvio_nomina_i_legami_non_risolti(ambiente, caplog):
    bot.TOPIC_BINDING[(-100, 7)] = ("offerte", "generale")      # forum acme
    bot.TOPIC_BINDING[(-200, 0)] = ("personalChef", "generale")  # grafo: personal
    bot.TOPIC_BINDING[(-300, 0)] = ("fantasma", "generale")      # nessuna istanza
    with caplog.at_level("INFO", logger="coordinator"):
        bot._log_instance_bindings()
    riga = next(r.getMessage() for r in caplog.records if "instance bindings" in r.getMessage())
    assert "2 resolved (acme=1, personal=1), 1 unresolved: -300:0=fantasma" in riga
