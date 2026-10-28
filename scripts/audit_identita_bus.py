#!/usr/bin/env python3
"""Audit delle identita' bus dichiarate nei `.env` dei repo di questo device.

PERCHE' ESISTE (issue #27, punto 1)
Il claim anti-gemello e il watcher vivono su una riga sola: `HUB_AGENT_ID` nel
`.env` del repo. Se manca, `hub-auto-claim.sh` esce in no-op silenzioso e la
sessione interattiva non claima niente; se e' SBAGLIATA, e' peggio — una
sessione aperta su un box prod che dichiara `outbound-dev-linux` claima l'identita'
di un ALTRO device e zittisce chi stava davvero rispondendo. Nessuno dei due
casi produce un errore: e' sempre e solo silenzio, cioe' la forma di guasto che
questo nodo paga piu' cara.

Un audit fatto a mano una volta invecchia il giorno dopo. Questo si rilancia:

    .venv/bin/python scripts/audit_identita_bus.py            # questo device
    .venv/bin/python scripts/audit_identita_bus.py --root /altro/percorso
    .venv/bin/python scripts/audit_identita_bus.py --offline  # senza toccare il bus

Esce 1 se ci sono anomalie, cosi' puo' stare in un controllo periodico.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import platform
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO))

# Il suffisso dice a quale device appartiene un'identita' interattiva.
# `-dev-linux` e' del motore (qualunque box Linux); gli altri sono i device di
# questa installazione, in locale/defaults.json come "modo|pattern|suffisso"
# (modo = prefisso o contiene, confronto sul path del repo).
import local_defaults  # noqa: E402

REGOLE_DEVICE: tuple[tuple[str, str, str], ...] = tuple(
    tuple(r.split("|", 2)) for r in local_defaults.get_list("BUS_SUFFISSI_DEVICE")
    if r.count("|") == 2
)
SUFFISSI_INTERATTIVI = tuple(dict.fromkeys(("-dev-linux", *(s for _m, _p, s in REGOLE_DEVICE))))
# Nomi di una stagione precedente del bus. Non sono morti: qualche script li
# legge ancora, e alcuni li preferiscono a quelli nuovi (`HUB_BASE_URL or
# HUB_URL`). Per questo un valore vecchio che DISSENTE da quello nuovo e' la
# cosa piu' pericolosa del file: lo stesso repo finisce per parlare con due bus
# diversi -- dev e prod sono tenant e dati diversi, non due indirizzi della
# stessa cosa -- e nessuno dei due da errore.
CHIAVI_LEGACY = {"HUB_BASE_URL": "HUB_URL", "HUB_AGENT_NAME": "HUB_AGENT_ID",
                 "HUB_TENANT_API_KEY": "HUB_API_KEY"}
# Porte ritirate il 2026-06-24: un HUB_URL che le usa da connection refused e i
# listener ritentano in silenzio per sempre (Network#2).
PORTE_RITIRATE = (":8031", ":3031")


def leggi_env(path: Path) -> dict[str, str]:
    valori: dict[str, str] = {}
    try:
        righe = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return valori
    for riga in righe:
        riga = riga.strip()
        if not riga or riga.startswith("#") or "=" not in riga:
            continue
        chiave, valore = riga.split("=", 1)
        valori[chiave.strip()] = valore.strip().strip("'\"")
    return valori


def identita(env: dict[str, str]) -> tuple[str, str]:
    """(agente, provenienza) — HUB_AGENT_ID, o il *:claude di HUB_LOCAL_AGENTS."""
    if env.get("HUB_AGENT_ID"):
        return env["HUB_AGENT_ID"], "HUB_AGENT_ID"
    for voce in env.get("HUB_LOCAL_AGENTS", "").split(","):
        if voce.endswith(":claude"):
            return voce.split(":", 1)[0], "HUB_LOCAL_AGENTS"
    return "", ""


def suffisso_device(host_os: str, root: Path) -> str:
    """Il suffisso che le identita' interattive DEVONO avere su questo device."""
    path = root.as_posix()
    for modo, pattern, suffisso in REGOLE_DEVICE:
        if modo == "prefisso" and path.lower().startswith(pattern.lower()):
            return suffisso
        if modo == "contiene" and pattern in path:
            return suffisso
    return "-dev-linux" if host_os == "Linux" else ""


def analizza(env: dict[str, str], atteso: str, noti: set[str]) -> list[str]:
    """Le anomalie di UN repo. Logica pura: niente rete, niente disco.

    Sta separata dalla stampa perche' e' l'unica parte che puo' sbagliarsi in
    modo interessante, e un audit di cui non ci si fida non lo guarda nessuno.
    """
    agente, _ = identita(env)
    note: list[str] = []
    if not agente:
        note.append("HUB_URL c'e' ma nessuna identita': il claim non scatta mai")
    elif agente.endswith(SUFFISSI_INTERATTIVI):
        if atteso and not agente.endswith(atteso):
            note.append(
                f"identita' di un ALTRO device ({agente}): una sessione aperta "
                f"qui ruberebbe il claim a chi risponde davvero"
            )
        if noti and agente not in noti:
            note.append(f"'{agente}' non e' registrato sul bus (403 a ogni chiamata)")
    elif noti and agente not in noti:
        note.append(f"'{agente}' non e' registrato sul bus")

    for vecchia, nuova in CHIAVI_LEGACY.items():
        if vecchia not in env:
            continue
        if nuova not in env:
            note.append(f"solo il nome vecchio {vecchia}: chi legge {nuova} non trova niente")
        elif env[vecchia] != env[nuova]:
            note.append(
                f"{vecchia}={env[vecchia]} e {nuova}={env[nuova]} non concordano: "
                f"gli script che preferiscono il nome vecchio parlano con un ALTRO bus"
            )

    url = env.get("HUB_URL", "") or env.get("HUB_BASE_URL", "")
    if any(porta in url for porta in PORTE_RITIRATE):
        note.append(f"HUB_URL su una porta ritirata il 2026-06-24: {url}")
    return note


async def agenti_del_bus() -> set[str]:
    """Chi il bus conosce davvero. Set vuoto = non si e' potuto chiedere."""
    import hub_client
    import hub_config

    url = os.environ.get("HUB_URL", "")
    if not url:
        return set()
    tenant = local_defaults.get("HUB_TENANTS", "default").split(",")[0].strip()
    agente = os.environ.get("HUB_AGENT_ID", "")
    chiave_agente = hub_config.load_agent_keys().get(agente, "") or os.environ.get(
        "HUB_AGENT_KEY", ""
    )
    client = hub_client.HubClient(
        url, os.environ.get(hub_config.api_key_env(tenant), ""), agente, chiave_agente
    )
    try:
        return {a.get("name", "") for a in await client.list_agents(tenant)}
    except Exception as exc:  # una sonda non solleva: l'audit deve finire lo stesso
        print(f"  (bus non interrogabile: {type(exc).__name__}: {exc})")
        return set()


def carica_env_coordinator() -> None:
    for riga in (_REPO / ".env").read_text(encoding="utf-8").splitlines():
        riga = riga.strip()
        if riga and not riga.startswith("#") and "=" in riga:
            k, v = riga.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip("'\""))


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--root", default=str(_REPO.parent),
                   help="cartella che contiene i repo (default: il padre di Coordinator)")
    p.add_argument("--offline", action="store_true", help="non interrogare il bus")
    args = p.parse_args(argv)

    try:
        carica_env_coordinator()
    except OSError:
        pass
    root = Path(args.root)
    atteso = suffisso_device(platform.system(), root)
    noti = set() if args.offline else asyncio.run(agenti_del_bus())

    print(f"Audit identita' bus — {root}  (device: {atteso or 'ignoto'})")
    print(f"Agenti noti al bus: {len(noti) if noti else 'non verificato'}\n")

    anomalie: list[str] = []
    for repo in sorted(p for p in root.iterdir() if p.is_dir()):
        env_file = repo / ".env"
        if not env_file.is_file():
            continue
        env = leggi_env(env_file)
        agente, fonte = identita(env)
        if not (env.get("HUB_URL") or agente or any(k in env for k in CHIAVI_LEGACY)):
            continue
        note = analizza(env, atteso, noti)

        stato = "OK " if not note else "!! "
        print(f"{stato}{repo.name:28} {agente or '(nessuna)':30} [{fonte or '-'}]")
        for n in note:
            print(f"     - {n}")
            anomalie.append(f"{repo.name}: {n}")

    print()
    if anomalie:
        print(f"{len(anomalie)} anomalie.")
        return 1
    print("Nessuna anomalia.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
