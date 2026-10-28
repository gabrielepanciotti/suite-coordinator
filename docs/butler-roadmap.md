# Maggiordomo — migliorie e sviluppi futuri

> Idee di evoluzione oltre l'M5 v1. Companion di `.claude/docs/butler.md`.
> Le voci con `#N` hanno già un issue; le altre sono candidate.

## A. Correttezza / robustezza (prossimi)

1. **Reschedule con validazione slot** (follow-up noto). Oggi `apply_postpone` sposta a orario
   assoluto senza validare overlap/disponibilità. Migliorare: prima di proporre, il seed sceglie
   slot liberi; oppure un endpoint server `reschedule` che riusa `find_all_available_slots` +
   `_assert_no_overlap` e rifiuta con 409 se occupato (il Coordinator ripropone).
2. **Match categoria su `display_name`** oltre a `name` (v3 già annotato in taskgen). Se un nodo emette
   l'etichetta umana ("Cucina") invece dello slug ("cucina") oggi cade sul default. Aggiungere match
   case-insensitive anche su `display_name`, poi fuzzy (Levenshtein) con soglia.
3. **Estendere `CATEGORY_NODE_MAP`** ai nodi mancanti così la preparazione (Slice C) li copra
   (oggi solo `cucina`/`funghi`; il resto → fallback → skip). Ogni nodo dichiara le sue categorie.
4. **Cache prep invalidation**: invalidare/rigenerare `_PREP_CACHE[st_id]` se il task viene
   modificato o riprogrammato tra la preparazione e la due; TTL esplicito sull'artefatto.
5. **Prep conflict-aware**: non pre-eseguire task che Slice A sta per riprogrammare (coordinare i due
   watcher: prep salta gli st_id con una proposta reorg viva).

## B. Modalità alternative (mode-seam, #18–21)

6. **#18 — Config per-agente completa**: persistenza/editing della `ButlerPolicy.per_agent` (oggi solo
   file JSON). UI o comando per attivare modalità per singolo agente. Il seam `resolve()` è già pronto.
7. **#19 — Slice A auto-apply fidate**: trust policy per *tipo* di riorganizzazione (accorpa vs sposta),
   auto-applica le fidate con notifica+`[Annulla]` (come taskgen trusted). Richiede una soglia di
   confidenza sulla proposta (l'LLM la dichiara, o la deriviamo da regole).
8. **#20 — Slice C side-effects**: prep che compie azioni (bozza email in draft, pre-compila ordine).
   Ogni azione dietro conferma + trust policy per tipo + audit. Il `prep_output_mode` seam c'è già.
9. **#21 — Trigger on-demand**: comandi `/riorganizza` e `/prepara <task>` che invocano la stessa
   pipeline del watcher a richiesta. Riusa `run_reorg_watcher`/`run_prep_watcher`.

## C. Intelligenza / feedback loop

10. **Feedback loop sulle proposte**: bottoni "utile/non utile" (o desumerlo da Applica/Ignora) per
    tarare frequenza e tipo di proposte per-nodo. Un nodo con molte proposte rifiutate viene svegliato
    meno spesso; le categorie di riorganizzazione spesso applicate salgono di fiducia.
11. **Reorg che impara i pattern della persona**: orari preferiti, raggruppamenti abituali (batch
    cooking il weekend, task casa al sabato). Deriva regole dallo storico ScheduledTask/Execution.
12. **Digest giornaliero**: invece di N card separate, un'unica card mattutina che raccoglie le proposte
    di riorganizzazione + le preparazioni pronte, con azioni multiple. Riduce il rumore.

## D. Affidabilità operativa

13. **Daily cap per butler** (come `taskgen.daily_cap`): limite di proposte reorg/prep al giorno per
    non intasare la chat.
14. **Pre-gate per i butler watcher**: riusare `taskgen_gate.passes_pre_gate` anche per reorg/prep
    (es. non svegliare se lo schedule è vuoto — già coperto dal no-op, ma un gate "dati" permette
    condizioni ricche: prep solo se la persona è in casa, reorg solo nei giorni feriali).
15. **Osservabilità**: contatori per tick (proposed/applied/prepared/failed) esposti via l'API
    notifiche o un pannello dashboard; alert se un watcher fallisce N volte di fila.
16. **Idempotenza rinforzata**: oggi il dedup è per contenuto+TTL; aggiungere un marker lato Scheduler
    (es. tag "proposto-da-maggiordomo") per non ri-proporre dopo un restart che perde il dedup.

## E. Estensione multi-persona / cross-nodo

17. **Reorg cross-nodo**: rispettare i vincoli di altri nodi (es. non spostare una cena su uno slot
    che UniUdine ha riservato per studio) leggendo le loro API invece del solo schedule Scheduler.
18. **Multi-profilo**: oggi i watcher sono per `recipient_profile`; estendere a più persone (un membro) con
    policy e specialisti distinti.

## F. Testing / qualità

19. **E2E live harness**: script che semina uno ScheduledTask, forza un tick reorg/prep, verifica
    card + reschedule/artefatto sul DB, e pulisce — come il gate E2E di taskgen Slice B.
20. **Contract test** Coordinator↔Scheduler: verificare che i DTO (`ScheduledTaskAgentRead`,
    `reschedule` body) restino allineati alle firme del client, in CI di entrambi i repo.

## Priorità suggerita
1 (overlap) → 3 (coverage prep) → 9 (#21 on-demand, alto valore/basso costo) → 12 (digest) →
10 (feedback) → il resto secondo uso reale.
