"""Ciò che viene detto a un bot in una stanza non deve vivere solo nella sessione.

Il guasto che questo modulo uccide, misurato il 2026-09-06. Gab ha chiesto tre
volte nel gruppo «Casa 🕌» di cambiare la bozza di contatto. Il bot ha risposto
di sì tutte e tre, e nessuna delle tre era arrivata sul repo del PC:

* le prime due erano state applicate davvero, ma erano rimaste **non
  committate** dentro il container (`.claude/commands/group-message.md`,
  `manda.md`, un nuovo esempio di stile) — cioè visibili solo lì dentro, dove
  nessuno le guarda;
* la terza era ancora solo nella sessione ``opencode``, che il resume ha poi
  buttato: persa del tutto.

Da fuori le tre cose sono indistinguibili — il bot dice «fatto» in tutti i casi.
Quindi servono due garanzie separate, e sono due funzioni:

``registra``   il testo grezzo finisce in un diario append-only nel repo del
               nodo, PRIMA che il modello parta. Anche se il turno muore, se il
               modello capisce male o se la sessione viene buttata, la frase
               esiste ancora e la si può rileggere.
``pubblica``   a turno finito, tutto ciò che è sporco in quel workspace viene
               committato e spinto. È ciò che mancava: il lavoro fatto dentro il
               container arriva così su ogni device, invece di restare lì.

Nessuna delle due solleva mai: un errore di git o un disco pieno non devono far
fallire una risposta su Telegram. Falliscono a voce, nel log.
"""
from __future__ import annotations

import logging
import os
import subprocess
from datetime import datetime
from pathlib import Path

logger = logging.getLogger("coordinator.indicazioni")

#: Il diario, relativo alla radice del workspace. Versionato di proposito: è la
#: memoria che deve sopravvivere alla sessione, quindi deve viaggiare col repo.
DIARIO = Path(".claude") / "indicazioni.md"

INTESTAZIONE = """\
# Indicazioni ricevute in chat

Append-only, scritto dal Coordinator **prima** che il modello risponda: qui c'è
ciò che è stato chiesto, testuale, anche quando la risposta è andata storta.
Non si edita a mano — si legge, si applica al codice e si lascia dov'è.
"""

#: Oltre questa lunghezza il messaggio viene tagliato: il diario è un registro di
#: indicazioni, non un posto dove incollare un documento intero.
MAX_CARATTERI = 4000

_TIMEOUT_GIT = 60

#: Oltre questa dimensione un file NUOVO non entra nel commit automatico (#96): 9 PDF
#: scaricati nel gruppo erano finiti in git (~740 MB) e GitHub rifiuta i file sopra 100 MB.
MAX_FILE_MB = float(os.environ.get("INDICAZIONI_MAX_FILE_MB", "20"))


def registra(workspace: str | Path, *, autore: str, testo: str,
             stanza: str = "", nodo: str = "") -> Path | None:
    """Appende il messaggio al diario del nodo. Ritorna il file, o ``None``.

    Si chiama **prima** di far partire il modello, non dopo: il momento in cui
    l'indicazione si perde è proprio quello in cui il turno fallisce.
    """
    testo = (testo or "").strip()
    if not testo:
        return None
    try:
        percorso = Path(workspace) / DIARIO
        percorso.parent.mkdir(parents=True, exist_ok=True)
        nuovo = not percorso.exists()
        if len(testo) > MAX_CARATTERI:
            testo = testo[:MAX_CARATTERI] + "\n\n[…troncato]"
        quando = datetime.now().strftime("%Y-%m-%d %H:%M")
        voce = [f"\n## {quando} — {autore}"]
        contesto = " · ".join(p for p in (stanza, nodo) if p)
        if contesto:
            voce.append(f"<sub>{contesto}</sub>\n")
        voce.append(testo + "\n")
        with percorso.open("a", encoding="utf-8") as fh:
            if nuovo:
                fh.write(INTESTAZIONE)
            fh.write("\n".join(voce))
        return percorso
    except OSError:
        # Il diario è una rete di sicurezza: se cade lei, la risposta va data
        # lo stesso. Si urla nel log e si tira dritto.
        logger.exception("diario non scritto (workspace=%s)", workspace)
        return None


def pubblica(workspace: str | Path, *, motivo: str) -> bool:
    """Committa e spinge tutto ciò che è sporco nel workspace. Mai solleva.

    ⚠️ `git add -A`, deliberatamente, ed è l'opposto della regola che vale sulle
    sessioni interattive del PC (dove si committano solo i file della propria
    sessione, per non mischiare il lavoro di sessioni parallele). Qui la
    situazione è rovesciata: nel container gira un turno per volta e nessuno
    guarda mai `git status`, quindi un file lasciato indietro non viene ripreso
    da nessuno — resta invisibile per settimane, che è esattamente com'è nato
    questo modulo.
    """
    radice = Path(workspace)
    try:
        sporco = _git(radice, "status", "--porcelain")
        if sporco is None:
            return False
        if not sporco.strip():
            return False
        if _git(radice, "add", "-A") is None:
            return False
        # `add -A` takes whatever the repo's .gitignore lets through: on
        # 2026-09-29 that was three backups of backend/.env.production, pushed
        # to GitHub. Anything that looks like a secret leaves the index here,
        # whatever the repo ignores or forgets to.
        messi = (_git(radice, "diff", "--cached", "--name-only", "-z") or "").split("\0")
        segreti = [f for f in messi if f and sembra_un_segreto(f)]
        if segreti:
            _git(radice, "reset", "-q", "--", *segreti)
            logger.warning(
                "workspace %s: esclusi dal commit automatico perche' sembrano segreti: %s",
                radice, ", ".join(segreti),
            )
            if not (_git(radice, "diff", "--cached", "--name-only") or "").strip():
                return False
        nuovi = (_git(radice, "diff", "--cached", "--name-only", "--diff-filter=A", "-z") or "").split("\0")
        grandi = [f for f in nuovi if f and _troppo_grande(radice / f)]
        if grandi:
            _git(radice, "reset", "-q", "--", *grandi)
            logger.warning(
                "workspace %s: esclusi dal commit automatico perche' oltre %g MB: %s",
                radice, MAX_FILE_MB, ", ".join(grandi),
            )
            if not (_git(radice, "diff", "--cached", "--name-only") or "").strip():
                return False
        if _git(radice, "commit", "-m", f"chore: {motivo}") is None:
            return False
        ramo = (_git(radice, "rev-parse", "--abbrev-ref", "HEAD") or "").strip()
        if not ramo or ramo == "HEAD":
            logger.warning("workspace %s in detached HEAD: committato, non spinto", radice)
            return False
        if _git(radice, "push", "origin", ramo) is None:
            # Il commit c'è comunque: il lavoro non è perso, solo non ancora
            # propagato. Il push riuscirà al turno dopo.
            logger.warning("push fallito su %s (%s): riprovo al prossimo turno", radice, ramo)
            return False
        logger.info("workspace %s: lavoro committato e spinto su %s", radice, ramo)
        return True
    except Exception:
        logger.exception("pubblicazione fallita (workspace=%s)", radice)
        return False


_SEGRETI_ESATTI = {".credentials.json", "credentials.json", "id_rsa", "id_ed25519"}
_SEGRETI_SUFFISSI = (".pem", ".key", ".p12", ".pfx", ".jks", ".keystore")


def sembra_un_segreto(percorso: str) -> bool:
    """Is this repo-relative path something an automatic commit must never push?

    `.env` and every variant of it (`.env.production`, `.env.production.bak-…`)
    except the documented templates (`.env.example`, `.env.*.example`).
    """
    nome = percorso.replace("\\", "/").rsplit("/", 1)[-1].lower()
    if nome in _SEGRETI_ESATTI or nome.endswith(_SEGRETI_SUFFISSI):
        return True
    if nome == ".env" or nome.startswith(".env."):
        return not (nome.endswith(".example") or ".example." in nome or nome.endswith(".sample"))
    return False


def _troppo_grande(percorso: Path) -> bool:
    try:
        return percorso.stat().st_size > MAX_FILE_MB * 1024 * 1024
    except OSError:
        return False


def _git(radice: Path, *args: str) -> str | None:
    """`git` nel workspace: l'output, oppure ``None`` se è andata male."""
    try:
        esito = subprocess.run(
            ("git", "-C", str(radice), *args),
            capture_output=True, text=True, timeout=_TIMEOUT_GIT,
        )
    except (OSError, subprocess.SubprocessError):
        logger.exception("git %s non eseguito in %s", args[0], radice)
        return None
    if esito.returncode != 0:
        logger.warning("git %s fallito in %s: %s", args[0], radice,
                       (esito.stderr or "").strip()[:300])
        return None
    return esito.stdout
