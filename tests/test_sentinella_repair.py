from datetime import datetime, timedelta, timezone

import sentinella_repair
from sentinella_config import Servizio
from sentinella_store import StatoServizio

T0 = datetime(2026, 8, 17, 10, 0, tzinfo=timezone.utc)
SRV = Servizio(chiave="portale", titolo="Portale", dominio="x.it",
               url_salute="http://x/h", container=("portale_db_prod",),
               compose_file="docker-compose.prod.yml",
               project_dir="X:/srv/PortaleWeb_Segnalatori")


def runner_finto(chiamate: list):
    async def _r(comando: str):
        chiamate.append(comando)
        return 0, ""
    return _r


async def test_ripara_container_mancante():
    chiamate = []
    tentati = await sentinella_repair.ripara(
        SRV, ["portale_db_prod"], StatoServizio(), T0, runner=runner_finto(chiamate))
    assert tentati == ["portale_db_prod"]
    assert "docker compose" in chiamate[0]
    assert "X:/srv/PortaleWeb_Segnalatori" in chiamate[0]


async def test_up_senza_nominare_i_servizi():
    """I nomi dei servizi compose non coincidono con quelli dei container:
    passarli farebbe fallire il riavvio con 'no such service'."""
    chiamate = []
    await sentinella_repair.ripara(
        SRV, ["portale_db_prod"], StatoServizio(), T0, runner=runner_finto(chiamate))
    assert chiamate[0].rstrip().endswith("up -d")


async def test_un_solo_comando_anche_con_piu_container_mancanti():
    chiamate = []
    tentati = await sentinella_repair.ripara(
        SRV, ["c1", "c2", "c3"], StatoServizio(), T0, runner=runner_finto(chiamate))
    assert len(chiamate) == 1
    assert tentati == ["c1", "c2", "c3"]


async def test_non_ritenta_entro_un_ora():
    """Un watchdog che riavvia in loop trasforma un guasto in un disastro."""
    stato = StatoServizio(riparazioni={"portale_db_prod": T0.isoformat()})
    chiamate = []
    tentati = await sentinella_repair.ripara(
        SRV, ["portale_db_prod"], stato, T0 + timedelta(minutes=30),
        runner=runner_finto(chiamate))
    assert tentati == []
    assert chiamate == []


async def test_ritenta_dopo_un_ora():
    stato = StatoServizio(riparazioni={"portale_db_prod": T0.isoformat()})
    chiamate = []
    tentati = await sentinella_repair.ripara(
        SRV, ["portale_db_prod"], stato, T0 + timedelta(hours=1, minutes=1),
        runner=runner_finto(chiamate))
    assert tentati == ["portale_db_prod"]


async def test_registra_il_tentativo_nello_stato():
    stato = StatoServizio()
    await sentinella_repair.ripara(
        SRV, ["portale_db_prod"], stato, T0, runner=runner_finto([]))
    assert stato.riparazioni["portale_db_prod"] == T0.isoformat()


async def test_un_comando_fallito_non_solleva_e_resta_registrato():
    """Se il riavvio fallisce, l'allarme deve partire comunque."""
    async def fallisce(comando):
        return 1, "no such service"

    stato = StatoServizio()
    tentati = await sentinella_repair.ripara(
        SRV, ["portale_db_prod"], stato, T0, runner=fallisce)
    assert tentati == ["portale_db_prod"]
    assert "portale_db_prod" in stato.riparazioni


async def test_runner_che_esplode_non_propaga():
    async def esplode(comando):
        raise OSError("ssh morto")

    tentati = await sentinella_repair.ripara(
        SRV, ["portale_db_prod"], StatoServizio(), T0, runner=esplode)
    assert tentati == ["portale_db_prod"]
