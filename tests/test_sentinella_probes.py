import httpx

import sentinella
import sentinella_probes


def _client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_http_200_e_ok():
    async with _client(lambda r: httpx.Response(200, json={"status": "healthy"})) as c:
        res = await sentinella_probes.sonda_http("http://x/health", client=c)
    assert res.esito == sentinella.OK


async def test_http_502_e_giu():
    async with _client(lambda r: httpx.Response(502)) as c:
        res = await sentinella_probes.sonda_http("http://x/health", client=c)
    assert res.esito == sentinella.GIU
    assert "502" in res.motivo


async def test_errore_di_rete_e_sconosciuto_non_ok():
    """Una sonda rotta non e' salute: mai interpretare un errore come ok."""
    def boom(request):
        raise httpx.ConnectError("connessione rifiutata")

    async with _client(boom) as c:
        res = await sentinella_probes.sonda_http("http://x/health", client=c)
    assert res.esito == sentinella.SCONOSCIUTO


async def test_backend_sano_ma_db_scollegato_e_giu():
    """HTTP 200 con database scollegato: l'app risponde e non funziona."""
    payload = {"status": "healthy", "database": {"connected": False, "error": "timeout"}}
    async with _client(lambda r: httpx.Response(200, json=payload)) as c:
        res = await sentinella_probes.sonda_http("http://x/api/health", client=c)
    assert res.esito == sentinella.GIU
    assert "database" in res.motivo


async def test_risposta_non_json_resta_ok():
    async with _client(lambda r: httpx.Response(200, text="<html>ciao</html>")) as c:
        res = await sentinella_probes.sonda_http("http://x/", client=c)
    assert res.esito == sentinella.OK


async def test_ping_deadman_riuscito():
    async with _client(lambda r: httpx.Response(200)) as c:
        assert await sentinella_probes.ping_deadman("http://hc/ping/abc", client=c) is True


async def test_ping_deadman_senza_url_non_esplode():
    async with _client(lambda r: httpx.Response(200)) as c:
        assert await sentinella_probes.ping_deadman("", client=c) is False
