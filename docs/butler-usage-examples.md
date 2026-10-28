# Maggiordomo — esempi d'uso per nodo e use-case pratici

> Companion di `.claude/docs/butler.md`. Esempi CONCRETI di cosa producono i tre sottosistemi
> (taskgen / riorganizza / preparazione) per ogni nodo personale, e scenari end-to-end reali.

## 0. Come si legge

Ogni nodo espone un dominio. Il maggiordomo lo sveglia (taskgen), gli chiede di riorganizzare il
tempo (Slice A), o pre-esegue lo specialista (Slice C). Il routing categoria→specialista oggi è
`task_routing.CATEGORY_NODE_MAP` (`cucina→personalChef`, `funghi→mycorise`; fallback
`scheduler/generale`). **Per abilitare la preparazione (Slice C) su un nuovo nodo** bisogna aggiungere
la sua categoria alla mappa — vedi §Estendere in fondo.

## 1. Esempi per nodo

### ChefToni (cucina) — `personalChef`
- **taskgen** (seed ChefToni, cron 11:00): ispeziona dispensa/scadenze e propone
  ```json
  {"suggestions":[{"title":"Cena con le zucchine","category":"cucina",
    "when_iso":"2026-07-10T20:00:00+02:00","duration_min":45,
    "description":"Le zucchine scadono domani","why":"scadenza ingrediente"}]}
  ```
  → categoria `cucina` fidata → task one-off → card Slice A alle 19:50.
- **riorganizza**: 3 task di cucina sparsi lun/mer/ven → proposta
  `{"kind":"accorpa","st_ids":[11,14,19],"new_start_iso":"2026-07-15T19:00:00+02:00",
    "rationale":"batch cooking: cucini una volta per 3 pasti"}` → card `[Applica]`.
- **preparazione** (T-6h): per "Cena con le zucchine" pre-genera **ricetta + lista spesa** →
  bottone `[📋 Preparazione]` sulla card: apri e hai già ricetta e cosa manca.

### Mycorise (funghi / growbox) — `mycorise`
- **taskgen** (pre-gate "dati", gate_cmd = probe HA): svegliato SOLO se il trend umidità non sale
  → `{"suggestions":[{"title":"Controlla umidificatore","category":"funghi",
    "when_iso":"...","duration_min":10,"description":"umidità ferma all'80% da 6h",
    "why":"il secchio potrebbe essere vuoto"}]}` → task.
  NB: il growbox è per lo più automatico; il maggiordomo interviene solo su anomalie di trend
  (non su soglie istantanee), come da direttiva utente.
- **preparazione**: per "Raccolta funghi programmata" pre-genera **checklist raccolta** (sterilità,
  contenitori, peso atteso).

### Scheduler (casa / generale) — fallback `scheduler/generale`
- **taskgen**: promemoria domestici generici (bollette, manutenzioni) → task categoria generica.
- **riorganizza**: è il nodo che più beneficia dell'accorpamento (task casa eterogenei) →
  `{"kind":"sposta","st_ids":[42],"new_start_iso":"...","rationale":"sposto la spesa al sabato mattina,
    slot libero e vicino agli altri task casa"}`.
- **preparazione**: oggi SKIP (categoria generica = fallback). Abilitabile mappando categorie casa
  specifiche a un agente Scheduler dedicato.

### Altri nodi (Cosmos, Travelers/Travel, RentFinder, UniUdine, …)
Oggi la **preparazione** li salta (nessuna categoria mappata → fallback). Sono comunque bersaglio di:
- **taskgen** (se registri un watcher col loro `node_key` in `taskgen.config.json`): es. UniUdine →
  "Ripassa per esame X", Travel → "Prenota treno per la gita di sabato".
- **riorganizza** (globale sullo schedule della persona, categoria-agnostica).
Per la preparazione mirata, aggiungi la categoria del nodo alla `CATEGORY_NODE_MAP` (§Estendere).

## 2. Use-case pratici end-to-end

### UC-1 — Batch cooking (ChefToni, Slice A)
1. Lun 08:00 il watcher `reorg-owner` legge lo schedule 48h, trova 3 cene separate.
2. Card in chat: *"🗂 Riorganizzo? accorpo «Cena zucchine», «Cena pasta», «Cena riso» → martedì 19:00.
   Motivo: batch cooking."* `[Applica] [No]`.
3. Tap **Applica** → i 3 `st_id` vengono riprogrammati al martedì 19:00 (reschedule assoluto).
   Card → "✅ Riorganizzato".

### UC-2 — Cena già pronta (ChefToni, Slice C)
1. 14:00 il watcher `prep-owner` vede "Cena zucchine" alle 20:00 (categoria cucina → specialista).
2. Pre-esegue ChefToni: genera ricetta + lista spesa, salvate in `_PREP_CACHE[st_id]`.
3. 19:50 arriva la card `task.due` con bottone extra `[📋 Preparazione]`.
4. Tap → vedi subito ricetta e lista spesa, senza aspettare la sessione dello specialista.

### UC-3 — Umidificatore solo se serve (Mycorise, Slice B + pre-gate)
1. Cron 09:00 → pre-gate "dati" esegue il probe HA. Umidità in salita → gate CHIUSO → nessuna sveglia.
2. Altro giorno: umidità ferma 6h → gate APERTO → sveglia Mycorise → task "Controlla umidificatore"
   → card. Zero rumore quando tutto va bene.

### UC-4 — Riprogrammazione difensiva (Slice A, qualsiasi nodo)
Task accalcati in un pomeriggio → proposta `sposta` per distribuirli su slot liberi → tap → schedule
più respirabile, senza che il maggiordomo tocchi nulla senza conferma.

## 3. Configurazioni di esempio

`taskgen.config.json` (Slice B) — aggiungere un watcher per un nodo:
```json
{ "watchers": [
    {"key":"cheftoni","node_key":"personalChef","cadence":"cron:0 11 * * *",
     "pre_gate":"temporal","enabled":true,"recipient_profile":"owner"},
    {"key":"mycorise-umidita","node_key":"mycorise","cadence":"cron:0 9 * * *",
     "pre_gate":"dati","gate_cmd":"<probe HA: exit 0 se trend umidità piatto>",
     "enabled":false,"recipient_profile":"owner"}
  ],
  "trust": {"categories":{"cucina":"trusted"},"daily_cap":2,"default_category_id":1} }
```

`butler.config.json` (Slice A+C):
```json
{ "watchers": [
    {"key":"reorg-owner","kind":"reorg","cadence":"cron:0 8 * * *","horizon_hours":48,
     "enabled":true,"recipient_profile":"owner"},
    {"key":"prep-owner","kind":"prep","cadence":"cron:0 */3 * * *","horizon_hours":6,
     "enabled":true,"recipient_profile":"owner"}
  ],
  "policy": {} }
```

## 4. Estendere a un nuovo nodo

1. **taskgen**: aggiungi un watcher in `taskgen.config.json` col `node_key` del nodo e una cadenza.
   Se serve un gate su dati, `pre_gate:"dati"` + `gate_cmd`.
2. **preparazione (Slice C)**: mappa la categoria del nodo in `task_routing.CATEGORY_NODE_MAP`
   (`{"<categoria>": "<node_key>"}`) + assicurati che `resolve_specialist` ritorni l'agente giusto.
   Senza mappatura il prep salta (per design, evita pre-esecuzioni inutili col generale).
3. **riorganizza (Slice A)**: nessuna config per-nodo — opera globalmente sullo schedule della persona.
4. **modalità per-nodo** (quando #18 landa): `policy.per_agent["<node_key>"]`.

## 5. Limiti noti (v1) e follow-up

- Reschedule assoluto senza validazione overlap → può sovrapporre; scegliere slot liberi lato seed.
- Categoria del suggerimento matchata su `name` (slug), non `display_name` → se un nodo emette
  l'etichetta umana cade sul default (v3: match anche display_name).
- Preparazione limitata ai nodi mappati a specialista.
- Alternative di modalità (#18–21) non ancora attive.

Roadmap completa: `.claude/docs/backlog.md` §maggiordomo.
