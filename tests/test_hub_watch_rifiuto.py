"""Un 401 non e' una rete che balla (issue #31).

Il bot aveva gia' imparato a fermarsi dopo N rifiuti; questo watcher no, e
ritentava la stessa credenziale una volta al secondo per sempre. Un errore che
si ripete mille volte l'ora smette di essere un segnale.
"""
from __future__ import annotations

import asyncio

import httpx
import pytest

import hub_watch


def _rifiuto(status: int) -> httpx.HTTPStatusError:
    richiesta = httpx.Request("GET", "http://bus/api/events/stream")
    return httpx.HTTPStatusError(
        "rifiutato", request=richiesta, response=httpx.Response(status, request=richiesta)
    )


def _connect_che_alza(exc: BaseException, conteggio: dict):
    async def stream():
        conteggio["n"] = conteggio.get("n", 0) + 1
        raise exc
        yield  # pragma: no cover - rende la funzione un generatore asincrono

    return stream


@pytest.mark.parametrize("status", [401, 403])
def test_un_rifiuto_ferma_il_watcher_subito(status, monkeypatch, tmp_path):
    monkeypatch.setenv("HUB_CLAIMS_DIR", str(tmp_path))
    conteggio: dict = {}
    with pytest.raises(hub_watch.RifiutatoDalBus) as caduta:
        asyncio.run(
            hub_watch.watch(
                "acme", "db-dev-linux", "http://bus", "k",
                connect=_connect_che_alza(_rifiuto(status), conteggio),
            )
        )
    assert conteggio["n"] == 1, "non deve esserci un secondo tentativo"
    assert caduta.value.status == status
    assert "non ritento" in str(caduta.value)
    assert "db-dev-linux" in str(caduta.value)


def test_un_guasto_vero_continua_a_ritentare(monkeypatch, tmp_path):
    """Il rimedio e' opposto: una connessione caduta si riprende da sola."""
    monkeypatch.setenv("HUB_CLAIMS_DIR", str(tmp_path))
    monkeypatch.setattr(hub_watch, "RECONNECT_BACKOFF_S", [0])
    conteggio: dict = {}

    async def connect():
        conteggio["n"] = conteggio.get("n", 0) + 1
        if conteggio["n"] >= 3:
            raise hub_watch.RifiutatoDalBus(401, "x")  # ferma il test
        raise httpx.ConnectError("rete giu'")
        yield  # pragma: no cover

    with pytest.raises(hub_watch.RifiutatoDalBus):
        asyncio.run(
            hub_watch.watch("acme", "db-dev-linux", "http://bus", "k", connect=connect)
        )
    assert conteggio["n"] == 3


def test_lo_stato_di_uscita_distingue_il_rifiuto_dalla_consegna(monkeypatch, capsys):
    """stdout vuoto e codice 2: non c'e' nessun evento da riprendere."""
    monkeypatch.setenv("HUB_URL", "http://bus")
    monkeypatch.setenv("HUB_CLAIM_TENANT", "acme")  # env > locale: no installation tenant
    monkeypatch.setenv("HUB_API_KEY_ACME", "k")

    def esplode(coro, *a, **k):
        coro.close()  # niente "coroutine was never awaited" a sporcare la suite
        raise hub_watch.RifiutatoDalBus(403, "db-dev-linux")

    monkeypatch.setattr(hub_watch.asyncio, "run", esplode)

    assert hub_watch.main(["db-dev-linux"]) == 2
    catturato = capsys.readouterr()
    assert catturato.out == ""
    assert "non ritento" in catturato.err
