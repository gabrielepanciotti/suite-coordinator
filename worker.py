"""Headless worker entrypoint: SSE-listen on local *-prod agents and wake claude -p.

No Telegram. One human face lives on the Hub; workers escalate via needs_human on
the bus. Auth is OAuth only (~/.claude/.credentials.json); ANTHROPIC_API_KEY is
refused because it would bill the paid API.
"""

from __future__ import annotations

import asyncio
import datetime
import json
import logging
import os
import time
from pathlib import Path

import claude_runner
import hub
import hub_config

logger = logging.getLogger("coordinator.worker")


# `run --rm`, not `docker exec`: when the credentials are dead this very worker
# refuses to start and sits in a restart loop, so there is nothing to exec into.
# The login lands in .worker-claude/ (directory mount, see docker-compose.prod.yml).
COMANDO_LOGIN = (
    "docker compose -f docker-compose.prod.yml run --rm --entrypoint claude "
    "coordinator-worker /login"
)


def stato_credenziali(path: Path) -> tuple[bool, str]:
    """``(utilizzabili, perche')`` per il file di credenziali OAuth.

    Che il file *esista* non dice niente, ed e' precisamente l'errore che e'
    costato tre mesi (issue #34): la CLI lo riscrive a ogni tentativo fallito,
    quindi sul box prod risultava modificato lo stesso minuto in cui nessuno
    riusciva ad autenticarsi. Un controllo di esistenza su un file che il
    guasto stesso tiene fresco non e' un controllo.

    Il verdetto guarda dentro: un token scaduto va benissimo *se* c'e' un
    refresh token — e' il caso normale, la CLI lo rinnova da sola. Morto e'
    scaduto **senza** modo di rinnovarsi, che e' la forma esatta osservata
    (``expiresAt: 0``, nessun ``refreshToken``).
    """
    # Un token di lunga durata (`claude setup-token`) in CLAUDE_CODE_OAUTH_TOKEN
    # autentica la CLI senza nessun file: e' la via per-device del bot, e il
    # worker sullo stesso .env deve poterla usare invece di restare in crash
    # loop con un .credentials.json vuoto (issue #34 resta coperta: senza
    # token il verdetto guarda ancora dentro il file).
    if os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", "").strip():
        return True, "token di lunga durata in CLAUDE_CODE_OAUTH_TOKEN"
    if not path.exists():
        return False, f"non esiste ({path})"
    try:
        dati = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return False, f"illeggibile ({exc})"
    oauth = dati.get("claudeAiOauth") if isinstance(dati, dict) else None
    if not isinstance(oauth, dict):
        oauth = dati if isinstance(dati, dict) else {}
    if oauth.get("refreshToken"):
        return True, "rinnovabile"
    scadenza = oauth.get("expiresAt") or 0
    try:
        scadenza = float(scadenza)
    except (TypeError, ValueError):
        scadenza = 0
    # expiresAt e' in millisecondi da quando la CLI e' passata a quel formato;
    # si accettano entrambi invece di indovinare.
    if scadenza > 1e11:
        scadenza /= 1000
    if scadenza <= 0:
        return False, "token scaduto (expiresAt=0) e nessun refresh token"
    if scadenza <= time.time():
        quando = datetime.datetime.fromtimestamp(
            scadenza, datetime.timezone.utc
        ).isoformat()
        return False, f"token scaduto il {quando} e nessun refresh token"
    return True, "valido"


def validate_auth(creds_path: Path | None = None) -> None:
    if os.environ.get("ANTHROPIC_API_KEY"):
        raise SystemExit(
            "ANTHROPIC_API_KEY is set: refused (would bill the paid API). "
            "Workers must use OAuth only."
        )
    path = creds_path or Path.home() / ".claude" / ".credentials.json"
    utilizzabili, perche = stato_credenziali(path)
    if not utilizzabili:
        # Rifiutarsi di partire, invece di partire e fallire ogni risveglio.
        # Un worker che apre gli stream SSE senza poter eseguire la CLI e' la
        # peggiore delle tre possibilita': il bus lo vede `listening: true`,
        # chi scrive non riceve nessun `delivery_warning`, la coda viene
        # svuotata davvero — e la sola traccia del guasto finisce dentro la
        # conversazione stallata. Meglio un container che non parte: e' una
        # cosa che si vede.
        raise SystemExit(
            f"credenziali OAuth inutilizzabili: {perche} ({path}).\n"
            f"Il worker NON parte: aprirebbe gli stream del bus senza poter "
            f"eseguire la CLI, e ogni risveglio fallirebbe in silenzio.\n"
            f"Rimedio (interattivo, serve la persona): {COMANDO_LOGIN}"
        )


CONTROLLO_CREDENZIALI_S = int(os.environ.get("WORKER_AUTH_CHECK_S", "900"))


async def sorveglia_credenziali(
    path: Path, intervallo: float = CONTROLLO_CREDENZIALI_S
) -> None:
    """Rileggere le credenziali finche' reggono, e morire quando non reggono.

    Controllarle solo all'avvio non basta: un worker sta su per settimane (sul
    box interno, due) e il token scade mentre gira. Dopo, il processo continua a
    tenere aperti gli stream e a dichiararsi in ascolto — che e' vero — mentre
    ogni risveglio fallisce. Tre mesi cosi', e il canale verso la produzione
    e' stato decorativo (issue #34).

    Morire e' deliberato: un guasto che si vede vale piu' di un avviso in un
    log che nessuno apre, e il riavvio del container e' l'unico segnale che
    passa attraverso `docker ps`.
    """
    while True:
        await asyncio.sleep(intervallo)
        utilizzabili, perche = stato_credenziali(path)
        if utilizzabili:
            continue
        logger.error(
            "credenziali OAuth non piu' utilizzabili: %s. Il worker si ferma: "
            "restare su significherebbe dichiararsi in ascolto sul bus senza "
            "poter rispondere. Rimedio: %s",
            perche,
            COMANDO_LOGIN,
        )
        raise SystemExit(1)


async def _main() -> None:
    cfg = hub_config.load_config()
    if cfg.role != "worker":
        raise SystemExit("worker.py requires COORDINATOR_ROLE=worker")
    if not cfg.enabled:
        raise SystemExit(
            "hub config not complete for worker (need HUB_URL, API key, HUB_LOCAL_AGENTS)"
        )
    hub.start(
        bot=None,
        run_claude=claude_runner.run_claude,
        send_to=None,
        config=cfg,
    )
    logger.info("worker up: role=%s agents=%s", cfg.role, list(cfg.local_agents))
    await sorveglia_credenziali(Path.home() / ".claude" / ".credentials.json")


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    validate_auth()
    asyncio.run(_main())


if __name__ == "__main__":
    main()
