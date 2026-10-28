#!/usr/bin/env python3
"""Porta sul bus le identita' che Life calcola per questo device (#81).

Gli agenti locali non si scrivono piu' a mano: li calcola Life
(``suite_core.instances.identity``, vedi ``bus_agents.py``). Ma un nome calcolato
non esiste sul bus finche' qualcuno non ce lo mette, e un listener su un nome che
il bus non conosce prende 403 a ogni giro. Questo script chiude il giro, per
ogni identita' calcolata che sul bus manca:

1. se sul bus c'e' ancora un suo nome vecchio (``formerIds`` del grafo, oppure la
   grafia kebab di prima di Network#42: ``acme-auth`` per ``acmeAuth``) lo
   **rinomina** (``POST /api/admin/agents/{vecchio}/rename``): stessa riga, stessa
   chiave, conversazioni e non letti al loro posto, e il nome vecchio resta un
   alias finche' non lo si ritira. Mai una seconda identita' accanto alla prima;
2. altrimenti la **crea** (``POST /api/agents``) e le semina la chiave
   (``POST .../key/rotate``: su un agente appena nato non c'e' nulla da ruotare).

Poi aggiorna ``HUB_AGENT_KEYS`` nel .env: un nome rinominato porta con se' la
chiave che aveva, uno nuovo prende quella appena seminata.

Uso, dalla radice del repo Coordinator del device:

    PY=$WS_LIFE/packages/shared/.venv/bin/python   # ha le dipendenze di suite_core
    HUB_ADMIN_PASSWORD=... $PY scripts/riconcilia_identita_bus.py           # anteprima
    HUB_ADMIN_PASSWORD=... $PY scripts/riconcilia_identita_bus.py --scrivi  # applica

Life viene letto dal checkout in ``WS_LIFE`` (o ``--life``), il registro del
device da ``LIFE_HOME_HOST_DIR`` (o ``~/.life``), l'host da ``LIFE_DEVICE``.
Dopo ``--scrivi`` va ricreato il bot (``scripts/deploy/ricrea-bot.sh
--force-recreate``): ``env_file`` si legge alla creazione del container.
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import shutil
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import bus_agents  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
from seed_agent_keys import chiave_di, leggi_env  # noqa: E402


def _req(method: str, url: str, headers: dict[str, str], body: dict | None = None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.load(resp)


def piano(agents, presenti: dict[str, set[str]]) -> list[tuple[str, object, str]]:
    """``(azione, agente, nome_vecchio)`` per ogni identita' che sul bus manca.

    ``azione`` e' ``rinomina`` o ``crea``. ``presenti`` = nomi per tenant.
    """
    out = []
    for a in agents:
        sul_bus = presenti.get(a.tenant, set())
        if a.name in sul_bus:
            continue
        vecchio = next(
            (n for n in bus_agents.former_names(a, bus_agents.suffix_of(a)) if n in sul_bus),
            "",
        )
        out.append(("rinomina" if vecchio else "crea", a, vecchio))
    return out


def _coppie(riga: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for pezzo in riga.split(","):
        if ":" in pezzo:
            nome, chiave = pezzo.split(":", 1)
            out[nome.strip()] = chiave.strip()
    return out


def aggiorna_chiavi(riga: str, rinomine: dict[str, str], nuove: dict[str, str]) -> str:
    """``HUB_AGENT_KEYS`` con i nomi rinominati e le chiavi nuove."""
    coppie = {rinomine.get(n, n): k for n, k in _coppie(riga).items()}
    coppie.update(nuove)
    return ",".join(f"{n}:{k}" for n, k in coppie.items())


def scrivi_env(percorso: str, chiavi: str) -> str:
    testo = open(percorso, "rb").read().decode("utf-8")
    crlf = "\r\n" in testo
    righe = testo.replace("\r\n", "\n").split("\n")
    righe = [f"HUB_AGENT_KEYS={chiavi}" if r.startswith("HUB_AGENT_KEYS=") else r for r in righe]
    if not any(r.startswith("HUB_AGENT_KEYS=") for r in righe):
        righe.append(f"HUB_AGENT_KEYS={chiavi}")
    backup = f"{percorso}.bak-identita-{datetime.datetime.now():%Y%m%d-%H%M}"
    shutil.copy2(percorso, backup)
    finale = "\n".join(righe)
    open(percorso, "wb").write((finale.replace("\n", "\r\n") if crlf else finale).encode())
    return backup


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--env", default=".env")
    ap.add_argument("--life", help="checkout di Life (default: WS_LIFE del .env)")
    ap.add_argument("--scrivi", action="store_true", help="applica (senza: anteprima)")
    args = ap.parse_args()

    env = leggi_env(args.env)
    password = os.environ.get("HUB_ADMIN_PASSWORD", "")
    if not password:
        raise SystemExit("HUB_ADMIN_PASSWORD non impostata (password admin del Network)")
    base = env.get("HUB_URL", "").rstrip("/")
    life_env = dict(os.environ)
    life_env["LIFE_FRAMEWORK_ROOT"] = args.life or env.get("WS_LIFE", "")
    life_env.setdefault("LIFE_HOME", env.get("LIFE_HOME_HOST_DIR") or str(Path.home() / ".life"))
    if env.get("LIFE_DEVICE"):
        life_env["LIFE_DEVICE"] = env["LIFE_DEVICE"]
    if env.get("LIFE_BUS_SUFFIX"):
        life_env["LIFE_BUS_SUFFIX"] = env["LIFE_BUS_SUFFIX"]
    agents, problemi = bus_agents.derive(life_env)
    for p in problemi:
        print(f"  avviso  {p}")

    admin = {"X-Admin-Password": password}
    presenti: dict[str, set[str]] = {}
    for tenant in sorted({a.tenant for a in agents}):
        q = urllib.parse.urlencode({"tenant": tenant})
        presenti[tenant] = {r["name"] for r in _req("GET", f"{base}/api/agents?{q}", admin)}

    azioni = piano(agents, presenti)
    # On the bus but without a key in this .env: key_required is on everywhere,
    # so such a listener gets 401 exactly like a missing identity.
    con_chiave = set(_coppie(env.get("HUB_AGENT_KEYS", "")))
    senza_chiave = [
        a for a in agents
        if a.name in presenti.get(a.tenant, set()) and a.name not in con_chiave
    ]
    print(f"{len(agents)} identita' calcolate, {len(agents) - len(azioni)} gia' sul bus")
    for azione, a, vecchio in azioni:
        print(f"  {azione:8} {a.tenant}/{a.name}" + (f"  (da {vecchio})" if vecchio else ""))
    for a in senza_chiave:
        print(f"  chiave   {a.tenant}/{a.name}  (sul bus, manca in HUB_AGENT_KEYS)")
    if not (azioni or senza_chiave) or not args.scrivi:
        if azioni or senza_chiave:
            print("anteprima: rilancia con --scrivi per applicare")
        return 0

    rinomine: dict[str, str] = {}
    nuove: dict[str, str] = {}

    def creatore(tenant: str) -> tuple[str, str]:
        """An identity of ``tenant`` to create from: (name, its key IN that tenant).

        The key is always read from the bus for that tenant, never matched by
        name in the .env: the same name lives in several tenants
        (`owner-telegram`), and its key would create the agent in the
        wrong one.
        """
        nomi = sorted(presenti.get(tenant, set()))
        if not nomi:
            raise SystemExit(f"{tenant}: nessun agente sul bus da cui crearne uno")
        chi = next((n for n in nomi if n.startswith("coordinator-")), nomi[0])
        return chi, chiave_di(base, tenant, chi, password)

    for azione, a, vecchio in azioni:
        q = urllib.parse.urlencode({"tenant": a.tenant})
        try:
            if azione == "rinomina":
                _req("POST", f"{base}/api/admin/agents/{vecchio}/rename?{q}", admin,
                     {"new_name": a.name})
                rinomine[vecchio] = a.name
            else:
                # The shared tenant key is retired: an agent is created by an
                # identity already in that tenant, with its own key.
                chi, sua = creatore(a.tenant)
                _req("POST", f"{base}/api/agents", {"X-Agent-Key": sua, "X-Agent-Id": chi},
                     {"name": a.name, "project": a.graph_id,
                      "device": bus_agents.suffix_of(a),
                      "description": f"{a.graph_id} su {bus_agents.suffix_of(a)} (dal grafo, #81)"})
                chiave = _req("POST", f"{base}/api/admin/agents/{a.name}/key/rotate?{q}", admin)
                nuove[a.name] = chiave["agent_key"]
            print(f"  ok       {azione} {a.name}")
        except urllib.error.HTTPError as exc:
            print(f"  ERRORE   {azione} {a.name}: HTTP {exc.code} {exc.read()[:200]!r}")

    for a in senza_chiave:
        nuove[a.name] = chiave_di(base, a.tenant, a.name, password)
        print(f"  ok       chiave {a.name}")

    riga = leggi_env(args.env).get("HUB_AGENT_KEYS", "")
    backup = scrivi_env(args.env, aggiorna_chiavi(riga, rinomine, nuove))
    print(f"HUB_AGENT_KEYS aggiornato ({len(rinomine)} rinominate, {len(nuove)} nuove); backup {backup}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
