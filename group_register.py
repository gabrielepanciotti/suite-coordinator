# group_register.py
"""Legare un gruppo Telegram a un progetto, senza cacciare il `chat_id` a mano.

Il `chat_id` di un gruppo lo conosce **il bot**: arriva dentro ogni update. La
procedura precedente lo faceva invece cercare a una persona — fermare il bot,
chiamare `getUpdates`, vincere la gara col polling — e sbagliava lato: chiedeva
a un umano un dato che il programma aveva gia' in mano. Qui il bot lo legge da
se' quando viene aggiunto al gruppo, e l'unica cosa che resta all'umano e' la
sola che il programma non puo' sapere: **a quale progetto** quel gruppo serve.

Questo modulo e' la parte pura: costruisce il menu e riscrive il manifesto.
Non parla con Telegram e non tocca lo stato del bot, cosi' e' testabile senza
ne' rete ne' bot (vedi `tests/test_group_register.py`).
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# Prefisso delle callback di questo flusso. Sta accanto a nm:/nr:/notif:/pl:...
# nella stessa convenzione a due lettere gia' usata da bot.py.
PREFIX = "gb"
NESSUNO = f"{PREFIX}:skip"

# Quanti progetti stanno in una schermata. Telegram accetta tastiere molto piu'
# grandi, ma una lista di 32 progetti costringe a scorrere per trovarne uno:
# meglio poche righe e una pagina in piu'.
PER_PAGINA = 8


AMBITO_ENV = "COORDINATOR_KIND"
"""Quale meta' della rete serve questo bot: "work", "personal", o non dichiarato.

Personale e lavoro restano separati per bot, non per filtro dentro un menu
comune: un gruppo appartiene a un mondo solo, e il bot che lo serve e' quello di
quel mondo. Un bot che sapesse legare gruppi dell'altra meta' renderebbe la
separazione una convenzione che regge finche' nessuno sbaglia bottone.
"""


def visibili(
    progetti: list[tuple[str, str, str]], ambito: str | None
) -> list[tuple[str, str]]:
    """I `(key, label)` che questo bot puo' legare, dati `(key, label, kind)`.

    `ambito` non dichiarato significa "non lo so", e in quel caso si mostrano
    tutti: nascondere meta' dei progetti su un device che non ha mai configurato
    nulla sarebbe un guasto invisibile, del tipo che questo repo ha gia' pagato
    con un registro di ripiego che sembrava quello vero.
    """
    scelto = (ambito or "").strip().lower()
    return [
        (key, label)
        for key, label, kind in progetti
        if not scelto or (kind or "").lower() == scelto
    ]


@dataclass(frozen=True)
class Bottone:
    """Una voce del menu: cosa si legge, e cosa torna indietro quando si preme."""

    testo: str
    dato: str


def pagina(
    progetti: list[tuple[str, str]], indice: int = 0, per_pagina: int = PER_PAGINA
) -> tuple[list[list[Bottone]], int]:
    """Le righe di bottoni per la pagina `indice`, e quante pagine ci sono.

    `progetti` e' una lista di `(key, label)` gia' ordinata da chi chiama: qui
    non si decide quali progetti mostrare, solo come impaginarli.

    Due per riga: un'etichetta con emoji piu' nome ci sta comoda, e una sola
    colonna raddoppierebbe le pagine per niente.
    """
    if per_pagina < 1:
        raise ValueError("per_pagina deve essere almeno 1")
    totale = max(1, -(-len(progetti) // per_pagina))  # ceil senza import
    indice = max(0, min(indice, totale - 1))
    fetta = progetti[indice * per_pagina : (indice + 1) * per_pagina]

    righe: list[list[Bottone]] = []
    for i in range(0, len(fetta), 2):
        righe.append(
            [Bottone(label, f"{PREFIX}:{key}") for key, label in fetta[i : i + 2]]
        )

    if totale > 1:
        nav: list[Bottone] = []
        if indice > 0:
            nav.append(Bottone("◀", f"{PREFIX}:p:{indice - 1}"))
        nav.append(Bottone(f"{indice + 1}/{totale}", f"{PREFIX}:p:{indice}"))
        if indice < totale - 1:
            nav.append(Bottone("▶", f"{PREFIX}:p:{indice + 1}"))
        righe.append(nav)

    # Sempre in fondo: aggiungere il bot a un gruppo non obbliga a legarlo
    # subito, e un menu senza uscita costringe a rispondere per forza.
    righe.append([Bottone("⏭ Nessuno, per ora", NESSUNO)])
    return righe, totale


def leggi_scelta(dato: str) -> tuple[str, str] | None:
    """Cosa significa una callback di questo flusso: ("pagina", n) | ("progetto", key)
    | ("istanza", id) | ("salta", "") — oppure None se non e' nostra.
    """
    if not dato.startswith(f"{PREFIX}:"):
        return None
    resto = dato[len(PREFIX) + 1 :]
    if resto == "skip":
        return ("salta", "")
    if resto.startswith("p:"):
        return ("pagina", resto[2:])
    if resto.startswith("i:"):
        # Una istanza Life, non un progetto (#82): il gruppo diventa il suo
        # canale telegram, scritto nell'instance.config.json dell'istanza.
        return ("istanza", resto[2:])
    return ("progetto", resto)


_SEZIONE = re.compile(r"^\[[^\]]+\]\s*$")
_TELEGRAM = re.compile(r"^\[telegram\]\s*$")
_CHAT_ID = re.compile(r"^\s*group_chat_id\s*=")


def con_group_chat_id(testo: str, chat_id: int) -> str:
    """Il manifesto `coordinator.toml` con `group_chat_id` impostato a `chat_id`.

    Riscrive **una riga**, non il file: un round-trip con una libreria TOML
    normalizzerebbe tutto e butterebbe via i commenti, che qui sono meta' del
    valore del file (ogni scelta e' motivata accanto alla riga che la applica).

    Tre casi, tutti coperti dai test: la chiave c'e' gia' (si sostituisce),
    la sezione `[telegram]` c'e' ma senza la chiave (si inserisce in testa alla
    sezione), la sezione non c'e' (si crea prima della prima altra sezione, cosi'
    le chiavi di primo livello di quella sezione non finiscono dentro un'altra).
    """
    righe = testo.splitlines()
    nuova = f"group_chat_id = {chat_id}"

    inizio = next((i for i, r in enumerate(righe) if _TELEGRAM.match(r)), None)
    if inizio is not None:
        fine = next(
            (i for i in range(inizio + 1, len(righe)) if _SEZIONE.match(righe[i])),
            len(righe),
        )
        for i in range(inizio + 1, fine):
            if _CHAT_ID.match(righe[i]):
                righe[i] = nuova
                return "\n".join(righe) + "\n"
        righe.insert(inizio + 1, nuova)
        return "\n".join(righe) + "\n"

    blocco = [
        "[telegram]",
        "# Scritto dal bot quando il gruppo e' stato legato al progetto.",
        nuova,
        "",
    ]
    prima_sezione = next((i for i, r in enumerate(righe) if _SEZIONE.match(r)), None)
    if prima_sezione is None:
        # Nessuna sezione: il blocco puo' solo andare in fondo, ma dopo una riga
        # vuota, altrimenti si incolla a un commento finale e ne diventa parte.
        coda = righe + ([""] if righe and righe[-1].strip() else []) + blocco
        return "\n".join(coda).rstrip("\n") + "\n"
    righe[prima_sezione:prima_sezione] = blocco
    return "\n".join(righe) + "\n"


def da_riproporre(
    in_attesa: dict[int, list[str]], legabili: list[str], legati: set[int]
) -> list[int]:
    """I gruppi a cui rimandare il menu, perche' ora c'e' qualcosa di nuovo.

    `in_attesa` e' `{chat_id: progetti gia' offerti}`: un gruppo che ha visto il
    menu e non e' stato legato. `on_my_chat_member` e' un evento di transizione
    che non si ripete, quindi un gruppo aggiunto prima che il suo progetto
    entrasse nel registro restava in sospeso finche' un umano non si ricordava
    `/lega` (#53). Qui lo si riprende, ma solo se fra i `legabili` di adesso ce
    n'e' almeno uno che quel gruppo non ha mai visto: ripetere lo stesso menu a
    ogni refresh sarebbe rumore, e un gruppo che ha premuto «salta» su una
    lista non deve rivederla identica.
    """
    disponibili = set(legabili)
    return [
        chat_id
        for chat_id, offerti in in_attesa.items()
        if chat_id not in legati and disponibili - set(offerti)
    ]
