# Architecture

> **Available languages**: [English (current)](ARCHITECTURE.md) | [Italiano](ARCHITECTURE.it.md)

## Overview

```
EventBridge Scheduler ──▶ Lambda (python3.12, 256 MB, 120 s, concurrency 1)
                            │
                            ├─ health_client  GET /public/currentevents (UTF-16)
                            ├─ routing        rules from ROUTING_RULES (pure functions)
                            ├─ state          DynamoDB, key event_arn, native TTL
                            ├─ formatter      HTML messages, Europe/Rome, Italian
                            └─ telegram       sendMessage, retry 429/5xx, pacing
```

The Lambda is the only writer (`ReservedConcurrentExecutions: 1`), so the state layer can use plain full-record `put_item` writes without conditional expressions.

## The state machine

DynamoDB table, partition key `event_arn`:

| Attribute | Type | Meaning |
|---|---|---|
| `event_arn` | S | AWS Health event ARN (the stable identity) |
| `last_log_timestamp` | N | Timestamp of the last `event_log` entry already published |
| `last_status` | S | Last published `status` |
| `telegram_messages` | M | `topic_id` → `message_id` of the opening message |
| `closed` | BOOL | Closure already announced |
| `first_seen` | N | First observation timestamp |
| `ttl` | N | Native TTL expiry (default 90 days) |

`telegram_messages` is a map, not a single value: the same event can be published in several topics and each topic has its own `message_id` to anchor replies to.

Per-event algorithm at every poll:

1. **Routing filters** — an event matching no rule and never tracked is ignored with no DynamoDB write at all.
2. **Unknown ARN** → opening message to every destination topic; the returned `message_id`s and the current state are stored. An event that is already resolved at first sighting is skipped entirely.
3. **Known ARN** → every `event_log` entry with `timestamp > last_log_timestamp` is an unpublished update. All pending entries are merged into **one** message per topic, sent as a reply to that topic's opening message.
4. **Status transition** (`status != last_status`) → highlighted in the update header (`🟡 Degrado → 🔴 Disservizio`).
5. **Closure** (`status == "0"` or `end_time` present, `closed` still false) → closure reply with total duration and the services involved (from `impacted_service_status_changes`); then `closed = true`.
6. Closed events are skipped at subsequent polls; the TTL eventually removes the record.

A tracked event that escalates past a rule's `min_status` mid-life gets an opening message in the newly matching topic at that moment (and the update that triggered it is not repeated there).

## Idempotency and failure handling

State advances **only after** Telegram confirms a send:

- Telegram send fails → the record is not updated → the same delta is recomputed and resent at the next poll. A duplicate is possible; a lost update is not.
- DynamoDB write fails after a successful send → same outcome (duplicate possible).
- Partial multi-topic delivery (budget exhausted or error midway) → any newly obtained opening `message_id`s are persisted, but `last_log_timestamp` does not advance unless every target topic received the update.

## Defensive parsing and the schema alarm

The endpoint is undocumented and UTF-16 encoded. `health_client`:

- decodes UTF-16 with a UTF-8 fallback;
- reads every field with `.get()` and a default; malformed items degrade, they never raise;
- raises `SchemaError` only when the overall payload shape is unrecognizable.

On `SchemaError` the handler logs a structured ERROR, emits a `SchemaParseFailures` metric in **CloudWatch Embedded Metric Format** (a plain `print` to stdout — no extra IAM, CloudWatch Logs extracts it) and returns without sending anything. The `AWS::CloudWatch::Alarm` in the template fires on that metric: without it, the only symptom of a format change would be topics silently going quiet.

## Rate limiting

Telegram allows ~20 messages/minute per group. Layers of defense, from outer to inner:

1. `MAX_MESSAGES_PER_RUN` budget per execution (default 15); leftovers resume next poll (`deferred` in the run stats).
2. Updates accumulated between polls are merged into one message per topic.
3. 1.5 s pause between consecutive sends.
4. On HTTP 429, retry after the `retry_after` value from the response; on 5xx, exponential backoff.

## Security notes

- The bot token lives in Secrets Manager, is fetched at runtime and cached across warm invocations. It is never logged and never appears in environment variables; Telegram error messages carry only the HTTP status and description.
- IAM is least-privilege: the four DynamoDB actions on the one table, `GetSecretValue` on the one secret, log writes on the one log group, `SendMessage` on the one DLQ.

## Phase 2 — private subscriptions (design hook)

`routing.evaluate(event, rules)` is a pure function returning a list of destinations. Phase 2 (per-user `/subscribe <region>` in private chat) will:

- add an API Gateway + webhook path for incoming commands (the scheduled polling is outbound-only);
- add a subscriptions table keyed by `user_id`;
- extend the destination type from "topic" to "topic | user" — the state machine and the send loop stay untouched;
- handle users who block the bot (HTTP 403 → subscription removal).

Nothing in the current code assumes destinations are topics except the send call site, on purpose.
