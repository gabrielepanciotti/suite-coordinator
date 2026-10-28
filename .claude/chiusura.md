# Chiusura — opencode morto su tutti i gruppi + minipc che perde il Wi-Fi

## Cosa è stato fatto
- **opencode di nuovo funzionante** su tutti i gruppi Telegram e sui wake del bus (segnalato su uniudine). In produzione sul minipc, verificato con un turno reale.
- **Log del wake bus**: «transport failure 4/3» ora si legge «attempt 4/4» (il comportamento era corretto, l'etichetta no).
- **Test `test_cli_non_autenticata`** isolato dal registro reale su disco: falliva solo su macchine con un `hub_topics.json` lasciato da altri test.
- Fuori da questo repo (comunity `scripts/minipc/`, `ba3a727`, `ef24e26`): `wifi-watchdog` + `connectwifi` per il Wi-Fi del minipc.

## Come funziona
- La rebuild del 2026-10-08 (tailscale-pp-cli, Life#95) pre-creava `/home/app/.local/share` con `install -d -o app`, che possiede solo l'ultima cartella: `/home/app/.local` nasceva di root e opencode non poteva creare `~/.local/state` (EACCES all'avvio, ogni turno).
- La correzione (`install -d -o app /home/app/.local /home/app/.local/state`) sta **dopo** `COPY *.py`: toccare la riga originale invalidava i layer pip/whisper (~1 GB) e quel download ha bloccato due volte la chiavetta Wi-Fi del minipc.
- Minipc: `wifi-watchdog` (timer systemd, ogni minuto) tiene un tetto di banda (cake 15/8 Mbit/s), distingue con una scansione «radio bloccata» (escalation: netplan apply → reset USB → reboot, max 3 in 6h) da «rete sparita» (solo riapplica la config); `connectwifi` ricorda tutte le reti note e chiede la password.

## Struttura
- `Dockerfile` — fix ownership `~/.local` dopo `COPY *.py`
- `tests/test_dockerfile_home.py` — nuovo: genitori delle cartelle di `app` e cartelle XDG
- `hub.py` — etichetta del log dei ritentativi
- `tests/test_cli_non_autenticata.py` — store isolato su `tmp_path`
- `CLAUDE.md` — due gotcha (install -d, ordine dei layer)

## Come si prova
- `pytest tests/` → 1716 passed
- `docker exec coordinator-bot ls -ld /home/app/.local` → `app app`
- `docker exec coordinator-bot sh -c 'cd /workspaces/academicAssistant && opencode run "Rispondi solo: ok" --dir . --auto'` → `ok`
- Minipc: `wifi-watchdog status`, `journalctl -t wifi-watchdog`

## Decisioni
- Test strutturale sul Dockerfile invece di un healthcheck runtime: blocca l'errore prima della build, a costo zero.
- Fix dopo `COPY *.py` invece che sulla riga originale: stessa correzione, nessun download pesante.
- Tetto di banda invece di vietare le build sul minipc: le build restano possibili, solo più lente.
- Scartato: rifare `install -d` con `chown -R` (copre ma nasconde il problema; il test lo rende esplicito).

## Aperto
- Bot di riserva sul PC da ricostruire dopo il merge del worktree nel checkout principale.
- Chiavetta Wi-Fi nuova (chip MediaTek mt76): il reset USB software non toglie corrente.
