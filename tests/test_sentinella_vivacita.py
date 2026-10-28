from datetime import datetime, timedelta, timezone

import sentinella
import sentinella_probes

T0 = datetime(2026, 8, 17, 10, 0, tzinfo=timezone.utc)


def runner_finto(uscita: int, testo: str):
    async def _r(comando: str):
        return uscita, testo
    return _r


async def test_scheduler_che_ha_lavorato_da_poco_e_ok():
    recente = (T0 - timedelta(minutes=5)).isoformat()
    res = await sentinella_probes.sonda_vivacita(
        "psql ...", 45, T0, runner=runner_finto(0, recente))
    assert res.esito == sentinella.OK


async def test_scheduler_zombie_viene_scoperto():
    """Processo su, job fermi: oggi e' completamente invisibile."""
    vecchio = (T0 - timedelta(hours=3)).isoformat()
    res = await sentinella_probes.sonda_vivacita(
        "psql ...", 45, T0, runner=runner_finto(0, vecchio))
    assert res.esito == sentinella.GIU
    assert "scheduler" in res.motivo.lower()


async def test_senza_comando_la_sonda_non_si_applica():
    res = await sentinella_probes.sonda_vivacita("", 45, T0, runner=runner_finto(0, ""))
    assert res.esito == sentinella.OK


async def test_timestamp_illeggibile_e_sconosciuto_non_giu():
    """Non sappiamo se lo scheduler e' fermo: non fingiamo di saperlo."""
    res = await sentinella_probes.sonda_vivacita(
        "psql ...", 45, T0, runner=runner_finto(0, "ciao"))
    assert res.esito == sentinella.SCONOSCIUTO


async def test_comando_fallito_e_sconosciuto():
    res = await sentinella_probes.sonda_vivacita(
        "psql ...", 45, T0, runner=runner_finto(1, "connection refused"))
    assert res.esito == sentinella.SCONOSCIUTO


async def test_disco_sotto_soglia_e_ok():
    res = await sentinella_probes.sonda_disco(85, runner=runner_finto(0, "62"))
    assert res.esito == sentinella.OK


async def test_disco_oltre_soglia_avvisa():
    res = await sentinella_probes.sonda_disco(85, runner=runner_finto(0, "91"))
    assert res.esito == sentinella.GIU
    assert "91" in res.motivo


async def test_avviso_ssh_prima_del_timestamp_non_falsa_lallarme():
    """Il falso allarme del 2026-08-17.

    `esegui_ssh` fonde stderr dentro stdout, quindi un avviso del client SSH
    ("Failed to add the host to the list of known hosts": la home del container
    non e' scrivibile) finiva nello stesso flusso del dato. Il timestamp
    diventava illeggibile, la sonda tornava SCONOSCIUTO, e dopo due giri la
    prod risultava giu' mentre il sito rispondeva 200.
    """
    recente = (T0 - timedelta(minutes=5)).isoformat()
    sporco = (
        "Failed to add the host to the list of known hosts "
        "(/home/app/.ssh/known_hosts).\r\n" + recente + "\n"
    )
    res = await sentinella_probes.sonda_vivacita(
        "psql ...", 45, T0, runner=runner_finto(0, sporco))
    assert res.esito == sentinella.OK


async def test_avviso_ssh_non_nasconde_uno_scheduler_fermo():
    """La tolleranza al rumore non deve diventare cecita' sul guasto vero."""
    vecchio = (T0 - timedelta(hours=3)).isoformat()
    sporco = "Warning: Permanently added host.\r\n" + vecchio + "\n"
    res = await sentinella_probes.sonda_vivacita(
        "psql ...", 45, T0, runner=runner_finto(0, sporco))
    assert res.esito == sentinella.GIU


async def test_solo_rumore_senza_timestamp_resta_sconosciuto():
    res = await sentinella_probes.sonda_vivacita(
        "psql ...", 45, T0, runner=runner_finto(0, "Warning: qualcosa\r\n\n"))
    assert res.esito == sentinella.SCONOSCIUTO
