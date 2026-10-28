#!/usr/bin/env python3
"""Popola HUB_AGENT_KEYS nel .env di QUESTO device, leggendo le chiavi dal bus.

Perche' esiste: senza HUB_AGENT_KEYS ogni chiamata al bus ripiega sulla chiave
di tenant condivisa - quella che puo' impersonare *ogni* agente del tenant - e
logga una volta per agente `bus auth DEGRADED for agent '<x>'`. Non rompe niente
il giorno in cui succede: rompe tutto insieme il giorno in cui quella chiave
viene revocata. Le identita' sul bus hanno gia' la loro chiave; quello che manca
e' distribuirla, ed e' un lavoro che a mano si sbaglia (un device dimenticato
resta degradato in silenzio, perche' continua a funzionare).

Le chiavi NON vengono inventate ne' ruotate: si leggono da
`GET {HUB_URL}/api/admin/agents/<nome>/key?tenant=<t>`. Se una manca, lo script
si ferma senza toccare il .env invece di scriverne uno a meta'.

Uso, dalla radice del repo Coordinator del device da sistemare:

    HUB_ADMIN_PASSWORD=... python3 scripts/seed_agent_keys.py           # anteprima
    HUB_ADMIN_PASSWORD=... python3 scripts/seed_agent_keys.py --scrivi  # applica

Poi RICREARE i container, non riavviarli: `env_file` si legge alla creazione, e
un `restart` lascia dentro le variabili vecchie senza dire niente.

    docker compose -f docker-compose.prod.yml up -d coordinator-worker coordinator-worker-deployer

Verifica che la chiave sia davvero in uso, non solo che il warning taccia: sul
bus, `X-Agent-Key` con il suo `X-Agent-Id` da' 200, la stessa chiave con l'id di
un altro agente da' 401.
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import shutil
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import local_defaults  # noqa: E402

DEFAULT_ENV = ".env"


def leggi_env(percorso: str) -> dict[str, str]:
    valori: dict[str, str] = {}
    with open(percorso, encoding="utf-8", errors="replace") as f:
        for riga in f:
            riga = riga.strip()
            if riga.startswith("#") or "=" not in riga:
                continue
            chiave, valore = riga.split("=", 1)
            valori[chiave] = valore
    return valori


def _tenant_di(env: dict[str, str]) -> str:
    """Primo tenant di HUB_TENANTS nel .env letto, poi il default dell'installazione."""
    return local_defaults.get("HUB_TENANTS", "default", env=env).split(",")[0].strip() or "default"


def identita(env: dict[str, str]) -> list[tuple[str, str]]:
    """Ogni identita' che questo device impersona sul bus, come (tenant, nome).

    HUB_LOCAL_AGENTS regge i listener; BOT_LOCAL_AGENTS e' la variante del device
    che fa girare bot e worker affiancati (il compose la rimappa su HUB_LOCAL_AGENTS).
    A queste si aggiungono il bot Telegram, l'identita' del repo e il deployer:
    parlano tutti sul bus e ognuno degrada per conto suo.
    """
    default_tenant = _tenant_di(env)
    trovate: dict[str, str] = {}

    for var in ("HUB_LOCAL_AGENTS", "BOT_LOCAL_AGENTS"):
        for pezzo in env.get(var, "").split(","):
            pezzo = pezzo.strip()
            if not pezzo or ":" not in pezzo:
                continue
            agente = pezzo.split(":", 1)[0].strip()
            # "tenant/agente:progetto" oppure "agente:progetto" (legacy).
            tenant, nome = agente.split("/", 1) if "/" in agente else (default_tenant, agente)
            trovate.setdefault(nome.strip(), tenant.strip())

    for var in ("HUB_TELEGRAM_AGENT", "HUB_AGENT_ID", "DEPLOYER_AGENT_ID"):
        nome = env.get(var, "").strip()
        if nome:
            # I nomi `-local` sono del tenant personale; il resto segue HUB_TENANTS.
            trovate.setdefault(nome, "life-personal" if nome.endswith("-local") else default_tenant)

    return sorted(((t, n) for n, t in trovate.items()), key=lambda x: x[1])


def chiave_di(base: str, tenant: str, nome: str, password: str) -> str:
    url = f"{base.rstrip('/')}/api/admin/agents/{nome}/key?tenant={tenant}"
    req = urllib.request.Request(url, headers={"X-Admin-Password": password})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            dati = json.load(resp)
    except urllib.error.HTTPError as exc:
        raise SystemExit(f"{tenant}/{nome}: il bus risponde HTTP {exc.code} - non scrivo niente")
    except Exception as exc:  # rete, DNS, timeout
        raise SystemExit(f"{tenant}/{nome}: non raggiungo il bus ({exc}) - non scrivo niente")

    chiave = dati.get("agent_key")
    if not chiave:
        raise SystemExit(
            f"{tenant}/{nome}: nessuna chiave sul bus. Seminala con "
            f"POST /api/admin/agents/{nome}/key/rotate?tenant={tenant}, poi rilancia."
        )
    if "," in chiave or ":" in chiave:
        # I separatori della mappa: una chiave che li contiene la spezzerebbe in
        # due voci sbagliate, e il danno si vedrebbe solo alla prima chiamata.
        raise SystemExit(f"{tenant}/{nome}: la chiave contiene ',' o ':' - non e' rappresentabile qui")
    return chiave


INTESTAZIONE = [
    "# Chiavi per-agente sul bus (X-Agent-Key): una per ogni identita' che questo",
    "# device impersona - i listener piu' il bot Telegram, HUB_AGENT_ID e il deployer.",
    "# Dal 2026-08-26 NON sono opzionali: key_required e' acceso su tutti gli agenti,",
    "# quindi senza la sua chiave un'identita' non parla proprio (401), non degrada.",
    "# Senza, ogni chiamata ripiega sulla chiave di tenant condivisa e logga",
    "# 'bus auth DEGRADED for agent ...': funziona finche' quella chiave non viene",
    "# revocata, poi si spegne tutto insieme. Rigenerabile: scripts/seed_agent_keys.py",
]


def riscrivi(percorso: str, riga_chiavi: str) -> str:
    testo = open(percorso, "rb").read().decode("utf-8")
    crlf = "\r\n" in testo  # i .env dei box Windows sono CRLF: va preservato
    righe = testo.replace("\r\n", "\n").split("\n")

    nuove: list[str] = []
    inserito = False
    for riga in righe:
        if riga.startswith("HUB_AGENT_KEYS=") or riga in INTESTAZIONE:
            continue  # rimpiazziamo il blocco vecchio, non ne accumuliamo due
        nuove.append(riga)
        if not inserito and riga.startswith(("HUB_LOCAL_AGENTS=", "BOT_LOCAL_AGENTS=")):
            nuove.extend(INTESTAZIONE)
            nuove.append(riga_chiavi)
            inserito = True

    if not inserito:
        raise SystemExit(
            "nessun HUB_LOCAL_AGENTS=/BOT_LOCAL_AGENTS= a cui agganciare il blocco: "
            "non tocco il .env"
        )

    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M")
    backup = f"{percorso}.bak-agentkeys-{stamp}"
    shutil.copy2(percorso, backup)

    finale = "\n".join(nuove)
    if crlf:
        finale = finale.replace("\n", "\r\n")
    open(percorso, "wb").write(finale.encode("utf-8"))
    return backup


def scrivi_singola(percorso: str, chiave: str) -> str:
    """Modo workspace: un solo HUB_AGENT_KEY accanto a HUB_AGENT_ID.

    I .env dei progetti non reggono listener: servono a /hub-send, che parla
    come una sola identita' (HUB_AGENT_ID). Finche' li' c'e' solo la chiave di
    tenant, quell'identita' non e' proteggibile con key_required - il turno
    finirebbe con un 403 dopo che l'agente ha gia' lavorato, cioe' nel modo piu'
    caro possibile.
    """
    testo = open(percorso, "rb").read().decode("utf-8")
    crlf = "\r\n" in testo
    righe = testo.replace("\r\n", "\n").split("\n")

    nuove: list[str] = []
    inserito = False
    for riga in righe:
        if riga.startswith("HUB_AGENT_KEY="):
            continue
        nuove.append(riga)
        if not inserito and riga.startswith("HUB_AGENT_ID="):
            nuove.append(f"HUB_AGENT_KEY={chiave}")
            inserito = True
    if not inserito:
        raise SystemExit(f"{percorso}: nessun HUB_AGENT_ID= a cui affiancare la chiave")

    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M")
    backup = f"{percorso}.bak-agentkey-{stamp}"
    shutil.copy2(percorso, backup)
    finale = "\n".join(nuove)
    if crlf:
        finale = finale.replace("\n", "\r\n")
    open(percorso, "wb").write(finale.encode("utf-8"))
    return backup


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env", default=DEFAULT_ENV, help="percorso del .env (default: ./.env)")
    parser.add_argument("--scrivi", action="store_true", help="applica; senza, mostra solo l'anteprima")
    parser.add_argument(
        "--workspace",
        action="store_true",
        help="modo workspace di progetto: scrive il singolo HUB_AGENT_KEY dell'HUB_AGENT_ID",
    )
    args = parser.parse_args()

    password = os.environ.get("HUB_ADMIN_PASSWORD", "")
    if not password:
        raise SystemExit("serve HUB_ADMIN_PASSWORD (ADMIN_PASSWORD del bus Network)")

    if not os.path.exists(args.env):
        raise SystemExit(f"{args.env} non esiste")

    env = leggi_env(args.env)
    base = env.get("HUB_URL", "").strip()
    if not base:
        raise SystemExit(f"{args.env} non ha HUB_URL")

    if args.workspace:
        nome = env.get("HUB_AGENT_ID", "").strip()
        if not nome:
            raise SystemExit(f"{args.env} non ha HUB_AGENT_ID: non e' un workspace di progetto")
        tenant = "life-personal" if nome.endswith("-local") else (
            _tenant_di(env))
        chiave = chiave_di(base, tenant, nome, password)
        if not args.scrivi:
            print(f"{args.env}: {tenant}/{nome} ha la sua chiave sul bus. --scrivi per applicarla.")
            return
        backup = scrivi_singola(args.env, chiave)
        print(f"{args.env}: HUB_AGENT_KEY di {nome} scritta (backup: {backup})")
        return

    elenco = identita(env)
    if not elenco:
        raise SystemExit("nessuna identita' nel .env: niente da fare")

    print(f"bus {base} - {len(elenco)} identita' da questo device:")
    coppie = []
    for tenant, nome in elenco:
        coppie.append(f"{nome}:{chiave_di(base, tenant, nome, password)}")
        print(f"  ok  {tenant}/{nome}")

    riga = "HUB_AGENT_KEYS=" + ",".join(coppie)
    if not args.scrivi:
        print(f"\nanteprima: {len(coppie)} coppie pronte. Rilancia con --scrivi per applicarle.")
        return

    backup = riscrivi(args.env, riga)
    print(f"\nscritte {len(coppie)} coppie in {args.env} (backup: {backup})")
    print("ora RICREA i container (up -d), non basta un restart: env_file si legge alla creazione.")


if __name__ == "__main__":
    sys.exit(main())
