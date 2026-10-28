# locale/ — ciò che è di questa installazione, non del motore

Il Coordinator è un motore: non nomina aziende, host, account o repo. Dove gli
serve uno di questi valori lo chiede a `local_defaults.py`, che legge questa
cartella. Tutto ciò che qui dentro è work instance resta qui dentro (issue #43).

| File | Chi lo legge | Cosa contiene |
|---|---|---|
| `defaults.json` | `local_defaults.py` | mappa piatta `{VARIABILE_ENV: valore}`: tenant del bus, identità del mailer Graph e libreria di auth app-only (`GRAPH_APP_AUTH`, `modulo:callable`), box e chiave della sentinella, chiavi di notifica ammesse in più |
| `projects.json` | `projects.py` (`_load_default_projects`) | il registro statico di ripiego (quello che usano i worker prod con `LIFE_BACKEND_URL=off`): lista di progetti con `key`, `label`, `emoji`, `host_rel` (path **relativo a `SRV_ROOT`**, mai assoluto), `workspace`, `aliases`, `session_globs`, `kind`. Il motore aggiunge da sé solo la voce generica `claude` (~/.claude); file assente o malformato → resta solo quella |
| `sentinella_servizi.json` | a mano → `data/sentinella_servizi.json` | il catalogo dei servizi prod sorvegliati (quello vivo sta nel volume dati; l'esempio neutro è `../sentinella_servizi.example.json`) |
| `workspaces.txt` | `deploy/setup.sh` | i repo-workspace da clonare, `nome\|repo_url\|WS_*` |
| `docker-compose.dbnet.override.yml` | `WORKER_EXTRA_COMPOSE` / `COMPOSE_FILE` nel `.env` | aggancia il worker alla rete Docker su cui sta il DB dell'installazione |

## Precedenza

1. variabile d'ambiente (un device può sempre sovrascrivere);
2. `defaults.json`;
3. il default neutro del motore (quello che trova un'installazione nuova).

Le chiavi che iniziano con `_` sono commenti. Un file assente non è un errore;
uno malformato viene loggato e si ricade sui default neutri.

## Nei container

`docker-compose.yml` e `docker-compose.prod.yml` montano `./locale:/app/locale:ro`
e `./local_defaults.py` accanto ai moduli che li usano; il `Dockerfile` copia la
cartella nell'immagine. Un percorso diverso si indica con `COORDINATOR_LOCALE_FILE`.

## Un'altra installazione

Svuota o sostituisci questi file: il motore parte comunque. Senza
`SENTINELLA_SSH_HOST` la sentinella dice «non configurato» invece di dare un box
per giù; senza identità Graph il mailer si dichiara spento, e senza
`GRAPH_APP_AUTH` si spegne il solo percorso app-only.
