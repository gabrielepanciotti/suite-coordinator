"""Una CLI non autenticata e' un guasto del processo, non della conversazione.

E' il difetto che ha reso #34 invisibile per tre mesi: l'unico segnale era un
`needs_human` scritto DENTRO la conversazione stallata — cioe' nel posto dove
finisce chi gia' non riceve risposta — e si ripeteva per ogni conversazione
invece di essere detto una volta.
"""
from __future__ import annotations

import asyncio

import claude_runner
import hub


def test_riconosce_la_cli_non_autenticata():
    assert claude_runner.is_auth_error("❌ claude exit=1\nNot logged in · Please run /login")
    assert claude_runner.is_auth_error("❌ claude exit=1\nPlease run /login")


def test_non_confonde_un_guasto_di_trasporto():
    assert not claude_runner.is_auth_error("❌ claude exit=1\nconnection reset by peer")
    assert claude_runner.is_transport_error("❌ claude exit=1\nconnection reset by peer")


def test_una_risposta_normale_non_e_un_guasto():
    """Un agente puo' benissimo *parlare* di login senza essere rotto."""
    assert not claude_runner.is_auth_error("Ti serve fare /login su quel box.")


def test_lo_dice_una_volta_sola_anche_su_conversazioni_diverse(monkeypatch, tmp_path):
    # The notice goes through _post_auto_notice, which skips conversations the
    # store already marks: without its own store the test read the real one.
    monkeypatch.setattr(hub.hub_store, "HUB_TOPICS_PATH", str(tmp_path / "hub_topics.json"))
    inviati: list[tuple[int, str]] = []

    class FintoClient:
        async def send_message(self, conv_id, text, needs_human=False):
            inviati.append((conv_id, text))

    monkeypatch.setattr(hub, "_client", lambda *a, **k: FintoClient())
    monkeypatch.setitem(hub._S, "send_to", None)
    hub._CLI_NON_AUTENTICATA.clear()

    async def scenario():
        for conv in (201, 202, 203):
            await hub._segnala_cli_non_autenticata(
                "acme", "cruscotto-prod", conv, "❌ Not logged in"
            )

    asyncio.run(scenario())

    assert len(inviati) == 1, "tre conversazioni, una notizia"
    assert inviati[0][0] == 201
    verdetto = hub.cli_non_autenticata()
    assert verdetto["agent"] == "cruscotto-prod"


def test_preferisce_telegram_al_thread_stallato(monkeypatch):
    """Se questo Coordinator ha una faccia umana, l'avviso esce di li'."""
    su_telegram: list[str] = []
    sul_bus: list[str] = []

    class FintoClient:
        async def send_message(self, conv_id, text, needs_human=False):
            sul_bus.append(text)

    async def send_to(uid, text):
        su_telegram.append(text)

    monkeypatch.setattr(hub, "_client", lambda *a, **k: FintoClient())
    monkeypatch.setitem(hub._S, "send_to", send_to)
    import profiles
    monkeypatch.setattr(profiles, "admin_user_ids", lambda: [1])
    hub._CLI_NON_AUTENTICATA.clear()

    asyncio.run(hub._segnala_cli_non_autenticata("acme", "db-prod", 9, "❌ Not logged in"))

    assert len(su_telegram) == 1 and not sul_bus
