"""Il menu e la riscrittura del manifesto, senza rete e senza bot.

La riscrittura e' l'unica parte che puo' rovinare un file di un altro repo:
ogni forma in cui il manifesto puo' arrivare ha il suo test, e ognuno verifica
che il TOML resti parsabile e che i commenti sopravvivano.
"""
import tomllib

import pytest

from group_register import (
    NESSUNO,
    PREFIX,
    Bottone,
    con_group_chat_id,
    visibili,
    leggi_scelta,
    pagina,
)

PROGETTI = [(f"p{i}", f"P{i}") for i in range(20)]


# --- menu ---------------------------------------------------------------------


def test_i_progetti_stanno_due_per_riga():
    righe, _ = pagina([("a", "A"), ("b", "B"), ("c", "C")], per_pagina=8)
    assert [len(r) for r in righe[:2]] == [2, 1]
    assert righe[0][0] == Bottone("A", f"{PREFIX}:a")


def test_una_pagina_sola_non_mostra_la_navigazione():
    righe, totale = pagina(PROGETTI[:4], per_pagina=8)
    assert totale == 1
    piatti = [b.dato for r in righe for b in r]
    assert not any(d.startswith(f"{PREFIX}:p:") for d in piatti)


def test_la_navigazione_appare_solo_dove_serve():
    _, totale = pagina(PROGETTI, per_pagina=8)
    assert totale == 3

    prima, _ = pagina(PROGETTI, 0, per_pagina=8)
    nav = [b.testo for b in prima[-2]]
    assert nav == ["1/3", "▶"]  # niente ◀ sulla prima

    mezzo, _ = pagina(PROGETTI, 1, per_pagina=8)
    assert [b.testo for b in mezzo[-2]] == ["◀", "2/3", "▶"]

    ultima, _ = pagina(PROGETTI, 2, per_pagina=8)
    assert [b.testo for b in ultima[-2]] == ["◀", "3/3"]  # niente ▶ sull'ultima


def test_una_pagina_fuori_scala_non_esplode_ma_si_ferma_al_bordo():
    # Una callback vecchia (menu ripescato da una chat di ieri) chiede pagina 99:
    # deve dare l'ultima, non sollevare e non mostrare una tastiera vuota.
    righe, _ = pagina(PROGETTI, 99, per_pagina=8)
    assert any(b.dato == f"{PREFIX}:p16" or b.testo == "P16" for r in righe for b in r)


def test_l_uscita_c_e_sempre():
    for indice in (0, 1, 2):
        righe, _ = pagina(PROGETTI, indice, per_pagina=8)
        assert righe[-1][0].dato == NESSUNO


def test_senza_progetti_resta_almeno_l_uscita():
    righe, totale = pagina([])
    assert totale == 1
    assert righe == [[Bottone("⏭ Nessuno, per ora", NESSUNO)]]


# --- lettura della callback ----------------------------------------------------


@pytest.mark.parametrize(
    "dato,atteso",
    [
        (f"{PREFIX}:contentextractor", ("progetto", "contentextractor")),
        (f"{PREFIX}:p:2", ("pagina", "2")),
        (f"{PREFIX}:i:energypass", ("istanza", "energypass")),
        (NESSUNO, ("salta", "")),
    ],
)
def test_una_callback_nostra_si_legge(dato, atteso):
    assert leggi_scelta(dato) == atteso


def test_una_callback_di_un_altro_flusso_non_e_nostra():
    # Il bot registra molti CallbackQueryHandler: rivendicare un dato altrui
    # ruberebbe il click a chi lo aspetta.
    assert leggi_scelta("nm:qualcosa") is None
    assert leggi_scelta("notif:1") is None
    assert leggi_scelta("") is None


# --- riscrittura del manifesto -------------------------------------------------

CON_SEZIONE = """\
# manifesto di prova
[telegram]
group_chat_id = -1001111111111
agent = "persona"

[[capability]]
key = "ingest"
"""

SEZIONE_SENZA_CHIAVE = """\
[telegram]
agent = "persona"

[[capability]]
key = "ingest"
"""

SENZA_SEZIONE = """\
# Nessun blocco [telegram]: il nodo passa dal bot radice.
# Questo commento spiega una decisione e non deve sparire.

[routing]
domains = ["instagram.com"]

[[capability]]
key = "ingest"
"""


def test_una_chiave_gia_presente_viene_sostituita_non_duplicata():
    fuori = con_group_chat_id(CON_SEZIONE, -1002222222222)
    d = tomllib.loads(fuori)
    assert d["telegram"]["group_chat_id"] == -1002222222222
    assert fuori.count("group_chat_id") == 1
    assert d["telegram"]["agent"] == "persona"  # il resto della sezione resta


def test_la_chiave_entra_nella_sezione_che_esiste_gia():
    fuori = con_group_chat_id(SEZIONE_SENZA_CHIAVE, -1003)
    d = tomllib.loads(fuori)
    assert d["telegram"] == {"group_chat_id": -1003, "agent": "persona"}


def test_senza_sezione_il_blocco_nasce_prima_delle_altre():
    fuori = con_group_chat_id(SENZA_SEZIONE, -1004)
    d = tomllib.loads(fuori)
    assert d["telegram"]["group_chat_id"] == -1004
    # Il punto della posizione: le chiavi di [telegram] non devono finire
    # dentro [routing], che in TOML e' esattamente cio' che succede se il
    # blocco viene appeso in fondo.
    assert d["routing"]["domains"] == ["instagram.com"]
    assert len(d["capability"]) == 1
    # I commenti sono meta' del valore di questi file.
    assert "Questo commento spiega una decisione" in fuori


def test_riscrivere_due_volte_e_idempotente():
    una = con_group_chat_id(SENZA_SEZIONE, -1005)
    due = con_group_chat_id(una, -1005)
    assert una == due


def test_un_file_di_soli_commenti_resta_valido():
    fuori = con_group_chat_id("# solo un commento\n", -1006)
    assert tomllib.loads(fuori)["telegram"]["group_chat_id"] == -1006


def test_il_manifesto_finisce_sempre_con_un_a_capo():
    for testo in (CON_SEZIONE, SEZIONE_SENZA_CHIAVE, SENZA_SEZIONE, "# x\n"):
        assert con_group_chat_id(testo, -1007).endswith("\n")


# --- ambito del bot ------------------------------------------------------------

MISTI = [
    ("offerte", "💼 Hub Offerte", "work"),
    ("cheftoni", "🍳 ChefToni", "personal"),
    ("cruscotto", "📊 Cruscotto", "work"),
]


def test_un_bot_di_lavoro_non_vede_i_progetti_personali():
    # La separazione vive qui: un bot che potesse legare un gruppo dell'altra
    # meta' renderebbe la divisione una convenzione, non una regola.
    assert visibili(MISTI, "work") == [
        ("offerte", "💼 Hub Offerte"),
        ("cruscotto", "📊 Cruscotto"),
    ]


def test_un_bot_personale_vede_solo_i_suoi():
    assert visibili(MISTI, "personal") == [("cheftoni", "🍳 ChefToni")]


def test_l_ambito_e_insensibile_a_maiuscole_e_spazi():
    assert len(visibili(MISTI, "  Work ")) == 2


@pytest.mark.parametrize("ambito", [None, "", "   "])
def test_senza_ambito_dichiarato_si_mostra_tutto(ambito):
    # Un device che non ha configurato nulla non deve perdere meta' del menu in
    # silenzio: meglio mostrare troppo che nascondere senza dirlo.
    assert len(visibili(MISTI, ambito)) == 3


def test_un_ambito_che_non_esiste_non_mostra_niente_invece_di_tutto():
    # Un refuso in .env deve dare un menu vuoto (visibile) e non un menu pieno
    # (che sembrerebbe funzionante).
    assert visibili(MISTI, "wrok") == []
