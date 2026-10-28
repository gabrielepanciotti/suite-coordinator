"""La sentinella deve saper distinguere «la prod e' giu'» da «sono cieca io».

Il 7 settembre 2026 tailscaled si e' fermato su dev-linux (uscita 0 dopo un
ENOBUFS sul TUN, quindi nessun restart automatico). Da quel momento ogni sonda
verso il box pubblico diceva "box irraggiungibile via ssh" — SCONOSCIUTO, che a valle
conta come fallimento — e la sentinella ha dichiarato giu' una produzione che
non e' mai caduta: la vedetta esterna sull'altro box prod ha continuato a leggere 200
per tutte le due ore e mezza.

Il costo non e' il messaggio sbagliato: e' che la persona lo ha creduto, e' andata
in RDP sul box sano e ha spento dei servizi che funzionavano.
"""
import pytest

import sentinella
import sentinella_probes as probes


class _Client:
    """httpx.AsyncClient finto: mappa url -> esito (codice o eccezione)."""

    def __init__(self, risposte):
        self.risposte = risposte
        self.visti = []

    async def get(self, url, **kw):
        self.visti.append(url)
        esito = self.risposte.get(url, 599)
        if isinstance(esito, Exception):
            raise esito
        return type("R", (), {"status_code": esito})()


TAILNET = "http://100.64.0.2:8006/health"
INTERNET = "https://www.google.com/generate_204"


@pytest.mark.asyncio
async def test_canale_ok_quando_il_riferimento_tailnet_risponde():
    client = _Client({TAILNET: 200})
    res = await probes.sonda_canale(
        client=client, url_tailnet=TAILNET, url_internet=INTERNET)
    assert res.esito == sentinella.OK
    # Internet non viene nemmeno interrogato: se la tailnet va, la domanda
    # "sono connesso al mondo?" non aggiunge nulla e costa un giro di rete.
    assert INTERNET not in client.visti


@pytest.mark.asyncio
async def test_canale_giu_quando_la_tailnet_tace_ma_internet_risponde():
    """Il caso del 7 settembre: rete pubblica viva, tailnet locale morta."""
    client = _Client({TAILNET: OSError("no route"), INTERNET: 204})
    res = await probes.sonda_canale(
        client=client, url_tailnet=TAILNET, url_internet=INTERNET)
    assert res.esito == sentinella.GIU
    assert "tailnet" in res.motivo.lower()


@pytest.mark.asyncio
async def test_canale_sconosciuto_se_cade_anche_internet():
    """Nemmeno la rete pubblica risponde: non si sa piu' di chi sia la colpa.

    Potrebbe essere la linea dell'ufficio, e in quel caso i box possono benissimo
    stare servendo i clienti. SCONOSCIUTO, non GIU: dire "tailnet giu'" sarebbe
    inventare una diagnosi che non abbiamo.
    """
    client = _Client({TAILNET: OSError("no route"), INTERNET: OSError("dns")})
    res = await probes.sonda_canale(
        client=client, url_tailnet=TAILNET, url_internet=INTERNET)
    assert res.esito == sentinella.SCONOSCIUTO


@pytest.mark.asyncio
async def test_canale_ok_se_non_configurato():
    """Nessun riferimento dichiarato: il gate non deve inventarsi una cecita'.

    Un controllo che, non configurato, blocca la sorveglianza sarebbe un modo
    nuovo di essere ciechi — quello che questo test impedisce.
    """
    client = _Client({})
    res = await probes.sonda_canale(client=client, url_tailnet="", url_internet=INTERNET)
    assert res.esito == sentinella.OK
    assert client.visti == []


# ---------------------------------------------------------------------------
# Il gate dentro il giro
# ---------------------------------------------------------------------------
from datetime import datetime, timezone  # noqa: E402

import httpx  # noqa: E402

import sentinella_config  # noqa: E402
import sentinella_giro  # noqa: E402
import sentinella_store  # noqa: E402
from sentinella_config import Servizio  # noqa: E402

T0 = datetime(2026, 9, 7, 13, 51, tzinfo=timezone.utc)

SERVIZI = [
    Servizio(chiave="portale", titolo="Portale Segnalazioni", dominio="segnalatori.example.com",
             url_salute="http://x/api/health", container=("portale_db_prod",)),
]


def _prepara(monkeypatch, tmp_path, canale):
    monkeypatch.setattr(sentinella_store, "STATO_PATH", str(tmp_path / "stato.json"))
    monkeypatch.setattr(sentinella_config, "carica", lambda path=None: SERVIZI)

    async def _canale(**kw):
        return canale
    monkeypatch.setattr(probes, "sonda_canale", _canale)

    chiamate = []

    async def _sonde(servizio, **kw):
        chiamate.append(servizio.chiave)
        return probes.ProbeResult(sentinella.SCONOSCIUTO, "box irraggiungibile via ssh"), [], "box"
    monkeypatch.setattr(sentinella_giro, "_sonde_di", _sonde)
    return chiamate


def _client():
    return httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200)))


@pytest.mark.asyncio
async def test_canale_giu_non_sonda_e_non_accusa_i_box(monkeypatch, tmp_path):
    """Il cuore del 7 settembre: cieca, non deve dire nulla SUI BOX.

    Le sonde non partono nemmeno — non perche' costino, ma perche' la loro
    risposta sarebbe priva di contenuto: da qui, con la tailnet giu', ogni box
    del mondo risulta irraggiungibile.
    """
    cieca = probes.ProbeResult(sentinella.GIU, "tailnet locale giu' (la rete pubblica risponde)")
    chiamate = _prepara(monkeypatch, tmp_path, cieca)

    async with _client() as c:
        assert await sentinella_giro.giro(T0, client=c) is None       # primo giro: rumore
        testo = await sentinella_giro.giro(T0, client=c)              # secondo: allarme

    assert chiamate == [], "con la tailnet giu' le sonde sui box non vanno eseguite"
    assert testo is not None
    assert "tailnet" in testo.lower()
    # Il difetto da cui nasce tutto: la prod dichiarata giu' mentre serviva 200.
    assert "Portale Segnalazioni" not in testo


@pytest.mark.asyncio
async def test_lo_stato_dei_servizi_non_si_sporca_mentre_e_cieca(monkeypatch, tmp_path):
    """Nessun fallimento accumulato sui servizi durante la cecita'.

    Se i contatori avanzassero, al ritorno della rete la sentinella troverebbe i
    servizi gia' oltre soglia e allarmerebbe su un guasto mai avvenuto —
    l'errore di oggi, soltanto differito.
    """
    cieca = probes.ProbeResult(sentinella.GIU, "tailnet locale giu'")
    _prepara(monkeypatch, tmp_path, cieca)
    async with _client() as c:
        await sentinella_giro.giro(T0, client=c)
        await sentinella_giro.giro(T0, client=c)

    stati = sentinella_store.carica()
    assert "portale" not in stati or stati["portale"].fallimenti == 0


@pytest.mark.asyncio
async def test_canale_ok_lascia_lavorare_le_sonde(monkeypatch, tmp_path):
    """Il gate non deve diventare un tappo: a canale sano si sorveglia come prima."""
    chiamate = _prepara(monkeypatch, tmp_path, probes.ProbeResult(sentinella.OK))
    async with _client() as c:
        await sentinella_giro.giro(T0, client=c)
    assert chiamate == ["portale"]


@pytest.mark.asyncio
async def test_il_messaggio_dice_che_e_la_sentinella_a_non_vedere(monkeypatch, tmp_path):
    """Chi legge alle tre di notte deve capire cosa NON sa, non cosa e' rotto.

    "🔴 Portale giu'" e "⚫ non vedo i box" chiedono due azioni opposte: la prima
    manda qualcuno a riavviare la produzione, la seconda a guardare la rete di
    qui. Il 7 settembre e' arrivata la prima, ed e' stata eseguita.
    """
    cieca = probes.ProbeResult(sentinella.GIU, "tailnet locale giu' (la rete pubblica risponde)")
    _prepara(monkeypatch, tmp_path, cieca)
    async with _client() as c:
        await sentinella_giro.giro(T0, client=c)
        testo = await sentinella_giro.giro(T0, client=c)

    assert testo.startswith("⚫")
    assert "non vedo" in testo.lower()
    assert "stato dei box sconosciuto" in testo.lower()
