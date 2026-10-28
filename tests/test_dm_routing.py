"""#83 F2: la chat diretta col Coordinator generale, prima l'istanza e poi il nodo."""

import asyncio
from types import SimpleNamespace

import pytest

import bot
import dm_routing as dr
import instance_acl
import instance_routing as ir
from project_model import Project

NODES = {
    "acme": [("hubOfferte", "💼 Hub Offerte", ("offerte",)), ("scheduler", "Scheduler", ())],
    "personal": [("personalChef", "🍳 Chef", ("chef",)), ("scheduler", "Scheduler", ())],
}
ALL = ["energypass", "personal", "acme"]


def d(text, *, bound=False, bound_instance=None, visible=ALL):
    return dr.decide(text, bound=bound, bound_instance=bound_instance, visible=visible,
                     all_instances=ALL, nodes_by_instance=NODES)


def test_prefisso_istanza_e_nodo():
    assert d("personal/chef: cosa cucino") == dr.Decision("route", "personal", "personalChef")
    assert d("acme/hubOfferte: stato?").project_key == "hubOfferte"


def test_prefisso_solo_istanza_chiede_il_nodo_se_si_cambia_istanza():
    assert d("acme: ciao", bound=True, bound_instance="acme").kind == "keep"
    got = d("personal: ciao", bound=True, bound_instance="acme")
    assert got.kind == "ask_node" and got.instance == "personal"


def test_un_prefisso_che_non_e_un_istanza_non_e_un_prefisso():
    assert dr.parse_prefix("opus/high: rifai il poller", ALL) is None
    assert dr.parse_prefix("nota: comprare il latte", ALL) is None
    assert d("nota: comprare il latte", bound=True).kind == "keep"


def test_istanza_fuori_profilo_negata():
    assert d("acme/hubOfferte: x", visible=["personal"]).kind == "denied"


def test_dm_legata_non_si_sposta_per_parole():
    assert d("parlami dello chef", bound=True, bound_instance="acme").kind == "keep"


def test_indizio_unico_decide_entrambi_i_piani():
    assert d("idea per lo chef stasera") == dr.Decision("route", "personal", "personalChef")


def test_nodo_in_due_istanze_chiede_l_istanza():
    got = d("apri lo scheduler")
    assert got.kind == "ask_instance" and set(got.options) == {"acme", "personal"}


def test_nessun_indizio_chiede_l_istanza_o_il_nodo():
    assert d("ciao").kind == "ask_instance"
    got = d("ciao", visible=["personal"])
    assert got.kind == "ask_node" and got.instance == "personal"


def test_callback():
    assert dr.read_callback("dmr:i:acme") == ("i", "acme", None)
    assert dr.read_callback("dmr:n:personal:personalChef") == ("n", "personal", "personalChef")
    assert dr.read_callback("dmr:n:personal:") is None
    assert dr.read_callback("gb:x") is None


# --- nel bot ----------------------------------------------------------------------

OWNER, MEMBER = 111, 222


@pytest.fixture
def ambiente(monkeypatch):
    monkeypatch.setenv("PROFILE_OWNER_IDS", str(OWNER))
    monkeypatch.setenv("PROFILE_MEMBER_IDS", str(MEMBER))
    bot.TOPIC_BINDING.clear()
    bot.TOPIC_INSTANCE.clear()
    bot._PENDING_ROUTE.clear()
    monkeypatch.setattr(bot, "_save_state", lambda: None)
    idx = ir.node_index([SimpleNamespace(instance_id="acme", graph_id="hubOfferte"),
                         SimpleNamespace(instance_id="personal", graph_id="personalChef")])
    monkeypatch.setattr(bot, "_NODE_INDEX", idx)
    monkeypatch.setattr(bot, "_ROUTING_INDEX", dict(idx))
    monkeypatch.setattr(bot, "_CHAT_INDEX", instance_acl.ChatIndex())
    progetti = {"hubOfferte": Project("hubOfferte", "Hub Offerte", "", "", "", aliases=("offerte",)),
                "personalChef": Project("personalChef", "Chef", "", "", "", aliases=("chef",))}
    monkeypatch.setattr(bot, "resolve_project", progetti.get)
    monkeypatch.setattr(bot, "list_projects", lambda: list(progetti.values()))
    import instances
    monkeypatch.setattr(instances, "known_ids", lambda env=None: ["personal", "acme"])
    yield
    bot.TOPIC_BINDING.clear()
    bot.TOPIC_INSTANCE.clear()


class _Msg:
    def __init__(self, text):
        self.text, self.replies = text, []
        self.message_thread_id = None

    async def reply_text(self, text, **k):
        self.replies.append((text, k.get("reply_markup")))


def _upd(uid, text, chat_id=5):
    return SimpleNamespace(effective_user=SimpleNamespace(id=uid),
                           effective_chat=SimpleNamespace(id=chat_id, type="private"),
                           effective_message=_Msg(text))


def test_il_prefisso_lega_la_dm_al_nodo(ambiente):
    u = _upd(OWNER, "personal/chef: cena?")
    stop = asyncio.run(bot._route_dm(u, bot.profile_for(OWNER), u.effective_message.text))
    assert stop is False and bot.TOPIC_BINDING[(5, 0)][0] == "personalChef"


def test_member_non_vede_acme_fra_le_istanze(ambiente):
    assert bot._visible_instances(bot.profile_for(MEMBER)) == ["personal"]
    u = _upd(MEMBER, "acme/offerte: x")
    stop = asyncio.run(bot._route_dm(u, bot.profile_for(MEMBER), u.effective_message.text))
    assert stop and "Non hai accesso" in u.effective_message.replies[0][0]


def test_ambiguita_parcheggia_e_chiede_con_i_pulsanti(ambiente):
    u = _upd(OWNER, "ciao")
    assert asyncio.run(bot._route_dm(u, bot.profile_for(OWNER), "ciao"))
    testo, tastiera = u.effective_message.replies[0]
    assert testo == "In quale istanza?"
    dati = [b.callback_data for r in tastiera.inline_keyboard for b in r]
    assert dati == ["dmr:i:personal", "dmr:i:acme"]
    assert (5, 0) in bot._PENDING_ROUTE
