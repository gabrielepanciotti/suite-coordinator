# Usare la Sala Comando del Coordinator

Questa è la guida per chi ha accesso al forum Telegram del Coordinator — la
**Sala Comando**: un solo bot da cui parli a *tutti* i tuoi progetti personali
(Cosmos, ChefToni, Scheduler, Mycorise, HomeResearcher, Travelers, ComUnity,
Life e gli altri), uno per scheda. Non serve saper programmare né aprire un
terminale.

A differenza del gruppo dedicato di un singolo progetto (es. ComUnity, dove
chiunque nel gruppo può scrivere), la Sala Comando risponde solo a chi è
**mappato come profilo** — di norma tu e chi vive con te. Se qui un comando
resta muto, è quasi sempre per questo, non un guasto (vedi in fondo).

## Le schede: una per (progetto, referente)

Il forum è diviso in **schede** (i "topic" di Telegram). Ogni scheda è legata
a un progetto e a un referente (un agente di quel progetto, o il generico
"💬 Generale"), e ha una sua memoria: dentro una scheda prosegui un discorso,
cambiando scheda cambi sia argomento sia interlocutore.

- **`/progetti`** — apre la dashboard: tocca un progetto per vedere le sue
  schede aperte o aprirne una nuova.
- **`/nuovo <progetto> [referente] [nome]`** — apre una scheda nuova. Con solo
  il progetto ti mostra le voci disponibili (funzionalità o agenti) da
  scegliere a bottoni; se sai già cosa vuoi puoi scriverlo diretto, es.
  `/nuovo cosmos builder` apre una scheda su Cosmos parlando col builder.
- **`/riprendi <progetto>`** — rientra in una sessione già aperta invece di
  aprirne una nuova.
- **`/reset`** — dentro una scheda, dimentica il filo e riparte pulito (serve
  quando la conversazione si è impantanata o cambi argomento).
- **`/chiudi`** — archivia la scheda corrente.

Puoi mandare anche **foto**: vanno al referente della scheda in cui le mandi
(es. a un progetto tipo cucina/dispensa, una foto aggiorna quello che ha in
casa).

## Scrivere alla scheda

In italiano corrente, come lo diresti a una persona — non ci sono comandi da
imparare per il lavoro vero, solo per navigare tra le schede. Più sei preciso
su *cosa deve succedere*, meno giri servono, ma puoi anche partire vago e
farti fare le domande.

## Scegliere il motore — `/modello` e `/compara`

Ogni scheda risponde con un motore di default (di solito il più
rapido/economico). Puoi cambiarlo, per quella scheda soltanto:

- **`/modello`** (senza argomenti) — mostra una tastiera con i motori
  disponibili in questa scheda: tipicamente **Claude (abbonamento)** — il più
  accurato, lo stesso del PC —, **DeepSeek** (Flash/Pro), e un catalogo
  **Zen** di modelli gratuiti/promozionali che ruota nel tempo. Tocchi un
  pulsante e la scheda resta su quella scelta finché non premi «torna al
  default» o riscrivi `/modello`.
- **`/compara [motori] su: <richiesta>`** — fa rispondere **più motori alla
  stessa richiesta**, un messaggio per motore, man mano che ciascuno finisce.
  Utile quando non sai quale renda meglio, o vuoi più proposte tra cui
  scegliere:

  ```
  /compara su: creami un messaggio di presentazione per questo annuncio
  ```

  Senza specificare nulla usa una terna di default (Claude, DeepSeek, e il
  modello Zen di default). Per scegliere tu:

  ```
  /compara deepseek claude su: creami un messaggio di presentazione per questo annuncio
  ```

  È una prova: non cambia il motore della scheda, che resta quello scelto con
  `/modello` (o il default).
- **`/opencodemodel`** (senza argomenti) — mostra quale modello Zen è
  impostato come default per l'alias "opencode" di `/compara`. Con un
  argomento (`/opencodemodel <provider/modello>`) lo cambia; `/opencodemodel
  auto` torna al default. È un'impostazione **di tutta la Sala Comando**, non
  di una singola scheda: cambiala solo se sai cosa stai scegliendo, o chiedi
  prima al referente di un progetto tecnico.

## Cosa aspettarsi, e cosa no

**Non è istantaneo.** Una risposta di cortesia arriva in fretta; un lavoro
vero richiede minuti, perché dietro girano davvero i comandi.

**Il motore di default può sbagliare.** È scelto per essere rapido ed
economico, non per essere il migliore su tutto: per un lavoro delicato passa
a Claude con `/modello`, o confronta con `/compara`.

**I commit e le azioni risultano tue.** Gli agenti usano le tue chiavi/il tuo
abbonamento: dalla storia di GitHub o dai log non si distingue "ha scritto
l'agente" da "hai scritto tu".

**Resta tutto scritto.** Ogni messaggio — comandi compresi — finisce in un
archivio sul minipc, insieme ai turni degli agenti. Non è sorveglianza, è
l'unico modo di ricostruire perché è stata presa una decisione.

## Se non risponde

1. Controlla di essere **dentro una scheda** legata a un progetto: una scheda
   appena creata senza referente non risponde a nessuno (il messaggio viene
   comunque archiviato).
2. Se tace ovunque, di norma è il bot fermo o in passaggio di consegne fra i
   computer. Si vede da `curl localhost:8013/ownership` sul minipc:
   `is_polling: true` significa che sta ascoltando. Dopo un riavvio ci mette
   circa 45 secondi a riprendere, di proposito.
3. Un comando che non parte da questa Sala (es. sei nel gruppo di un progetto
   invece che nella Sala Comando) può non essere disponibile lì: i comandi di
   navigazione (`/progetti`, `/nuovo`, `/riprendi`) sono della Sala Comando,
   non dei gruppi dedicati ai singoli progetti.
