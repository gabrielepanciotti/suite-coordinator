"""#82: «istanza <id>» nel menu del gruppo, scrittura del canale sull'host."""

import asyncio
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import instance_link

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import host_instance_link  # noqa: E402

LIFE = ROOT.parent / "Life"


def _registry(tmp_path, monkeypatch, istanze: dict) -> Path:
    home = tmp_path / "life-home"
    home.mkdir()
    reg = {"version": 1, "instances": {}}
    for band, (iid, channels) in enumerate(istanze.items(), start=1):
        path = tmp_path / f"life-{iid}"
        path.mkdir()
        (path / "instance.config.json").write_text(
            json.dumps({"profile": iid, "channels": channels}, indent=2) + "\n",
            encoding="utf-8",
        )
        reg["instances"][iid] = {"path": str(path), "profile": iid, "band": band}
    (home / "instances.json").write_text(json.dumps(reg), encoding="utf-8")
    monkeypatch.setenv("LIFE_HOME", str(home))
    return home


# --- il menu ---------------------------------------------------------------------


def test_il_menu_offre_solo_le_istanze_senza_gruppo(tmp_path, monkeypatch):
    _registry(tmp_path, monkeypatch, {
        "energypass": {"bus": {"tenant": "life-energypass"}, "telegram": {}},
        "acme": {"telegram": {"chatId": -100123, "agent": "owner-telegram"}},
    })
    assert instance_link.menu_entries() == [("i:energypass", "\U0001f9e9 istanza energypass")]


def test_registro_rotto_non_offre_niente_e_non_esplode(tmp_path, monkeypatch):
    home = tmp_path / "h"
    home.mkdir()
    (home / "instances.json").write_text("{rotto", encoding="utf-8")
    monkeypatch.setenv("LIFE_HOME", str(home))
    assert instance_link.unlinked() == []


def test_le_istanze_entrano_in_coda_ai_progetti_legabili(tmp_path, monkeypatch):
    import bot

    _registry(tmp_path, monkeypatch, {"energypass": {"telegram": {}}})
    chiavi = [k for k, _ in bot._progetti_legabili()]
    # #83 F1: prima l'istanza, il forum da collegare sta un tocco sotto.
    assert "in:energypass" in chiavi
    assert len(chiavi) > 1  # i progetti senza istanza restano
    tasti = [b.callback_data for r in bot._menu_istanza("energypass").inline_keyboard for b in r]
    assert tasti[0] == "gb:i:energypass"


# --- la scrittura sull'host ------------------------------------------------------


def _git_repo(path: Path, bare: Path) -> None:
    run = lambda *a: subprocess.run(a, cwd=path, check=True, capture_output=True)  # noqa: E731
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
    run("git", "init", "-q", "-b", "main")
    run("git", "config", "user.email", "t@t")
    run("git", "config", "user.name", "t")
    run("git", "add", ".")
    run("git", "commit", "-qm", "init")
    run("git", "remote", "add", "origin", str(bare))
    run("git", "push", "-qu", "origin", "main")


@pytest.mark.skipif(not (LIFE / "packages/shared/suite_core/instances/connect.py").is_file(),
                    reason="checkout di Life assente")
def test_l_host_scrive_con_write_channel_committa_e_pusha(tmp_path, monkeypatch):
    home = _registry(tmp_path, monkeypatch, {
        "energypass": {"bus": {"tenant": "life-energypass"}, "telegram": {}}})
    path = tmp_path / "life-energypass"
    (path / "altro.txt").write_text("x")
    _git_repo(path, tmp_path / "remote.git")
    (path / "altro.txt").write_text("lavoro di qualcun altro")  # dirty, non va committato

    esito = host_instance_link.link_telegram(
        {"op": "instance-telegram", "instance": "energypass", "chatId": -1009,
         "agent": "owner-telegram"},
        env={"LIFE_HOME": str(home), "LIFE_FRAMEWORK_ROOT": str(LIFE)},
    )

    assert esito["ok"] and esito["pushed"], esito
    cfg = json.loads((path / "instance.config.json").read_text())
    assert cfg["channels"]["telegram"] == {"chatId": -1009, "agent": "owner-telegram"}
    assert cfg["channels"]["bus"] == {"tenant": "life-energypass"}  # il resto intatto
    remoto = subprocess.run(["git", "--git-dir", str(tmp_path / "remote.git"), "show",
                             "main:instance.config.json"], capture_output=True, text=True)
    assert '"chatId": -1009' in remoto.stdout
    stato = subprocess.run(["git", "status", "--short"], cwd=path, capture_output=True, text=True)
    assert stato.stdout.strip() == "M altro.txt"


def test_l_host_non_sovrascrive_un_gruppo_diverso(tmp_path, monkeypatch):
    home = _registry(tmp_path, monkeypatch, {"acme": {"telegram": {"chatId": -100123}}})
    scritti = []
    esito = host_instance_link.link_telegram(
        {"instance": "acme", "chatId": -1009, "agent": "a"},
        env={"LIFE_HOME": str(home)}, writer=lambda *a: scritti.append(a),
        git=lambda *a: (0, ""),
    )
    assert not esito["ok"] and "gia' collegata" in esito["detail"]
    assert scritti == []


def test_l_host_rifiuta_un_gruppo_gia_di_un_altra_istanza(tmp_path, monkeypatch):
    # #83 F0: -1003899308208 era finito sia in acme sia in personal.
    home = _registry(tmp_path, monkeypatch, {
        "acme": {"telegram": {"chatId": -100777}},
        "personal": {"telegram": {}},
    })
    scritti = []
    esito = host_instance_link.link_telegram(
        {"instance": "personal", "chatId": -100777, "agent": "a"},
        env={"LIFE_HOME": str(home)}, writer=lambda *a: scritti.append(a),
        git=lambda *a: (0, ""),
    )
    assert not esito["ok"] and "istanza acme" in esito["detail"]
    assert scritti == []


@pytest.mark.parametrize("req,motivo", [
    ({"instance": "../etc", "chatId": -1, "agent": "a"}, "instance"),
    ({"instance": "x", "chatId": 5, "agent": "a"}, "chatId"),
    ({"instance": "x", "chatId": True, "agent": "a"}, "chatId"),
    ({"instance": "x", "chatId": -1, "agent": "a b; rm"}, "agent"),
])
def test_l_host_rifiuta_richieste_fuori_forma(req, motivo):
    esito = host_instance_link.link_telegram(req, env={}, git=lambda *a: (0, ""))
    assert not esito["ok"] and motivo in esito["detail"]


def test_istanza_non_registrata_sull_host(tmp_path, monkeypatch):
    home = _registry(tmp_path, monkeypatch, {})
    esito = host_instance_link.link_telegram(
        {"instance": "fantasma", "chatId": -1, "agent": "a"},
        env={"LIFE_HOME": str(home)}, git=lambda *a: (0, ""))
    assert not esito["ok"] and "non registrata" in esito["detail"]


def test_il_wrapper_forzato_instrada_l_op_senza_lanciare_claude(tmp_path, monkeypatch):
    home = _registry(tmp_path, monkeypatch, {})
    req = json.dumps({"op": "instance-telegram", "instance": "fantasma", "chatId": -1,
                      "agent": "a"})
    out = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "host_claude_exec.py")],
        input=req + "\n", capture_output=True, text=True, timeout=30,
        env={"LIFE_HOME": str(home), "PATH": "/usr/bin:/bin",
             "HOST_EXEC_CLAUDE_BIN": "/bin/false"},
    )
    assert out.returncode == 0
    assert json.loads(out.stdout.strip().splitlines()[-1])["ok"] is False


# --- il client nel container e la callback ---------------------------------------


def test_la_risposta_dell_host_si_legge_anche_dopo_rumore():
    out = b"motd\n" + json.dumps({"ok": True, "written": True, "pushed": True,
                                   "detail": "life-x abc123"}).encode() + b"\n"
    assert instance_link.parse_reply(out, b"", 0) == instance_link.LinkResult(
        True, True, True, "life-x abc123")
    assert not instance_link.parse_reply(b"", b"Permission denied", 255).ok


def test_senza_host_exec_si_dice_e_non_si_scrive(monkeypatch):
    esito = asyncio.run(instance_link.link_telegram("x", -1, "a"))
    assert not esito.ok and "HOST_EXEC_SSH" in esito.detail


class _Q:
    def __init__(self, data, chat_id):
        self.data = data
        self.message = SimpleNamespace(chat=SimpleNamespace(id=chat_id), text="")
        self.from_user = SimpleNamespace(id=4242)
        self.answers, self.edits = [], []

    async def answer(self, *a, **k):
        self.answers.append(a)

    async def edit_message_text(self, text, **k):
        self.edits.append(text)


@pytest.fixture(autouse=True)
def _admin(monkeypatch):
    # #83: legare un gruppo a un'istanza lo fa un admin o un membro di quell'istanza.
    monkeypatch.setenv("PROFILE_OWNER_IDS", "4242")


def test_chi_non_ha_un_profilo_non_lega(tmp_path, monkeypatch):
    import bot

    _registry(tmp_path, monkeypatch, {"energypass": {"telegram": {}}})

    async def mai(*a):
        raise AssertionError("non doveva scrivere")

    monkeypatch.setattr(instance_link, "link_telegram", mai)
    q = _Q("gb:i:energypass", -1009)
    q.from_user = SimpleNamespace(id=999)
    asyncio.run(bot.on_group_bind_cb(SimpleNamespace(callback_query=q), None))
    assert q.edits == [] and q.answers


def test_la_scelta_istanza_collega_il_gruppo(tmp_path, monkeypatch):
    import bot

    _registry(tmp_path, monkeypatch, {"energypass": {"telegram": {}}})
    chiamate = []

    async def finto(iid, chat, agent):
        chiamate.append((iid, chat, agent))
        return instance_link.LinkResult(True, True, True, "life-energypass abc1234")

    monkeypatch.setattr(instance_link, "link_telegram", finto)
    monkeypatch.setenv("HUB_TELEGRAM_AGENT", "owner-telegram")
    monkeypatch.setattr(bot, "_save_state", lambda: None)
    bot._GRUPPI_IN_ATTESA[-1009] = ["i:energypass"]
    q = _Q("gb:i:energypass", -1009)

    asyncio.run(bot.on_group_bind_cb(SimpleNamespace(callback_query=q), None))

    assert chiamate == [("energypass", -1009, "owner-telegram")]
    assert "canale Telegram dell'istanza" in q.edits[-1]
    assert -1009 not in bot._GRUPPI_IN_ATTESA


def test_un_istanza_gia_collegata_non_si_ricollega(tmp_path, monkeypatch):
    import bot

    _registry(tmp_path, monkeypatch, {"acme": {"telegram": {"chatId": -100123}}})

    async def mai(*a):
        raise AssertionError("non doveva scrivere")

    monkeypatch.setattr(instance_link, "link_telegram", mai)
    q = _Q("gb:i:acme", -1009)
    asyncio.run(bot.on_group_bind_cb(SimpleNamespace(callback_query=q), None))
    assert q.edits == [] and q.answers


def test_un_push_fallito_si_dice(tmp_path, monkeypatch):
    import bot

    _registry(tmp_path, monkeypatch, {"energypass": {"telegram": {}}})

    async def push_ko(*a):
        return instance_link.LinkResult(False, True, False, "push fallito: denied")

    monkeypatch.setattr(instance_link, "link_telegram", push_ko)
    q = _Q("gb:i:energypass", -1009)
    asyncio.run(bot.on_group_bind_cb(SimpleNamespace(callback_query=q), None))
    assert "scritto ma non pushato" in q.edits[-1] and "denied" in q.edits[-1]


# --- instance-idea (#78) -----------------------------------------------------------


def _idea(**over):
    req = {"op": "instance-idea", "instance": "comunity", "nodo": "ComUnity",
           "testo": "idea del team", "session": "telegram:-1003875691522:12",
           "marker": "vocale-telegram"}
    req.update(over)
    return req


def test_queue_idea_appends_to_the_registered_instance_and_publishes(tmp_path, monkeypatch):
    _registry(tmp_path, monkeypatch, {"acme": {}, "comunity": {}})
    pushed = []
    res = host_instance_link.queue_idea(
        _idea(), publish=lambda p, n: pushed.append((p, n)) or True
    )
    assert res == {"ok": True, "written": True, "pushed": True, "detail": "life-comunity"}
    rows = (tmp_path / "life-comunity" / ".brain-inbox" / "idee.jsonl").read_text().splitlines()
    row = json.loads(rows[0])
    assert (row["nodo"], row["testo"], row["marcatore"]) == (
        "ComUnity", "idea del team", "vocale-telegram"
    )
    assert not (tmp_path / "life-acme" / ".brain-inbox").exists()
    assert pushed == [(tmp_path / "life-comunity", "ComUnity")]


def test_queue_idea_failed_push_is_still_written(tmp_path, monkeypatch):
    _registry(tmp_path, monkeypatch, {"comunity": {}})
    res = host_instance_link.queue_idea(_idea(), publish=lambda p, n: False)
    assert (res["ok"], res["written"], res["pushed"]) == (True, True, False)


def test_queue_idea_unknown_instance_writes_nothing(tmp_path, monkeypatch):
    _registry(tmp_path, monkeypatch, {"acme": {}})
    res = host_instance_link.queue_idea(_idea(), publish=lambda p, n: True)
    assert res["ok"] is False and "non registrata" in res["detail"]


@pytest.mark.parametrize("over", [
    {"instance": "../etc"},
    {"nodo": "../../x"},
    {"nodo": ""},
    {"testo": "   "},
    {"testo": "x" * 9000},
    {"session": "telegram:1:2; rm -rf"},
    {"marker": "altro"},
])
def test_queue_idea_rejects_anything_outside_the_contract(tmp_path, monkeypatch, over):
    _registry(tmp_path, monkeypatch, {"comunity": {}})
    res = host_instance_link.queue_idea(_idea(**over), publish=lambda p, n: True)
    assert res["ok"] is False and res["written"] is False
    assert not (tmp_path / "life-comunity" / ".brain-inbox").exists()


def test_idea_request_is_one_json_line():
    raw = instance_link.idea_request("comunity", "ComUnity", "t", "telegram:-1:2",
                                     "testo-telegram")
    assert raw.endswith(b"\n") and json.loads(raw)["op"] == "instance-idea"
