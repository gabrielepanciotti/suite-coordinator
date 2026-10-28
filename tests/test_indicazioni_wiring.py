"""Il diario è cablato sulla strada che percorrono tutti i messaggi umani.

Questi test guardano il *cablaggio*, non il modulo: `indicazioni.py` era
corretto e coperto da nove test verdi, eppure il 2026-09-08 il diario era
vuoto. La funzione che lo riempie era chiamata da un gestore solo — quello del
bot extra — mentre i messaggi del gruppo passano da `handle_message`. Un pezzo
che esiste ma che nessuno chiama non lo vede nessun test sul pezzo.
"""

from __future__ import annotations

import inspect

import bot


def test_ogni_messaggio_umano_finisce_nel_diario():
    """`handle_message` è l'imbuto di tutti i bot: la registrazione sta lì.

    I gestori a monte (il bot extra, il routing per nodo) delegano tutti qui.
    Registrare più su significa registrare solo una parte dei messaggi — che è
    esattamente il guasto del 2026-09-08."""
    corpo = inspect.getsource(bot.handle_message)
    assert "_registra_indicazione(" in corpo


def test_la_registrazione_precede_il_modello():
    """Il senso del diario è sopravvivere al turno che va storto: se si
    scrivesse dopo, un turno morto non lascerebbe traccia — ed è così che una
    delle tre indicazioni del 2026-09-06 è sparita del tutto."""
    corpo = inspect.getsource(bot.handle_message)
    assert corpo.index("_registra_indicazione(") < corpo.index("resolve_agent_by_intent(")


def test_il_lavoro_del_turno_esce_dal_container():
    """L'altra metà: `pubblica` sul percorso comune del turno. Le due garanzie
    sono separate e si rompono separatamente — il 2026-09-08 questa funzionava
    e l'altra no."""
    corpo = inspect.getsource(bot._claude_turn_locked)
    assert "indicazioni.pubblica" in corpo


def test_la_registrazione_non_e_rimasta_anche_sul_gestore_del_bot_extra():
    """Quel gestore delega a `handle_message`: lasciarcela scriverebbe due
    volte lo stesso messaggio."""
    corpo = inspect.getsource(bot._make_node_text_handler)
    assert "_registra_indicazione(" not in corpo
