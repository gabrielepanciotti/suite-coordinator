from datetime import datetime, timezone

import httpx

import sentinella
import sentinella_config
import sentinella_giro
import sentinella_store
from sentinella_config import Servizio
from sentinella_probes import ProbeResult

T0 = datetime(2026, 8, 17, 10, 0, tzinfo=timezone.utc)

SERVIZI = [
    Servizio(chiave="portale", titolo="Portale Segnalazioni", dominio="segnalatori.example.com",
             url_salute="http://x/api/health", container=("portale_db_prod",),
             project_dir="X:/srv/PortaleWeb_Segnalatori"),
    Servizio(chiave="offerte", titolo="Offerte pubbliche", dominio="offerte.example.com",
             url_salute="http://y/health", container=("offerte-pubbliche-bff-1",),
             project_dir="X:/srv/offerte-pubbliche"),
]


def _prepara(monkeypatch, tmp_path, sonde):
    monkeypatch.setattr(sentinella_store, "STATO_PATH", str(tmp_path / "stato.json"))
    monkeypatch.setattr(sentinella_config, "carica", lambda path=None: SERVIZI)
    monkeypatch.setattr(sentinella_giro, "_sonde_di", sonde)


def _sonde(esito, motivo="", causa="servizio", mancanti=()):
    async def _f(servizio, *, client=None, runner=None, adesso=None):
        return ProbeResult(esito, motivo), list(mancanti), causa
    return _f


def _client():
    return httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200)))


async def test_tutto_ok_non_dice_niente(monkeypatch, tmp_path):
    """Un canale che parla anche quando va tutto bene non viene letto."""
    _prepara(monkeypatch, tmp_path, _sonde(sentinella.OK))
    async with _client() as c:
        assert await sentinella_giro.giro(T0, client=c) is None


async def test_secondo_fallimento_produce_il_messaggio(monkeypatch, tmp_path):
    _prepara(monkeypatch, tmp_path,
             _sonde(sentinella.GIU, "motore Docker non risponde", causa="box"))
    async with _client() as c:
        assert await sentinella_giro.giro(T0, client=c) is None      # primo giro: rumore
        testo = await sentinella_giro.giro(T0, client=c)             # secondo: allarme
    assert testo is not None
    assert "🔴" in testo
    assert "motore Docker non risponde" in testo


async def test_il_guasto_del_box_produce_un_messaggio_solo(monkeypatch, tmp_path):
    _prepara(monkeypatch, tmp_path,
             _sonde(sentinella.GIU, "motore Docker non risponde", causa="box"))
    async with _client() as c:
        await sentinella_giro.giro(T0, client=c)
        testo = await sentinella_giro.giro(T0, client=c)
    assert testo.count("🔴") == 1
    assert "2 servizi" in testo


async def test_lo_stato_viene_persistito(monkeypatch, tmp_path):
    _prepara(monkeypatch, tmp_path, _sonde(sentinella.GIU, "502"))
    async with _client() as c:
        await sentinella_giro.giro(T0, client=c)
    assert sentinella_store.carica()["portale"].fallimenti == 1


async def test_riepilogo_parla_anche_quando_va_tutto_bene(monkeypatch, tmp_path):
    """Se una mattina il riepilogo non arriva, e' esso stesso un segnale."""
    _prepara(monkeypatch, tmp_path, _sonde(sentinella.OK))
    async with _client() as c:
        testo = await sentinella_giro.giro(T0, client=c, modo="riepilogo")
    assert testo is not None
    assert "2 servizi" in testo


async def test_una_riparazione_non_e_mai_silenziosa(monkeypatch, tmp_path):
    """Anche se il guasto non raggiunge la soglia e il riavvio riesce, si parla:
    una riparazione muta nasconde un guasto che si ripete."""
    _prepara(monkeypatch, tmp_path,
             _sonde(sentinella.GIU, "container assenti: portale_db_prod",
                    mancanti=("portale_db_prod",)))

    async def runner(comando):
        return 0, ""

    async with _client() as c:
        testo = await sentinella_giro.giro(T0, client=c, runner=runner)   # primo giro

    assert testo is not None
    assert "🔧" in testo
    assert "riavviato" in testo


async def test_riparazione_tentata_finisce_nel_messaggio(monkeypatch, tmp_path):
    _prepara(monkeypatch, tmp_path,
             _sonde(sentinella.GIU, "container assenti: portale_db_prod",
                    mancanti=("portale_db_prod",)))

    async def runner(comando):
        return 0, ""

    async with _client() as c:
        await sentinella_giro.giro(T0, client=c, runner=runner)
        testo = await sentinella_giro.giro(T0, client=c, runner=runner)
    assert "riavvio tentato" in testo
