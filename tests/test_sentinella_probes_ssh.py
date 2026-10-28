import sentinella
import sentinella_probes


def runner_finto(uscita: int, testo: str):
    async def _r(comando: str):
        return uscita, testo
    return _r


async def test_motore_docker_vivo():
    res = await sentinella_probes.sonda_motore_docker(runner=runner_finto(0, "ok"))
    assert res.esito == sentinella.OK


async def test_motore_docker_fermo_e_il_guasto_di_agosto():
    """Box acceso, Docker Desktop mai ripartito dopo il riavvio dell'8 agosto."""
    testo = "failed to connect to the docker API at npipe:////./pipe/dockerDesktopLinuxEngine"
    res = await sentinella_probes.sonda_motore_docker(runner=runner_finto(1, testo))
    assert res.esito == sentinella.GIU
    assert "motore Docker" in res.motivo


async def test_ssh_irraggiungibile_e_sconosciuto():
    res = await sentinella_probes.sonda_motore_docker(runner=runner_finto(255, "timeout"))
    assert res.esito == sentinella.SCONOSCIUTO
    assert "irraggiungibile" in res.motivo


async def test_container_tutti_presenti():
    presenti = "portale_segnalazioni_backend_prod\nportale_db_prod"
    res, mancanti = await sentinella_probes.sonda_container(
        ("portale_segnalazioni_backend_prod", "portale_db_prod"),
        runner=runner_finto(0, presenti))
    assert res.esito == sentinella.OK
    assert mancanti == []


async def test_container_mancante_viene_nominato():
    res, mancanti = await sentinella_probes.sonda_container(
        ("portale_segnalazioni_backend_prod", "portale_db_prod"),
        runner=runner_finto(0, "portale_segnalazioni_backend_prod"))
    assert res.esito == sentinella.GIU
    assert mancanti == ["portale_db_prod"]
    assert "portale_db_prod" in res.motivo


async def test_docker_ps_fallito_e_sconosciuto_non_container_mancanti():
    """Se il comando fallisce non sappiamo nulla: non e' 'sono tutti giu'."""
    res, mancanti = await sentinella_probes.sonda_container(
        ("c1",), runner=runner_finto(1, "errore"))
    assert res.esito == sentinella.SCONOSCIUTO
    assert mancanti == []


async def test_runner_che_esplode_diventa_sconosciuto():
    async def esplode(comando):
        raise OSError("ssh non installato")

    res = await sentinella_probes.sonda_motore_docker(runner=esplode)
    assert res.esito == sentinella.SCONOSCIUTO
