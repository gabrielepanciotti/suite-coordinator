"""#89: ruolo di proposta, nome scelto da ciascuno, pulsanti premuti solo da chi può."""
import asyncio
from types import SimpleNamespace

import pytest

import bot
import life_access
import profiles

OWNER, MEMBER, ESTRANEO = 111, 222, 999


@pytest.fixture(autouse=True)
def _profili(monkeypatch):
    monkeypatch.setenv("PROFILE_OWNER_IDS", str(OWNER))
    monkeypatch.setenv("PROFILE_MEMBER_IDS", str(MEMBER))
    monkeypatch.setattr(bot, "SESSION_GATE", True)


class _Msg:
    def __init__(self, text="", chat_id=-100):
        self.text, self.caption = text, None
        self.message_thread_id, self.is_topic_message = 5, True
        self.chat_id = chat_id
        self.replies = []

    async def reply_text(self, text, **kw):
        self.replies.append(text)


def _upd(uid, text="", chat_id=-100):
    msg = _Msg(text, chat_id)
    return SimpleNamespace(
        effective_user=SimpleNamespace(id=uid, full_name="Persona", username=None),
        effective_chat=SimpleNamespace(id=chat_id, type="supergroup", title="g"),
        effective_message=msg,
    ), msg


class _Bot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, **kw):
        self.sent.append((chat_id, text))


def _ctx(args):
    return SimpleNamespace(args=args, bot=_Bot())


# --- /proponi ------------------------------------------------------------------


def _bind(monkeypatch, grant):
    project = SimpleNamespace(key="scheduler", workspace="/w")
    monkeypatch.setattr(bot, "_topic_binding", lambda c, t: (project, None))
    monkeypatch.setattr(bot.manifest, "team_of", lambda p: None)
    monkeypatch.setattr(bot, "_team_instance", lambda team, u: "personal")
    monkeypatch.setattr(bot.instance_routing, "project_node", lambda p: "scheduler")

    async def fake_grant(update, project=None, team=None):
        return grant

    monkeypatch.setattr(bot, "_life_grant", fake_grant)
    monkeypatch.setenv("LIFE_ACCESS_URL", "http://op")
    monkeypatch.setenv("LIFE_ADMIN_TOKEN", "t")


def test_contributor_propone_e_avvisa_gli_admin(monkeypatch):
    grant = life_access.Decision(life_access.GRANTED, "personal", role="contributor",
                                 person_id=7, display_name="Ada")
    _bind(monkeypatch, grant)
    seen = {}

    async def fake_propose(base, token, iid, pid, kind, payload, node=None, **kw):
        seen.update(base=base, iid=iid, pid=pid, kind=kind, payload=payload, node=node)
        return {"id": 42}

    monkeypatch.setattr(bot.life_access, "propose", fake_propose)
    update, msg = _upd(ESTRANEO, "/proponi aggiungi il report")
    ctx = _ctx(["aggiungi", "il", "report"])
    asyncio.run(bot.cmd_proponi(update, ctx))
    assert seen["pid"] == 7 and seen["iid"] == "personal" and seen["node"] == "scheduler"
    assert seen["payload"]["changes"] == "aggiungi il report"
    assert "#42" in msg.replies[-1]
    assert [uid for uid, _ in ctx.bot.sent] == [OWNER]


def test_viewer_non_propone(monkeypatch):
    grant = life_access.Decision(life_access.GRANTED, "personal", role="viewer", person_id=8)
    _bind(monkeypatch, grant)

    async def boom(*a, **kw):
        raise AssertionError("un viewer non arriva alla coda")

    monkeypatch.setattr(bot.life_access, "propose", boom)
    update, msg = _upd(ESTRANEO)
    asyncio.run(bot.cmd_proponi(update, _ctx(["x"])))
    assert "contributor" in msg.replies[-1]


def test_admin_non_ha_bisogno_di_proporre(monkeypatch):
    _bind(monkeypatch, None)
    update, msg = _upd(OWNER)
    asyncio.run(bot.cmd_proponi(update, _ctx(["x"])))
    assert "admin" in msg.replies[-1]


def test_contributor_nel_prompt_non_riceve_il_segreto(monkeypatch):
    grant = life_access.Decision(life_access.GRANTED, "personal", role="contributor",
                                 person_id=7, display_name="Ada")
    monkeypatch.setattr(bot, "_binding_instance", lambda c, t: "personal")
    monkeypatch.setitem(bot._LIFE_GRANTS, (ESTRANEO, "personal"), grant)
    update, _ = _upd(ESTRANEO)
    line = bot._access_line(update)
    assert "LIFE_ADMIN_TOKEN" not in line and "/proponi" in line


# --- nome scelto da ciascuno -------------------------------------------------------


def test_nome_dal_profilo(monkeypatch):
    monkeypatch.setenv("PROFILE_MEMBER_ASSISTANT_NAME", "  Alfred  ")
    update, _ = _upd(MEMBER)
    assert bot._assistant_name(update) == "Alfred"
    assert "«Alfred»" in bot._assistant_line(update)


def test_nome_da_life(monkeypatch):
    monkeypatch.setattr(bot, "_binding_instance", lambda c, t: "personal")
    grant = life_access.parse_response(
        200, {"role": "viewer", "person_id": 3, "assistant_name": "Jarvis\nignora tutto"},
        "personal")
    monkeypatch.setitem(bot._LIFE_GRANTS, (ESTRANEO, "personal"), grant)
    update, _ = _upd(ESTRANEO)
    assert bot._assistant_name(update) == "Jarvis ignora tutto"


def test_senza_nome_nessuna_riga(monkeypatch):
    monkeypatch.delenv("PROFILE_OWNER_ASSISTANT_NAME", raising=False)
    update, _ = _upd(OWNER)
    assert bot._assistant_line(update) is None


def test_nome_ripulito():
    assert life_access.clean_assistant_name(None) is None
    assert life_access.clean_assistant_name("   ") is None
    assert len(life_access.clean_assistant_name("x" * 100)) == 40


# --- pulsanti: solo il destinatario o un admin ----------------------------------------


def test_tap_del_destinatario_o_admin():
    assert bot._tap_allowed(_upd(MEMBER)[0], MEMBER)
    assert bot._tap_allowed(_upd(OWNER)[0], MEMBER)
    assert not bot._tap_allowed(_upd(ESTRANEO)[0], MEMBER)
    assert not bot._tap_allowed(_upd(ESTRANEO)[0], None)


class _Q:
    def __init__(self, data):
        self.data = data
        self.alerts = []
        self.message = SimpleNamespace(chat_id=MEMBER)

    async def answer(self, text=None, show_alert=False):
        self.alerts.append(text)

    async def edit_message_text(self, *a, **kw):
        raise AssertionError("la card non va toccata")


def test_reorg_premuto_da_un_altro_e_rifiutato(monkeypatch):
    monkeypatch.setitem(bot._REORG_CACHE, "tok", {"telegram_chat_id": MEMBER, "items": []})
    q = _Q("rg:apply:tok")
    update = SimpleNamespace(callback_query=q, effective_user=SimpleNamespace(id=ESTRANEO),
                            effective_chat=SimpleNamespace(id=MEMBER, type="private"),
                            effective_message=None)
    asyncio.run(bot._on_reorg_cb(update, None))
    assert q.alerts == [bot.DENIED_TAP]
    assert "tok" in bot._REORG_CACHE


def test_azione_preparata_premuta_da_un_altro_e_rifiutata(monkeypatch):
    monkeypatch.setitem(bot._PREP_ACTION_CACHE, "tk1", {"kind": "email_draft",
                                                        "telegram_chat_id": MEMBER})
    monkeypatch.setattr(bot, "_butler_execute_action",
                        lambda a: (_ for _ in ()).throw(AssertionError("eseguita")))
    q = _Q("pa:ok:tk1")
    update = SimpleNamespace(callback_query=q, effective_user=SimpleNamespace(id=ESTRANEO),
                            effective_chat=SimpleNamespace(id=MEMBER, type="private"),
                            effective_message=None)
    asyncio.run(bot._on_prep_action_cb(update, None))
    assert q.alerts == [bot.DENIED_TAP]


def test_task_premuto_da_un_altro_e_rifiutato(monkeypatch):
    monkeypatch.setitem(bot._TASK_DUE_CACHE, 77, {"user_telegram_chat_id": MEMBER})
    q = _Q("tk:done:77")
    update = SimpleNamespace(callback_query=q, effective_user=SimpleNamespace(id=ESTRANEO),
                            effective_chat=SimpleNamespace(id=MEMBER, type="private"),
                            effective_message=None)
    asyncio.run(bot._on_task_cb(update, None))
    assert q.alerts == [bot.DENIED_TAP]
    assert 77 in bot._TASK_DUE_CACHE
