# .worker-claude — home Claude minimale per il worker headless

Montata su `/home/app/.claude` nel container `coordinator-worker`.

**Perché esiste:** caricare la `~/.claude` completa dell'utente come `user`
setting-source attiva, ad ogni cold-start di `claude -p`, gli hook SessionStart
(es. `session-start-backsync.sh`) e il sync dei ~30 marketplace plugin. In un
container headless questo va in loop e blocca la sessione prima che possa
rispondere sul bus via `/hub-send`. Il worker è l'executor, non la chat face:
non gli serve nessuno di quei macchinari.

**Cosa contiene:** solo `settings.json` (vuoto → zero hook, zero plugin).
Le due cose che servono davvero sono montate *live* dall'host (vedi
`docker-compose.prod.yml`), così restano single-source-of-truth:

- OAuth: **non** e' piu' montato dall'host. Vive qui (`.credentials.json`, gitignored), del worker: un file montato da solo non si puo' sostituire con `rename()` (EBUSY) e Claude Code perdeva in silenzio login e refresh. Login: `docker compose -f docker-compose.prod.yml run --rm --entrypoint claude coordinator-worker /login`
- `${CLAUDE_HOST_DIR}/skills/hub-send`   → la skill `/hub-send` (RO)

Il docker CLI vive nell'immagine ed è disponibile per i task di deploy (parla col
docker engine dell'host via socket montato). **git NON è installato di proposito**:
i workspace sono bind-mount Windows su Docker Desktop, dove i metadati viaggiano a
~1 file/s (un `git status` su 562 file ha misurato 530s). Con git nel PATH, il
`git status` automatico di Claude Code all'avvio supererebbe il timeout idle e
ucciderebbe ogni turno. Senza git quel probe è saltato → turni veloci; info
commit/branch si leggono dai file in `.git`. Pull/commit avvengono lato host
(git nativo, veloce). Vedi il commento nel `Dockerfile`.
