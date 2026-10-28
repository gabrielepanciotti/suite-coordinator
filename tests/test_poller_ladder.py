"""#83 F4: una scala sola per il poller del bot unico, e il lease fra device."""

import asyncio
import json
from dataclasses import replace

import pytest

import ownership
import poller_ladder as pl

WORK = ["pg1-vus-rmdevsrv", "INTEGRA", "NB-FO-GABPAN"]
KNOWN = WORK + ["minipc"]


def test_l_intersezione_delle_istanze_che_restringono():
    cfgs = {"acme": {"pollers": WORK}, "energypass": {"pollers": WORK[::-1]}, "personal": {}}
    ladder, problems = pl.poller_ladder(cfgs, KNOWN)
    # acme viene prima in ordine alfabetico: è la sua lista a dare l'ordine.
    assert ladder == WORK and problems == []


def test_personal_non_restringe_e_un_device_personale_resta_fuori():
    cfgs = {"acme": {"pollers": WORK}, "personal": {"pollers": ["minipc", *WORK]}}
    ladder, _ = pl.poller_ladder(cfgs, KNOWN)
    assert "minipc" not in ladder and ladder == WORK


def test_nessuna_istanza_che_restringe_vuol_dire_priorita_di_oggi():
    assert pl.poller_ladder({"personal": {}}, KNOWN) == (None, [])


def test_un_device_sconosciuto_si_salta_e_si_dice():
    ladder, problems = pl.poller_ladder({"acme": {"pollers": ["pg1-vus-rmdevsrv", "boh"]}}, KNOWN)
    assert ladder == ["pg1-vus-rmdevsrv"] and "boh" in problems[0]


def test_priorita_e_peer_dalla_scala(monkeypatch):
    assert pl.priority_for("pg1-vus-rmdevsrv", WORK) == 30
    assert pl.priority_for("nb-fo-gabpan", WORK) == 10
    assert pl.priority_for("minipc", WORK) is None
    env = {"OWNERSHIP_PEER_INTEGRA": "http://integra:8013/", "OWNERSHIP_PEER_NB_FO_GABPAN": ""}
    assert pl.peers_of("pg1-vus-rmdevsrv", WORK, env) == ["http://integra:8013"]


def test_load_config_dalle_istanze(monkeypatch, tmp_path):
    monkeypatch.setattr(ownership, "_instance_ladder", lambda: (WORK, [], True))
    monkeypatch.setenv("LIFE_DEVICE", "INTEGRA")
    monkeypatch.setenv("OWNERSHIP_PEER_PG1_VUS_RMDEVSRV", "http://devlinux:8013")
    monkeypatch.delenv("PEER_OWNERSHIP_URL", raising=False)
    cfg = ownership.load_config()
    assert (cfg.priority, cfg.eligible, cfg.peers, cfg.ladder) == (20, True, ("http://devlinux:8013",), tuple(WORK))
    monkeypatch.setenv("LIFE_DEVICE", "minipc")
    cfg = ownership.load_config()
    assert not cfg.eligible and "minipc" in cfg.reason


# --- la macchina a stati con il lease ---------------------------------------------------


class Box:
    """One device: a manager whose peers are the other boxes' /ownership."""

    def __init__(self, tmp_path, device, priority, *, eligible=True):
        self.calls, self.peers, self.now = [], [], 1000.0
        cfg = ownership.OwnershipConfig(device=device, priority=priority, port=0,
                                        peer_url="http://x", probe_interval=15,
                                        fail_threshold=3, state_dir=str(tmp_path),
                                        eligible=eligible, reason="no" if not eligible else "")

        async def acquire():
            self.calls.append("acquire")

        async def release():
            self.calls.append("release")

        async def probe():
            return [p.mgr.status() if p.up else None for p in self.peers]

        self.up = True
        self.mgr = ownership.OwnershipManager(cfg, acquire=acquire, release=release, probe=probe)
        self.mgr._clock = lambda: self.now

    def tick(self, n=1):
        for _ in range(n):
            asyncio.run(self.mgr.tick())


def _rete(tmp_path):
    a = Box(tmp_path / "a", "pg1-vus-rmdevsrv", 30)
    b = Box(tmp_path / "b", "INTEGRA", 20)
    a.peers, b.peers = [b], [a]
    return a, b


def test_il_primo_della_scala_tiene_il_polling(tmp_path):
    a, b = _rete(tmp_path)
    a.tick(); b.tick()
    assert a.mgr.is_polling and not b.mgr.is_polling
    st = a.mgr.status()["lease"]
    assert st["holder"] == "pg1-vus-rmdevsrv" and st["epoch"] == 1 and st["until"] == 1045.0


def test_se_cade_il_primo_lo_prende_il_secondo_e_poi_lo_restituisce(tmp_path):
    a, b = _rete(tmp_path)
    a.tick(); b.tick()
    a.up = False
    b.tick(2)
    assert not b.mgr.is_polling          # isteresi: due probe mancate non bastano
    b.tick()
    assert b.mgr.is_polling and b.mgr.epoch == 2
    a.up = True
    b.tick()                              # il più alto è tornato e lo vuole
    assert not b.mgr.is_polling
    a.tick()
    assert a.mgr.is_polling and a.mgr.epoch == 3


def test_un_detentore_stantio_cede_a_un_epoch_piu_nuovo(tmp_path):
    a, b = _rete(tmp_path)
    a.mgr.is_polling, a.mgr.epoch = True, 1       # si sveglia credendo di tenerlo
    b.mgr.is_polling, b.mgr.epoch = True, 5
    b.mgr.lease_until = 2000.0
    a.peers = [b]
    a.tick()
    assert a.calls == ["release"] and not a.mgr.is_polling


def test_un_device_non_ammesso_non_polla_mai(tmp_path):
    m = Box(tmp_path, "minipc", -1, eligible=False)
    m.peers = []
    m.tick(5)
    assert m.calls == [] and m.mgr.status()["wants_ownership"] is False
    asyncio.run(m.mgr.apply_handoff("minipc"))
    m.tick()
    assert m.calls == []


def test_l_epoch_sopravvive_al_riavvio(tmp_path):
    a, b = _rete(tmp_path)
    a.tick()
    raw = json.loads((tmp_path / "a" / "ownership-pg1-vus-rmdevsrv.json").read_text())
    assert raw["epoch"] == 1
    again = Box(tmp_path / "a", "pg1-vus-rmdevsrv", 30)
    assert again.mgr.epoch == 1


# --- fail-safe: il token del bot unico senza scala non si polla (#83 F5) -----------


def _registry(tmp_path, monkeypatch, *, mounted):
    import json as _json
    inst = tmp_path / "life-acme"
    if mounted:
        inst.mkdir()
        (inst / "instance.config.json").write_text(
            _json.dumps({"pollers": ["pg1-vus-rmdevsrv", "INTEGRA"]}), encoding="utf-8")
    home = tmp_path / "life"
    home.mkdir()
    (home / "instances.json").write_text(_json.dumps({"version": 1, "instances": {
        "acme": {"path": str(inst), "profile": "work", "band": 1}}}), encoding="utf-8")
    monkeypatch.setenv("LIFE_HOME", str(home))
    monkeypatch.setenv("OWNERSHIP_LADDER", "auto")


def test_istanza_non_montata_scala_incompleta(tmp_path, monkeypatch):
    _registry(tmp_path, monkeypatch, mounted=False)
    found, problems, complete = ownership._instance_ladder()
    assert found is None and not complete and "non montato" in problems[0]


def test_token_del_bot_unico_senza_scala_non_polla_mai(tmp_path, monkeypatch):
    _registry(tmp_path, monkeypatch, mounted=False)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN_FILE", "/x/telegram-coordinator-bot.token")
    monkeypatch.setenv("COORDINATOR_ROLE", "bot")
    monkeypatch.setenv("LIFE_DEVICE", "INTEGRA")
    monkeypatch.setenv("OWNER_PRIORITY", "20")
    monkeypatch.delenv("PEER_OWNERSHIP_URL", raising=False)
    cfg = ownership.load_config()
    assert not cfg.eligible and cfg.priority == -1 and "non calcolabile" in cfg.reason
    # Even with no peers, ownership stays on (standby): never plain run_polling.
    assert cfg.enabled
    monkeypatch.setenv("PEER_OWNERSHIP_URL", "http://devlinux:8013")
    assert not ownership.load_config().eligible  # the .env patch cannot override it


def test_istanza_montata_scala_normale(tmp_path, monkeypatch):
    _registry(tmp_path, monkeypatch, mounted=True)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN_FILE", "/x/telegram-coordinator-bot.token")
    monkeypatch.setenv("LIFE_DEVICE", "INTEGRA")
    monkeypatch.setenv("OWNERSHIP_PEER_PG1_VUS_RMDEVSRV", "http://devlinux:8013")
    monkeypatch.delenv("PEER_OWNERSHIP_URL", raising=False)
    cfg = ownership.load_config()
    assert cfg.eligible and cfg.priority == 10 and cfg.peers == ("http://devlinux:8013",)


def test_token_proprio_senza_scala_resta_alle_priorita_env(tmp_path, monkeypatch):
    _registry(tmp_path, monkeypatch, mounted=False)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN_FILE", raising=False)
    monkeypatch.setenv("OWNER_PRIORITY", "7")
    cfg = ownership.load_config()
    assert cfg.eligible and cfg.priority == 7


def test_al_primo_409_chi_vede_un_epoch_piu_alto_cede_subito(tmp_path):
    # D3 (#83 F5): dev-linux comes back and takes the lease with a newer epoch;
    # integra used to wait for its next probe (and 2 Conflicts). Now the first
    # 409 makes it look at the peers at once and yield.
    a, b = _rete(tmp_path)
    a.up = False
    b.tick(3)
    assert b.mgr.is_polling
    a.up = True
    a.tick()
    assert a.mgr.is_polling and a.mgr.epoch > b.mgr.epoch
    asyncio.run(b.mgr.on_conflict())
    assert not b.mgr.is_polling and b.calls[-1] == "release"


def test_i_device_del_catalogo_sono_noti_anche_senza_nodi_nel_grafo(tmp_path, monkeypatch):
    # conv 720: the minipc hosts no node of any graph, but it is in the instance's
    # `devices` catalogue — it must stay in the ladder, second after dev-linux.
    import json as _json
    _registry(tmp_path, monkeypatch, mounted=True)
    inst = tmp_path / "life-acme"
    (inst / "instance.config.json").write_text(_json.dumps({
        "pollers": ["pg1-vus-rmdevsrv", "minipc", "NB-FO-GABPAN", "ghost"],
        "devices": {"pg1-vus-rmdevsrv": {}, "minipc": {}, "NB-FO-GABPAN": {}}}),
        encoding="utf-8")
    (inst / "life.graph.json").write_text(_json.dumps({"nodes": {
        "coordinator": {"paths": {"pg1-vus-rmdevsrv": "/x"}}}}), encoding="utf-8")
    found, problems, complete = ownership._instance_ladder()
    assert complete
    assert found == ["pg1-vus-rmdevsrv", "minipc", "NB-FO-GABPAN"]
    assert any("'ghost'" in p for p in problems)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN_FILE", "/x/telegram-coordinator-bot.token")
    monkeypatch.setenv("DEVICE_NAME", "minipc")
    cfg = ownership.load_config()
    assert cfg.eligible and cfg.priority == 20
