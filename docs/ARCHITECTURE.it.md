# Architettura

> **Lingue disponibili**: [English](ARCHITECTURE.md) | [Italiano (corrente)](ARCHITECTURE.it.md)

## Panoramica

```
EventBridge Scheduler ──▶ Lambda (python3.12, 256 MB, 120 s, concurrency 1)
                            │
                            ├─ health_client  GET /public/currentevents (UTF-16)
                            ├─ routing        regole da ROUTING_RULES (funzioni pure)
                            ├─ state          DynamoDB, chiave event_arn, TTL nativo
                            ├─ formatter      messaggi HTML, Europe/Rome, italiano
                            └─ telegram       sendMessage, retry 429/5xx, pacing
```

La Lambda è l'unico writer (`ReservedConcurrentExecutions: 1`), quindi il layer di stato può usare semplici `put_item` a record intero senza espressioni condizionali.

## La macchina a stati

Tabella DynamoDB, chiave di partizione `event_arn`:

| Attributo | Tipo | Significato |
|---|---|---|
| `event_arn` | S | ARN dell'evento AWS Health (l'identità stabile) |
| `last_log_timestamp` | N | Timestamp dell'ultimo `event_log` già pubblicato |
| `last_status` | S | Ultimo `status` pubblicato |
| `telegram_messages` | M | `topic_id` → `message_id` del messaggio di apertura |
| `closed` | BOOL | Chiusura già annunciata |
| `first_seen` | N | Timestamp della prima osservazione |
| `ttl` | N | Scadenza TTL nativa (default 90 giorni) |

`telegram_messages` è una mappa e non un valore singolo: lo stesso evento può essere pubblicato in più topic e ogni topic ha il proprio `message_id` a cui agganciare le reply.

Algoritmo per ogni evento a ogni poll:

1. **Filtri di routing** — un evento che non matcha alcuna regola e non è mai stato tracciato viene ignorato senza alcuna scrittura su DynamoDB.
2. **ARN sconosciuto** → messaggio di apertura in ogni topic di destinazione; i `message_id` restituiti e lo stato corrente vengono salvati. Un evento già risolto alla prima osservazione viene saltato del tutto.
3. **ARN conosciuto** → ogni voce di `event_log` con `timestamp > last_log_timestamp` è un aggiornamento non pubblicato. Tutte le voci pendenti vengono accorpate in **un solo** messaggio per topic, inviato come reply al messaggio di apertura di quel topic.
4. **Transizione di stato** (`status != last_status`) → evidenziata nell'intestazione dell'aggiornamento (`🟡 Degrado → 🔴 Disservizio`).
5. **Chiusura** (`status == "0"` oppure `end_time` presente, `closed` ancora false) → reply di chiusura con durata totale e servizi coinvolti (da `impacted_service_status_changes`); poi `closed = true`.
6. Gli eventi chiusi vengono saltati nei poll successivi; il TTL elimina il record a tempo debito.

Un evento tracciato che supera a metà vita la soglia `min_status` di una regola riceve in quel momento il messaggio di apertura nel topic appena matchato (e l'aggiornamento che ha causato l'escalation non viene ripetuto lì).

## Idempotenza e gestione dei fallimenti

Lo stato avanza **solo dopo** la conferma di invio da Telegram:

- invio Telegram fallito → il record non viene aggiornato → lo stesso delta viene ricalcolato e reinviato al poll successivo. Un duplicato è possibile; un aggiornamento perso no.
- scrittura DynamoDB fallita dopo un invio riuscito → stesso esito (possibile duplicato).
- consegna multi-topic parziale (budget esaurito o errore a metà) → gli eventuali nuovi `message_id` di apertura vengono salvati, ma `last_log_timestamp` non avanza finché tutti i topic di destinazione non hanno ricevuto l'aggiornamento.

## Parsing difensivo e allarme sullo schema

L'endpoint è non documentato e codificato UTF-16. `health_client`:

- decodifica UTF-16 con fallback a UTF-8;
- legge ogni campo con `.get()` e un default; gli elementi malformati degradano, non sollevano mai eccezioni;
- solleva `SchemaError` solo quando la forma complessiva del payload è irriconoscibile.

Su `SchemaError` l'handler logga un ERROR strutturato, emette la metrica `SchemaParseFailures` in **CloudWatch Embedded Metric Format** (un semplice `print` su stdout — nessuna IAM aggiuntiva, la estrae CloudWatch Logs) e termina senza inviare nulla. La `AWS::CloudWatch::Alarm` del template scatta su quella metrica: senza, l'unico sintomo di un cambio di formato sarebbe un topic che smette silenziosamente di pubblicare.

## Rate limiting

Telegram consente ~20 messaggi/minuto per gruppo. Difese, dall'esterna all'interna:

1. budget `MAX_MESSAGES_PER_RUN` per esecuzione (default 15); il resto riprende al poll successivo (`deferred` nelle statistiche della run).
2. Gli aggiornamenti accumulati tra due poll vengono accorpati in un messaggio per topic.
3. Pausa di 1,5 s tra invii consecutivi.
4. Su HTTP 429, retry dopo il valore `retry_after` della risposta; sui 5xx, backoff esponenziale.

## Note di sicurezza

- Il token del bot vive in Secrets Manager, viene letto a runtime e messo in cache tra invocazioni warm. Non viene mai loggato e non compare mai nelle variabili d'ambiente; gli errori Telegram riportano solo status HTTP e description.
- IAM a privilegio minimo: le quattro azioni DynamoDB sulla sola tabella, `GetSecretValue` sul solo secret, scrittura log sul solo log group, `SendMessage` sulla sola DLQ.

## Fase 2 — sottoscrizioni private (punto di innesto)

`routing.evaluate(event, rules)` è una funzione pura che restituisce una lista di destinazioni. La fase 2 (`/subscribe <region>` per utente in chat privata) dovrà:

- aggiungere API Gateway + webhook per i comandi in ingresso (il polling schedulato è solo in uscita);
- aggiungere una tabella delle sottoscrizioni con chiave `user_id`;
- estendere il tipo di destinazione da "topic" a "topic | utente" — macchina a stati e ciclo di invio restano invariati;
- gestire gli utenti che bloccano il bot (HTTP 403 → rimozione della sottoscrizione).

Nulla nel codice attuale assume che le destinazioni siano topic, tranne il punto di invio: di proposito.
