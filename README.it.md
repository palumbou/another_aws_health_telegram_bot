# Another AWS Health Telegram Bot

Bot serverless che monitora lo stato dei servizi AWS e pubblica gli eventi su Telegram, in topic dedicati di un supergruppo forum, seguendo ogni incidente per tutta la sua durata: apertura, aggiornamenti intermedi, cambi di stato, chiusura.

> **Lingue disponibili**: [English](README.md) | [Italiano (corrente)](README.it.md)

Progetto indipendente: nessuna dipendenza di codice e nessuna risorsa AWS condivisa con [another_rss_telegram_bot](https://github.com/palumbou/another_rss_telegram_bot).

## Differenza rispetto a un feed reader

Un bot RSS tratta ogni elemento come immutabile: o è nuovo, o è già stato visto. Un evento AWS Health è invece un'entità che **evolve nel tempo**: stessa identità (l'ARN), N aggiornamenti successivi, transizioni di stato, una chiusura. Il cuore del progetto è una macchina a stati persistita, non una deduplica.

## Architettura

```
EventBridge Scheduler (ogni 3 min)
        │
        ▼
   Lambda (Python 3.12)
        │
        ├── GET health.aws.amazon.com/public/currentevents
        ├── DynamoDB: stato per ARN (cosa è già stato pubblicato)
        └── Telegram Bot API: sendMessage nei topic del forum
```

Nessun VPC, nessun layer, solo standard library (più `boto3`, già presente nel runtime Lambda). Costo atteso: pochi centesimi al mese, ampiamente nel free tier.

## Sorgente dati

```
GET https://health.aws.amazon.com/public/currentevents
```

Pubblico, senza autenticazione, in tempo reale: è l'endpoint che alimenta la dashboard pubblica di AWS Health.

**Avvertenze** (tutte gestite nel codice):

- L'endpoint è **non documentato ufficialmente**. AWS raccomanda EventBridge per l'ingestione programmatica e il formato può cambiare. Il parsing è interamente difensivo (`.get()` con default ovunque) e un payload irriconoscibile produce un log ERROR strutturato, una metrica CloudWatch e **zero messaggi** — mai un crash in loop. Sulla metrica è configurato un allarme CloudWatch: un cambio di formato silenzioso non può passare inosservato per settimane.
- La risposta è codificata in **UTF-16**, con fallback a UTF-8 in caso di cambiamento.
- I timestamp in `date`, `end_time` ed `event_log` sono in **secondi**; quelli in `impacted_service_status_changes` in **millisecondi**.
- Il codice region si ricava dal terzo segmento dell'ARN (`eu-central-1`, oppure `global`); `region_name` è solo un nome leggibile ed è null per gli eventi globali.

### Alternative valutate (e percorso di migrazione se l'endpoint sparisse)

| Sorgente | Perché no |
|---|---|
| Health API (`DescribeEvents`) | Richiede piano di supporto Business / Enterprise On-Ramp / Enterprise; dagli altri account restituisce `SubscriptionRequiredException`. |
| EventBridge `aws.health` | Via raccomandata da AWS, ma per gli eventi pubblici la consegna può avere fino a un'ora di ritardo — incompatibile con il seguire l'incidente in tempo reale. |
| RSS `status.aws.amazon.com` | Per-servizio, poco strutturato, e AWS ne ha rimosso la documentazione ad agosto 2025. |

### Codici di stato

Dedotti dall'osservazione (da confermare sul campo), isolati in una costante di modulo (`src/formatter.py`):

| Valore | Significato | Emoji |
|---|---|---|
| `0` | Risolto / operativo | 🟢 |
| `1` | Informativo / in indagine | 🔵 |
| `2` | Degrado delle performance | 🟡 |
| `3` | Disservizio | 🔴 |

## Routing

Il routing è espresso come **regole dichiarative**, mai come `if` nel codice: la variabile d'ambiente JSON `ROUTING_RULES` (parametro CloudFormation).

```json
[
  {
    "name": "key-regions",
    "topic_id": 1,
    "regions": ["eu-south-1", "eu-west-1", "eu-central-1", "us-east-1", "global"],
    "min_status": 1
  },
  {
    "name": "all",
    "topic_id": 2,
    "regions": "*",
    "min_status": 2
  }
]
```

- `regions`: lista di codici region, oppure `"*"` per tutte.
- `min_status`: soglia minima di `status` per pubblicare in quel topic — taglia il rumore degli eventi puramente informativi nel topic generale.
- Un evento può matchare più regole e viene pubblicato in tutti i topic corrispondenti, con `message_id` tracciati separatamente.
- Se un evento supera la soglia di una regola a metà vita, riceve in quel momento il messaggio di apertura in quel topic.

Razionale delle region chiave: `eu-south-1` (Milano) ed `eu-west-1` (Irlanda) sono le più usate dall'utenza italiana, `eu-central-1` (Francoforte) è comunissima come secondaria, `us-east-1` ospita i control plane di molti servizi globali e i suoi guasti hanno effetti a cascata ovunque, `global` copre gli eventi non regionali come CloudFront, Route 53 e IAM.

## Messaggi

I testi fissi sono in italiano; i testi originali AWS restano in inglese di proposito (comunicazioni ufficiali: una traduzione automatica introdurrebbe ambiguità in un contesto operativo). L'apertura avvia il thread; aggiornamenti e chiusura arrivano come **reply** al messaggio di apertura di ciascun topic. Più aggiornamenti accumulati tra due poll vengono accorpati in un solo messaggio. Formattazione HTML con escaping, troncamento a 4096 caratteri con `[...]`, timestamp convertiti in Europe/Rome.

## Rate limiting

Telegram limita a ~20 messaggi al minuto per gruppo — soglia raggiungibile durante un incidente maggiore. Contromisure:

- tetto di messaggi per esecuzione (`MaxMessagesPerRun`, default 15); gli eventi rimanenti riprendono al poll successivo;
- pausa tra invii consecutivi;
- retry rispettando `retry_after` su HTTP 429, backoff esponenziale sui 5xx;
- accorpamento degli aggiornamenti multipli dello stesso evento.

Idempotenza: lo stato su DynamoDB avanza solo **dopo** la conferma di invio da Telegram. Un invio fallito viene ritentato al poll successivo — meglio un messaggio duplicato che un aggiornamento perso.

## Prerequisiti Telegram

- Il bot deve essere **amministratore** del supergruppo.
- Serve il permesso **Gestisci topic** (`can_manage_topics`), altrimenti l'API restituisce `TOPIC_CLOSED` sui topic chiusi in scrittura.
- Il `chat_id` di un gruppo pubblico: `https://api.telegram.org/bot<TOKEN>/getChat?chat_id=@username`.
- Il `message_thread_id` di un topic è il secondo segmento numerico del link al topic (`t.me/c/<id_interno>/<thread_id>`).

## Deploy

Prerequisiti: AWS CLI configurata, un bucket S3 per il pacchetto di deploy, il token del bot Telegram.

```bash
TELEGRAM_BOT_TOKEN='123456:ABC...' ./scripts/deploy.sh \
  --bucket my-artifacts-bucket \
  --region eu-west-1 \
  --chat-id '-100xxxxxxxxxx'
```

Lo script esegue i test, valida il template, crea lo zip di `src/`, lo carica su S3 e deploya `infrastructure/template.yaml`. Il token va in **Secrets Manager** e viene letto a runtime — mai in variabile d'ambiente. Nei deploy successivi omettere `TELEGRAM_BOT_TOKEN` per mantenere quello già salvato.

Ogni risorsa taggabile porta un tag `CostCenter` valorizzato con il nome dello stack, per il tracciamento dei costi (unica eccezione la schedule EventBridge: CloudFormation non supporta i tag su quella risorsa, che comunque non ha costo diretto).

### Parametri CloudFormation

| Parametro | Default | Descrizione |
|---|---|---|
| `BotName` | `another-aws-health-telegram-bot` | Nome base di tutte le risorse |
| `TelegramBotToken` | — | Token del bot (NoEcho, inizializza il secret) |
| `TelegramChatId` | — (obbligatorio) | Supergruppo di destinazione |
| `RoutingRules` | le due regole sopra | JSON delle regole di routing |
| `ScheduleExpression` | `rate(3 minutes)` | Cadenza di polling (mai sotto il minuto) |
| `MaxMessagesPerRun` | `15` | Tetto di messaggi Telegram per esecuzione |
| `LogRetentionDays` | `30` | Retention dei log CloudWatch |
| `StateTtlDays` | `90` | TTL DynamoDB dei record di stato |
| `CodeS3Bucket` / `CodeS3Key` | — | Posizione del pacchetto Lambda |

## Test

```bash
pip install -r requirements.txt
python -m pytest tests/ -q
```

La suite copre la macchina a stati con fixture realistiche: nessun duplicato tra poll, aggiornamenti accorpati, transizioni, chiusura annunciata una volta sola, payload malformati, gestione del 429, budget di messaggi. Dettagli di progetto in [docs/ARCHITECTURE.it.md](docs/ARCHITECTURE.it.md).

## Struttura del repository

```
src/                  sorgente Lambda (solo stdlib)
  handler.py          entry point, orchestrazione
  health_client.py    fetch + parsing difensivo dell'endpoint
  state.py            accesso DynamoDB, record della macchina a stati
  routing.py          valutazione delle regole dichiarative di routing
  formatter.py        composizione dei messaggi Telegram (italiano)
  telegram.py         client Bot API, retry, rate limiting
  config.py           lettura e validazione delle env var
infrastructure/       template CloudFormation
scripts/deploy.sh     package + deploy
tests/                suite pytest + fixture realistiche
docs/                 note di architettura
```

## Fase 2 — sottoscrizioni personali (non implementata)

Il filtro per singolo utente dentro un topic di gruppo è tecnicamente impossibile (Telegram non ha visibilità per-utente sui messaggi di gruppo); l'unica strada è l'invio in chat privata, che richiede un webhook via API Gateway per i comandi in ingresso, una tabella delle sottoscrizioni e la gestione degli errori per-utente. Il punto di innesto è già pronto: il routing è una funzione pura da evento a lista di destinazioni, quindi aggiungere destinazioni di tipo "utente" accanto a quelle di tipo "topic" è una modifica contenuta. Dettagli in [docs/ARCHITECTURE.it.md](docs/ARCHITECTURE.it.md).

## Licenza

[Creative Commons Attribution-NonCommercial 4.0 International](LICENSE).
