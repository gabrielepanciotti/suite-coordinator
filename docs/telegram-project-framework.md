# Spec — Telegram Project Framework (Coordinator)

> **Stato:** ✅ implementato — framework #4 chiuso; Travelers migrato al manifesto
> (`ee6baaa`: rimossi `handle_travelers_group_message` / `handle_travelers_fix` /
> `TRAVELERS_GROUP_ID`, ora serviti dal path generico + `coordinator.toml`). RicercaCasa
> in onboarding (#6, `2ae034b`). ⚠️ Le descrizioni del "codice hardcodato attuale" più
> sotto sono **storiche** (stato pre-migrazione), conservate come razionale del design.
> Spec-figlia (primo caso d'uso): `RicercaCasa/.claude/docs/fase-1f-coordinator.md`.
> **Per chi implementa:** usa `superpowers:writing-plans` per derivare il piano task-by-task.

## Obiettivo

Rendere **generico e config-driven** il pattern oggi hardcodato in Travelers: un
progetto del Coordinator può avere **un gruppo Telegram di membri** dove il bot
**notifica** e **riceve/elabora** messaggi, e dove un insieme di **capability CLI**
del progetto sono esposte come **comandi del bot**, ciascuna con uno **scope** che
ne decide la disponibilità (sala comando vs gruppo). Aggiungere un progetto o una
funzionalità deve costare **un solo file** (`coordinator.toml`), zero codice.

## Principio spina dorsale

**Tutto ciò che un progetto sa fare è un sottocomando CLI.** La CLI è l'unica fonte
di verità delle capability. Il manifesto mappa `CLI → capability del bot`; il bot le
invoca col pattern `[CMD:/nome] args` (già usato da Travelers, vedi sotto) e la
sessione `claude` del progetto esegue la CLI. Umani, comandi deterministici e LLM
passano tutti dalla stessa CLI.

## Modello in una frase

**Il Coordinator è l'unico orecchio (un solo bot, un solo polling) + il cervello
(`claude -p` nel workspace, `bypassPermissions`). Ogni progetto dichiara, in un
manifesto, il suo gruppo, le sue capability e le sue notifiche.**

Due gruppi distinti, mai mischiati:

| Contesto | Cos'è | Chi c'è | Potere |
|---|---|---|---|
| **Sala comando** (forum del Coordinator) | dashboard `(progetto, agente)` per topic | solo admin | pieno: tutta la CLI, anche cambi struttura/codice |
| **Gruppo progetto** (es. "🏠 Casa Udine") | un gruppo per progetto | membri fidati | ristretto: solo capability `group`/`both` |

---

## Due modelli di binding — `group_chat_id` + `group_thread_id`

Un progetto può vivere in Telegram secondo **due modelli**, scelti nel `[telegram]`
del manifesto. Discriminante: `group_thread_id` (default `0`).

| Modello | `[telegram]` | Routing | Quando |
|---|---|---|---|
| **Gruppo dedicato** | `group_chat_id` = gruppo suo, `group_thread_id = 0` | `seed_group_bindings` lega il topic `0` **e** registra un `MessageHandler` chat-wide (`filters.Chat(chat_id)`) → tutto il gruppo = un progetto | progetti **personal**: notifiche + ingresso "raw" per il vault, audience separata (es. ChefToni = famiglia) |
| **Forum + topic** | `group_chat_id` = forum condiviso "Coordinator Work", `group_thread_id` = id del topic | `seed_group_bindings` lega `(chat_id, thread_id)`; **nessun** handler chat-wide (più progetti condividono il `chat_id`, si calpesterebbero) → instrada il generico `handle_message` via `TOPIC_BINDING[chat:topic]` | progetti di **lavoro** + il **Coordinator stesso**: un topic ciascuno nel forum |

**Policy ibrida (default del progetto):** forum condiviso con un topic per i progetti
di lavoro e per la sala regia del Coordinator; gruppo dedicato **solo** quando
l'audience è davvero separata (progetti personal). Il bot, se admin con
`can_manage_topics`, **crea i topic del forum via `createForumTopic`** — nessun
gruppo nuovo da creare a mano per i progetti di lavoro.

`group_bindings()` (`manifest.py`) ritorna `(group_chat_id, group_thread_id,
project_key, agent)`; salta i progetti con `group_chat_id` assente → un manifesto
senza chat_id è **fail-safe** (nessun binding, nessuna notifica recapitata).

---

## Cosa esiste già (riuso) — con riferimenti

Il framework **non inventa**: generalizza pezzi già funzionanti.

- **Handler gruppo → `claude`**: `handle_travelers_group_message` (`bot.py:1916`)
  fa mirror del messaggio (testo/foto/documento) in `vault/raw/...`, scarica gli
  allegati, **poi** passa tutto alla sessione `claude` del progetto con un prompt
  strutturato `[CMD:/ingest-telegram] mittente:… Testo:… Allegato (leggilo e
  interpretalo): <path>` (`bot.py:1978-1988`) e risponde nel gruppo via
  `_invoke_and_reply`.
- **Pattern `[CMD:/nome] args`**: il ponte comando→capability esiste già (sopra).
- **Gruppo trusted, nessun gate per-utente**: `handle_travelers_group_message`
  documenta e applica `we do NOT gate on ALLOWED_USER_IDS` (`bot.py:1922-1923`).
  → **appartenenza al gruppo = autorizzazione**.
- **Slash-command scoped per-chat**: `/fix` registrato **solo** per il gruppo
  Travelers con `filters.Chat(chat_id=TRAVELERS_GROUP_ID)` (`bot.py:2131-2136`,
  handler `handle_travelers_fix` `bot.py:1999`). È esattamente lo scope per-chat.
- **Invio su topic**: `_send_chunks_thread(bot, chat_id, thread_id, text)`
  (`bot.py:878`) sa già passare `message_thread_id`. (Il path notifiche invece usa
  `_send_chunks_to` `bot.py:859`, **senza** thread — vedi gap.)
- **Routing `(chat_id, topic_id) → (progetto, agente)`**: `TOPIC_BINDING`
  (`bot.py:143`), `_topic_id` (`bot.py:408`, gruppo non-forum / privato → `0`),
  `_topic_binding` (`bot.py:420`), persistito in `data/state.json` come
  `"chat_id:topic_id": [project_key, agent_name]`.
- **Notifiche schedulate**: `Notification` (`notifications.py:29-53`), provider
  `agent|static|growbox` (`providers.py`), scheduler APScheduler in-process
  (`scheduler.py:41-91`), store `data/notifications.json`, gestione `/notifiche`.
- **Sessione `claude` per workspace**: `run_claude(prompt, *, workspace, persona,
  resume_id)` (`claude_runner.py:118`), `--permission-mode bypassPermissions`
  (`claude_runner.py:139-145`).
- **Registro progetti**: `Project(key,label,emoji,host_path,workspace,aliases,
  session_globs,kind)` (`projects.py:24-34`), override per-device
  `projects.local.json`, mount docker-compose `${WS_<X>}:/workspaces/<x>`.
- **Funzionalità**: `Functionality(key,label,agent,companions,seed)`
  (`functionalities.py:23-29`), discovery da `<workspace>/.claude/functionalities.toml`
  (`functionalities.py:40-77`).

---

## Il manifesto di progetto — `<workspace>/.claude/coordinator.toml`

Scoperto come `functionalities.toml` (per-workspace, cache su mtime). È l'unico
punto da toccare per aggiungere/cambiare l'integrazione Telegram di un progetto.

```toml
[telegram]
group_chat_id = -100xxxxxxxxxx      # gruppo membri del progetto (trusted). Opzionale:
                                    # senza, il progetto vive solo in sala comando + DM.
bot           = "home"              # QUALE bot presidia la stanza (default: il bot storico
                                    # unico). Vedi "Piu' bot" sotto: dichiaralo solo se quel
                                    # bot e' davvero membro del gruppo.
agent         = "planner"           # persona/agente della sessione di gruppo (slug in .claude/agents)
pin           = "Tabella: {sheet_url}"   # messaggio da tenere pinnato (opzionale; {var} risolte dal progetto)

# --- routing: cosa rende un messaggio "di questo nodo" (cluster) -------------
[routing]
domains  = ["immobiliare.it", "idealista.it"]   # segnale FORTE: un match decide
keywords = ["affitto", "canone", "monolocale"]  # segnale DEBOLE: ordina, non decide

# --- capability: una CLI esposta come comando del bot -----------------------
[[capability]]
key   = "aggiungi"
label = "➕ Aggiungi a tabella"
cli   = "python -m casa_udine.cli ingest-url {url}"   # {placeholder} riempiti dagli arg del comando
scope = "both"                       # command-room | group | both
hint  = "Incolla il link di un annuncio."             # opzionale, per /help e menu

[[capability]]
key   = "refactor"
label = "🛠 Cambio struttura"
cli   = "..."
scope = "command-room"               # NON disponibile nel gruppo

# --- notifiche: cron, con destinazione configurabile ------------------------
[[notification]]
key      = "riepilogo"
title    = "Riepilogo annunci"
provider = "agent"                   # agent | static | <futuro: cli>
schedule = "0 8,18 * * *"            # crontab 5 campi
to       = "group"                   # group | dm | chat:<id>[:<thread>]
prompt   = "Genera il riepilogo dei nuovi annunci di oggi."   # provider=agent
```

### Schema

- `[telegram]`: `group_chat_id` (int, opz.), `bot` (str, opz., default: il bot
  storico unico), `agent` (str, opz., default `generale`), `pin` (str template, opz.).
- `[routing]` (opz.): `domains` (lista di host), `keywords` (lista di parole).
  Serve solo ai nodi **figli di un cluster**; un nodo con stanza propria non ne
  ha bisogno. Un blocco malformato viene **scartato per intero** con un warning:
  meglio nessun routing che un routing a metà che instrada altrove in silenzio.
- `[[capability]]`: `key`, `label`, `cli` (str con `{placeholder}`), `scope`
  (`command-room|group|both`, default `command-room`), `hint` (opz.).
  Una capability con `label` non vuota e scope che include `group` appare anche
  nel menu "➕ Nuova"/help del gruppo. **`[[capability]]` generalizza
  `[[functionality]]`**: una functionality è una capability con menu+seed; la
  discovery vecchia resta valida (retrocompat), le due liste si fondono.
- `[[notification]]`: campi attuali di `Notification` (`key,title,provider,
  schedule`) **+ `to`** (destinazione). `prompt`/`text` vivono come oggi
  (in `params` o inline a seconda del provider).

### Provider `command` — job deterministici, nessun LLM (#84)

Un lavoro del nodo che e' gia' uno script (nessuna decisione da prendere) non deve
passare da una sessione Claude: costa, e puo' inventare. Il provider `command` lancia
il comando e ne inoltra l'esito.

```toml
[[notification]]
key         = "<nodo>-<job>"
title       = "..."
provider    = "command"
schedule    = "0 7 * * *"
to          = "dm"
recipients  = ["owner"]
command     = "sh scripts/pyrun scripts/scheduler_push.py finestra --apply"
timeout_sec = 300
```

- **cwd = repo del nodo** (nel container `/workspaces/<nodo>`); `project` e' il nodo
  del manifest. Stesso modo di eseguire del `gate` (shell, stdout/stderr separati).
- **exit 0** con stdout non vuoto -> lo stdout e' il messaggio; stdout vuoto -> silenzio
  voluto, niente invio.
- **exit != 0** -> messaggio `⚠️ «titolo» fallito (exit N)` + coda dello stderr (ultime
  20 righe, max 1500 caratteri; stderr vuoto -> coda dello stdout). Mai silenzio.
- **timeout** (`timeout_sec`, default 300, ammessi 1..3600) -> processo ucciso (con i
  suoi figli) e messaggio `⚠️ «titolo» scaduto dopo N s`. Anche progetto non risolvibile,
  `command` vuoto o comando non lanciabile danno un messaggio rumoroso.
- L'output e' escapato (Telegram HTML): `<` e `&` nello stdout non rompono l'invio. I
  testi lunghi li spezza l'invio in chunk da 4096.
- **Solo da manifest**: esegue shell, quindi non si crea ne' si modifica da chat o dalla
  web UI (API: 403 su create/preview/cambio di provider/params; il wizard `/notifiche`
  non lo offre; `GET /api/meta` non lo elenca). Abilitare/disabilitare e `POST /{key}/test`
  restano permessi. Il comando e' quello versionato nel `coordinator.toml` del nodo.

---

## Più bot, e i cluster — `bot`, `COORDINATOR_BOT_*`, `[routing]`

Il modello originale aveva **un solo bot** per tutti i progetti. Oggi ogni dominio
può averne uno proprio, e un **cluster** (un nodo del grafo con figli) può possedere
una stanza sola per tutta la famiglia. Tre variabili d'ambiente, tutte gitignored:

```
COORDINATOR_BOTS=chef,travel,home,health,scheduler   # quali bot esistono
TELEGRAM_BOT_TOKEN_<NAME>=…                          # il token di ciascuno
COORDINATOR_BOT_NODE_<NAME>=<chiave progetto>        # opz.: il suo workspace…
COORDINATOR_BOT_ROOT_<NAME>=<id nodo del grafo>      # opz.: …e da dove parte il menu
```

**`NODE` e `ROOT` non sono la stessa cosa**, ed è la distinzione che costa più cara
quando si sbaglia:

- `NODE` è un **progetto**: un workspace su disco, e **deve risolvere nel registro**.
  È dove gira il lavoro. Se non risolve, l'avvio si rifiuta di dire `ready` e nomina
  la variabile da correggere.
- `ROOT` è un **nodo del grafo**: può essere un cluster senza repo. È dove comincia
  il menu `/start`. Se manca dal grafo, il menu sarebbe vuoto — e l'avvio lo dice.

Entrambi sono **opzionali quando il nome del bot coincide già** con la chiave giusta
(il caso di `home`). Configurazione che ripete un default è configurazione che deriva:
non scriverla.

### Come un cluster sceglie il figlio

Il bot del cluster legge il `[routing]` di ogni discendente e assegna un punteggio:
**dominio = 100, parola = 1**. Il rapporto è deliberato — un link a un portale non è
ambiguo, mentre le parole sono condivise fra fratelli ("casa" appartiene a tutti i
nodi del cluster). Quattro esiti, e nessuno è il silenzio:

| esito | quando | cosa fa |
|---|---|---|
| `routed` | un candidato stacca gli altri | apre la sessione su quel nodo |
| `ambiguous` | pareggio fra fratelli | **chiede con i bottoni**, e parcheggia il messaggio |
| `root` | nessun segnale | risponde il cluster stesso |
| `unbuilt` | il nodo esiste nel grafo ma non su disco | lo nomina e dice dove finirebbe il lavoro |

Un thread già legato si **ri-instrada solo su prova forte** (un dominio rivendicato da
un altro nodo), mai su un cambio d'argomento a parole: dentro una conversazione aperta
la continuità vale più di una singola frase.

Regola operativa che vale più di tutto lo schema: **`bot` si dichiara solo quando quel
bot è davvero dentro la stanza**. Dichiararne uno che non è membro non sposta la
risposta su di lui — fa tacere il bot di default, e la stanza resta muta senza dire
perché. Si verifica con `getMe` (`can_read_all_group_messages`) e `getChat`, non a
memoria.

---

## Capability & scope — come si fa rispettare

Lo scope è **pratico, non sandbox crittografica** (adatto a gruppi privati fidati):

1. **Registrazione comandi per-chat**: una capability `command-room` non viene
   registrata come comando nel gruppo (come oggi `/fix` esiste solo nel gruppo
   Travelers). Nel gruppo sono raggiungibili solo `group`/`both`.
2. **Persona dell'agente di gruppo**: l'agente legato al gruppo (es. "casa", come
   "Gonzalo" per Travelers) ha una persona ristretta che non esegue cambi
   strutturali anche se richiesti a parole.
3. **Una stanza, un bot che risponde**: dove un nodo ha un bot dedicato
   (`telegram.bot`), le sue capability **non** restano raggiungibili anche dal
   Coordinator in quella stanza. Le `group` lo erano già per costruzione (hanno
   un `filters.Chat`); le `command-room`/`both` no — si registravano senza alcun
   filtro, quindi i due token rispondevano **entrambi** allo stesso `/cmd`.
   Successo davvero: `/contatta` in "Casa 🕌" produceva due bozze per una
   richiesta sola (2026-08-12). Oggi quegli handler si **restringono** invece di
   sparire (`~filters.Chat(moved_chats)` in `_register_default_handlers`), come
   già facevano i catch-all: muti nelle stanze passate al bot dedicato, intatti
   ovunque altro, così la command room del Coordinator continua a funzionare.

   ⚠️ Vale anche per un nodo **senza stanza propria**: HomeResearcher non ha
   `group_chat_id` (la stanza è del padre `Home`), quindi la guardia che
   riguardava solo il ramo di gruppo non sarebbe scattata comunque. La stanza da
   silenziare è quella del nodo *che ce l'ha* e che dichiara lo stesso bot —
   `moved_chats` si calcola da `group_bindings()` su tutto l'insieme
   `reassigned`, non dal singolo progetto.

> **Hardening futuro (fuori scope ora):** per gruppi non fidati (clienti) si potrà
> far girare la sessione di gruppo con tool-permessi ristretti (`--allowedTools` /
> permission-mode non-bypass) invece del bypass globale. Oggi `claude_runner` usa
> sempre `bypassPermissions`; la differenziazione per-contesto è un'estensione
> separata, da non costruire in questa fase.

### Il ponte comando → capability

Un comando del bot (slash o voce di menu) si traduce in un prompt
`[CMD:/<key>] <args>` per la sessione `claude` del progetto (identico al pattern
Travelers `[CMD:/ingest-telegram] …`). Il `CLAUDE.md`/persona del progetto e/o una
riga del manifesto insegnano alla sessione la mappa `key → cli`. La sessione esegue
la CLI via Bash e risponde nel contesto. Così **la stessa CLI** serve umano, bot e
LLM, e lo scope è solo "quali key registro in quale chat".

---

## Notifiche — due primitive, una destinazione

- **Cron (Coordinator)**: `[[notification]]` schedulate, già esistenti, + campo
  `to`. Per "riepilogo alle 8 e alle 18".
- **A evento (il progetto spinge)**: il progetto, quando succede qualcosa (es.
  scan che trova un ottimo annuncio), **invia direttamente** col token condiviso
  alla destinazione. Solo invio → nessun conflitto 409.

**Risoluzione della destinazione `to`** (condivisa fra le due primitive):

| `to` | risolve a |
|---|---|
| `dm` | chat 1:1 di ogni user id dei profili `recipients` (comportamento attuale) |
| `group` | `[telegram].group_chat_id` del progetto |
| `chat:<id>[:<thread>]` | chat/topic esplicito |

---

## Componenti da costruire (i 5 pezzi "Nuovo")

Tutti nel repo `$SRV_ROOT/Coordinator`. Ordine suggerito.

### 1. Discovery del manifesto `coordinator.toml`
- Modulo nuovo (es. `manifest.py`) che, per ogni `Project`, carica
  `<workspace>/.claude/coordinator.toml` (riusa il pattern cache+mtime di
  `functionalities.py:40-77`). Espone: `telegram_of(project)`, `capabilities_of(project)`,
  `notifications_of(project)`.
- Un manifesto malformato logga warning e si ignora (non rompe l'avvio), come le
  functionalities.

### 2. Handler gruppo generico (generalizza Travelers)
- Estrarre da `handle_travelers_group_message` (`bot.py:1916`) un
  `handle_project_group_message` parametrico sul progetto.
- Costruire all'avvio una mappa `{group_chat_id: project_key}` da **tutti** i
  manifesti (al posto del singolo `TRAVELERS_GROUP_ID`). Registrare il group-handler
  con `filters.Chat(chat_id=<ognuno>)` (come `bot.py:2126-2136`).
- Mantenere la semantica trusted (no gate per-utente) e il pre-seed del binding
  `(group_chat_id, 0) → (project, agent)` in `TOPIC_BINDING`/`state.json`.
- **Travelers diventa il primo consumer**: `TRAVELERS_GROUP_ID` + `travelers/gonzalo`
  espresso come `coordinator.toml` in `$SRV_ROOT/Travelers`, l'handler hardcodato si
  ritira (retrocompat: se l'env è ancora settato, va bene, ma la via maestra è il
  manifesto).

### 3. Registrazione dinamica delle capability come comandi (con scope)
- All'avvio, per ogni progetto con `group_chat_id`, registrare le capability
  `group`/`both` come comandi nel gruppo; le `command-room`/`both` come comandi nel
  forum. Ogni comando produce il prompt `[CMD:/<key>] <args>` → `_invoke_and_reply`.
- Esporre le capability anche in `/help` contestuale e nel menu "➕ Nuova".

### 4. Campo `destination` sulle notifiche + send-path su topic
- Aggiungere `to` a `Notification` (`notifications.py:29-53`) + `from_dict`/`to_dict`.
  Default `dm` (retrocompat: notifiche esistenti restano DM).
- Nel `_fire`/send-path dello scheduler (`scheduler.py:70-91`, callable iniettato a
  `bot.py:2051`): risolvere `to`. Per `group`/`chat:` usare
  `_send_chunks_thread` (`bot.py:878`) invece di `_send_chunks_to` (che non ha
  thread). Aggiungere un secondo callable iniettato (`send_to_thread`) o
  generalizzare quello esistente.
- Aggiornare il wizard `/notifiche` per chiedere anche la destinazione.

### 5. Helper `pinChatMessage`
- Helper basso livello che invia il messaggio `[telegram].pin` (con `{var}` risolte
  dal progetto) e lo **pinna** nel gruppo (`bot.pin_chat_message`). Oggi non esiste
  alcun `pin_chat_message` in `bot.py`.
- Politica: re-pin idempotente (se il testo pin cambia, sostituisci; altrimenti
  lascia). Esposto come capability/azione invocabile dal progetto.

---

## La skill — `~/.claude/skills/coordinator-project-setup/`

Onboarding identico per ogni progetto. Passi guidati:

1. **Crea il gruppo** Telegram, aggiungi il bot del Coordinator, rendilo admin.
2. **Cattura il `chat_id`** del gruppo (istruzioni: `getUpdates` o messaggio di
   probe).
3. **Scrivi `coordinator.toml`** nel workspace del progetto (`[telegram]` +
   `[[capability]]` mappate alla CLI + `[[notification]]`).
4. **Registra il progetto** nel Coordinator se non c'è: voce in `projects.local.json`
   + mount `WS_<X>` in `docker-compose.yml`/`.env`.
5. **Mappa le capability**: verifica che ogni `cli` esista (`--help`); se manca,
   segnala cosa creare lato progetto.
6. **Reload** del Coordinator (`docker compose up -d` / restart) e smoke-test
   (un comando nel gruppo, una notifica di prova).

---

## Decomposizione → 3 issue + build order

1. **Coordinator framework** (questo doc) — i 5 pezzi. Repo `Coordinator`.
2. **Skill `coordinator-project-setup`** — repo `~/.claude` (shared).
3. **Istanza RicercaCasa** — spec-figlia. Repo `RicercaCasa`.

**Ordine:** 1 → 3 (la 2 in parallelo). La 1 sblocca tutto. Travelers va migrato al
manifesto come parte della 1 (è il primo consumer e il test di non-regressione).

## Fuori scope (annotare)

- Differenziazione del permission-mode per-contesto (gruppo ristretto vs forum
  bypass) — hardening futuro per gruppi non fidati.
- Provider notifica `cli` (eseguire una CLI e mandarne l'output) — utile, ma oggi
  `agent`/`static` bastano; aggiungere quando serve.
- Webhook (resta polling).

## Domande aperte

- **Persistenza del binding di gruppo**: pre-seed all'avvio dai manifesti vs comando
  admin `/bind`. Proposta: pre-seed all'avvio (deterministico, niente passi manuali).
- **Override del binding di gruppo se un membro apre una sessione diversa**: nel
  gruppo il binding è fisso al progetto del manifesto (un gruppo = un progetto);
  niente dashboard `/progetti` dentro i gruppi progetto.
