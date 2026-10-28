# Coordinator — guida d'uso

Per chi lo usa, non per chi lo sviluppa. Il dettaglio tecnico è in `CLAUDE.md` e `.claude/docs/`.

## Cosa fa

- **Un bot Telegram unico** per tutti i progetti: ogni gruppo-forum è un progetto, ogni topic una
  scheda `(progetto, agente)` con la sua sessione. Scrivi nel topic, risponde l'agente di quel progetto.
- **Notifiche a orario** (report, promemoria, salute dei servizi, coltivazione), anche gestite dal web.
- **Ponte col bus Network**: quando un agente chiede qualcosa al proprietario, la richiesta arriva come card
  nel topic del progetto; la risposta scritta lì torna all'agente.
- **Maggiordomo**: propone task (taskgen), riorganizza lo schedule e prepara il materiale dei task in scadenza.

## Come si apre

| Cosa | Dev (PC) | Prod (minipc) |
|---|---|---|
| Bot | lo stesso bot Telegram: il PC fa da riserva, il minipc risponde | gruppo Telegram del progetto |
| UI notifiche | `npm run dev` in `frontend/` → <http://localhost:3335> | <http://gab-server:3335> (Tailscale) |
| API notifiche | <http://localhost:10001/api/health> | `:10001` sul minipc, con `X-Api-Token` |

## Casi d'uso

1. **Parlare con un progetto**: apri il suo gruppo, scegli il topic dell'agente, scrivi. `/nuovo <progetto>
   [agente] [nome]` apre una scheda nuova, `/riprendi <progetto>` riprende una sessione, `/chiudi` la chiude.
2. **Scegliere il modello di un messaggio**: prefisso `opus/high: …`, oppure `/modello` per il default della
   scheda (pulsanti provider → modello → effort). `/compara deepseek claude su: …` mette a confronto le risposte.
3. **Notifiche**: `/notifiche` dal bot, o la UI web per crearle, sospenderle e vederne lo stato di salute.
4. **Ricordare qualcosa**: un vocale o `/ricorda <testo>` finiscono nella coda del brain dell'istanza.
5. **Maggiordomo a richiesta**: `/riorganizza` (proposte sullo schedule, applichi con il pulsante),
   `/prepara` (materiale pronto per i task in scadenza), `/proponi` (proposta di un contributor).
6. **Pulizia (admin)**: `/pulisci` toglie i messaggi vuoti o doppi delle ultime 48 h; `/azzera` svuota i gruppi, con conferma.
7. **Stato di rete (admin)**: `/stato` mostra cosa sta degradando adesso (da Life `/api/health/network`): nodi degradati con la causa, nodi che non rispondono nominati, e «Life non risponde» detto esplicitamente (#97).

## Servizi LLM e da dove si cambiano

Le conversazioni nei topic usano il modello scelto da te (`/modello`). I lavori automatici invece seguono la
coda dell'istanza, in `<istanza>/config/llm-routing.json`, che si cambia dalla pagina LLM di Life (`?view=llm`)
o con `scripts/llm-run` del framework:

| Servizio | Lavoro | Coda |
|---|---|---|
| `coordinator.notificaAgente` | notifica a orario col provider agente | tipo `contesto-lungo` / facile |
| `coordinator.taskgen` | un nodo propone task da pianificare | tipo `contesto-lungo` / medio |
| `coordinator.preparazione` | materiale pronto per un task in scadenza | tipo `contesto-lungo` / medio |
| `coordinator.riorganizza` | proposte sullo schedule | tipo `giudizio` / medio; coda v1 (OpenCode Go, poi DeepSeek) dove la cella è vuota, finché Life#182 |
| `coordinator.instradaReparto` | il reparto sceglie a quale nodo passare un messaggio | come sopra |
| `coordinator.<progetto>` / categoria `sessione` | risveglio di un agente dal bus | coda della categoria `sessione` |

Se sulla macchina del bot il motore non può girare (manca il login, le chiavi o il venv di Life), questi
lavori restano sul modello del container e nei log compare `no entry of the queue runs on this host`.

## Problemi comuni

- **Risposta `❌ coda … esaurita`**: tutti i modelli della coda hanno fallito; il motivo di ciascuno è nel
  messaggio. Crediti finiti o login scaduto si sistemano sull'account, non riprovando.
- **«non ha risposto»** su una card del bus: l'agente svegliato non ha scritto nulla; la card resta aperta.
- **UI notifiche 403**: il nome con cui la apri non è fra gli host ammessi; aggiungilo a
  `FRONTEND_ALLOWED_HOSTS` nel `.env` del device e ricrea `coordinator-frontend`.
- **Il bot non risponde da nessuna parte**: due device sullo stesso token si contendono Telegram (409);
  vedi «Ruolo Telegram» in `CLAUDE.md`.
