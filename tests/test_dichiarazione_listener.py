"""Il Coordinator dichiara al bus chi ascolta per ogni agente (issue #37).

Due processi sullo stesso nome ricevono entrambi ogni evento e svegliano
entrambi: due risposte alla stessa domanda, nessuna al corrente dell'altra. E
nessuno dei due puo' accorgersene da solo — per questo la dichiarazione e' una
funzione, non una nota in un log.
"""
from __future__ import annotations

import asyncio

import httpx
import pytest

import hub_client


class _FintoClient:
    def __init__(self, registro, risposta):
        self._registro = registro
        self._risposta = risposta

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, headers=None, json=None):
        self._registro.append((url, headers, json))
        if isinstance(self._risposta, Exception):
            raise self._risposta
        return self._risposta


def _installa(monkeypatch, registro, risposta):
    monkeypatch.setattr(
        hub_client.httpx, "AsyncClient",
        lambda **k: _FintoClient(registro, risposta),
    )


def _risposta(status: int) -> httpx.Response:
    return httpx.Response(status, request=httpx.Request("POST", "http://bus/x"))


def test_dichiara_host_e_agente(monkeypatch):
    monkeypatch.setenv("HUB_LISTENER_HOST", "listener-host-1")
    registro: list = []
    _installa(monkeypatch, registro, _risposta(200))

    assert asyncio.run(
        hub_client.dichiara_proprietario("http://bus", "tk", "offerte-dev-linux", "ak")
    ) is True
    url, headers, corpo = registro[0]
    assert url.endswith("/api/agents/offerte-dev-linux/owner")
    assert corpo == {"host": "listener-host-1"}
    # Autenticata con la chiave dell'agente: dichiarare e' provare, non promettere.
    assert "ak" in str(headers)


@pytest.mark.parametrize(
    "esito", [_risposta(404), _risposta(500), httpx.ConnectError("giu'")]
)
def test_un_bus_che_non_collabora_non_ferma_il_listener(esito, monkeypatch):
    """La dichiarazione e' diagnosi, non permesso."""
    _installa(monkeypatch, [], esito)
    assert asyncio.run(
        hub_client.dichiara_proprietario("http://bus", "tk", "db-dev-linux")
    ) is False


def test_lo_stream_porta_l_host_nell_header(monkeypatch):
    """Dentro un container gethostname() e' l'id del container, che non dice
    su quale macchina sta: il bus vedeva `?172.24.0.1` (issue #37)."""
    monkeypatch.setenv("HUB_LISTENER_HOST", "listener-host-1")
    visti: dict = {}

    class _Stream:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        def raise_for_status(self):
            pass

        def aiter_lines(self):
            async def vuoto():
                return
                yield  # pragma: no cover
            return vuoto()

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        def stream(self, metodo, url, headers=None):
            visti.update(headers or {})
            return _Stream()

    monkeypatch.setattr(hub_client.httpx, "AsyncClient", lambda **k: _Client())

    async def consuma():
        async for _ in hub_client.stream_events("http://bus", "tk", "db-dev-linux"):
            pass

    asyncio.run(consuma())
    assert visti["X-Listener-Host"] == "listener-host-1"
