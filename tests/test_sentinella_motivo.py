"""Il motivo non deve gonfiarsi a ogni giro.

Il 26 agosto il messaggio di `offerte-pubbliche` conteneva DODICI copie di
"riavvio tentato": la nota veniva accodata al motivo gia' salvato, che a sua
volta veniva risalvato. Un messaggio che cresce senza limite e' illeggibile
proprio quando serve leggerlo.
"""
from sentinella_giro import _con_nota_riavvio


def test_la_nota_viene_aggiunta_una_volta():
    m = _con_nota_riavvio("container assenti: c1", ["c1"])
    assert m == "container assenti: c1 — riavvio tentato: c1"


def test_applicarla_di_nuovo_non_la_duplica():
    m = _con_nota_riavvio("container assenti: c1", ["c1"])
    for _ in range(11):
        m = _con_nota_riavvio(m, ["c1"])
    assert m.count("riavvio tentato") == 1


def test_senza_container_recenti_il_motivo_resta_intatto():
    assert _con_nota_riavvio("container assenti: c1", []) == "container assenti: c1"
