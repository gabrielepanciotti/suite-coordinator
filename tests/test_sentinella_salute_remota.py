import sentinella
import sentinella_probes


def runner_finto(uscita: int, testo: str):
    async def _r(comando: str):
        return uscita, testo
    return _r


async def test_200_dal_box_e_ok():
    res = await sentinella_probes.sonda_http_remota("curl ...", runner=runner_finto(0, "200"))
    assert res.esito == sentinella.OK


async def test_502_dal_box_e_giu():
    res = await sentinella_probes.sonda_http_remota("curl ...", runner=runner_finto(0, "502"))
    assert res.esito == sentinella.GIU
    assert "502" in res.motivo


async def test_nessuna_risposta_e_giu():
    """Il servizio non ascolta: e' giu', non 'non lo so'."""
    res = await sentinella_probes.sonda_http_remota("curl ...", runner=runner_finto(0, "000"))
    assert res.esito == sentinella.GIU


async def test_box_irraggiungibile_e_sconosciuto():
    res = await sentinella_probes.sonda_http_remota(
        "curl ...", runner=runner_finto(255, "timeout"))
    assert res.esito == sentinella.SCONOSCIUTO


async def test_uscita_non_numerica_e_sconosciuto():
    res = await sentinella_probes.sonda_http_remota(
        "curl ...", runner=runner_finto(0, "boh"))
    assert res.esito == sentinella.SCONOSCIUTO
