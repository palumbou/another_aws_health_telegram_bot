# Another AWS Health Telegram Bot

A serverless bot that monitors the status of AWS services and publishes events to Telegram, in dedicated topics of a forum supergroup, following each incident for its whole lifetime: opening, intermediate updates, status transitions, closure.

> **Available languages**: [English (current)](README.md) | [Italiano](README.it.md)

Independent project: no code dependency and no shared AWS resources with [another_rss_telegram_bot](https://github.com/palumbou/another_rss_telegram_bot).

## How it differs from a feed reader

An RSS bot treats every item as immutable: either it is new, or it has already been seen. An AWS Health event is instead an entity that **evolves over time**: same identity (the ARN), N successive updates, status transitions, a closure. The heart of this project is a persisted state machine, not a deduplication.

## Architecture

```
EventBridge Scheduler (every 3 min)
        │
        ▼
   Lambda (Python 3.12)
        │
        ├── GET health.aws.amazon.com/public/currentevents
        ├── DynamoDB: per-ARN state (what was already published)
        └── Telegram Bot API: sendMessage into forum topics
```

No VPC, no layers, standard library only (plus `boto3`, already in the Lambda runtime). Expected cost is a few cents per month, well within the free tier.

## Data source

```
GET https://health.aws.amazon.com/public/currentevents
```

Public, unauthenticated, real-time: it is the endpoint feeding the public AWS Health dashboard.

**Caveats** (all handled in code):

- The endpoint is **not officially documented**. AWS recommends EventBridge for programmatic ingestion and the format may change. Parsing is fully defensive (`.get()` with defaults everywhere) and an unrecognizable payload produces a structured ERROR log, a CloudWatch metric and **zero messages** — never a crash loop. A CloudWatch alarm fires on that metric, so a silent format change cannot go unnoticed for weeks.
- The response is **UTF-16** encoded, with a UTF-8 fallback in case AWS changes it.
- Timestamps in `date`, `end_time` and `event_log` are in **seconds**; the ones in `impacted_service_status_changes` are in **milliseconds**.
- The region code is taken from the third ARN segment (`eu-central-1`, or `global`); `region_name` is only a display name and is null for global events.

### Alternatives evaluated (and the migration path if the endpoint dies)

| Source | Why not |
|---|---|
| Health API (`DescribeEvents`) | Requires a Business / Enterprise On-Ramp / Enterprise support plan; other accounts get `SubscriptionRequiredException`. |
| EventBridge `aws.health` | The AWS-recommended route, but public events can be delivered with up to one hour of delay — incompatible with following incidents in real time. |
| RSS `status.aws.amazon.com` | Per-service, poorly structured, and AWS removed its documentation in August 2025. |

### Status codes

As observed on the endpoint (to be confirmed in the field), kept in a single module constant (`src/formatter.py`):

| Value | Meaning | Emoji |
|---|---|---|
| `0` | Resolved / operational | 🟢 |
| `1` | Informational / investigating | 🔵 |
| `2` | Performance degradation | 🟡 |
| `3` | Outage | 🔴 |

## Routing

Routing is expressed as **declarative rules**, never as conditionals in code: the `ROUTING_RULES` JSON environment variable (a CloudFormation parameter).

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

- `regions`: list of region codes, or `"*"` for all.
- `min_status`: minimum `status` (severity) published to that topic — it cuts the noise of purely informational events in the catch-all topic.
- An event can match several rules and is then published to every matching topic, with per-topic `message_id` tracking.
- If an event escalates past a rule's threshold mid-life, it gets its opening message in that topic at that moment.

Rationale for the key-regions list: `eu-south-1` (Milan) and `eu-west-1` (Ireland) are the most used by the Italian community, `eu-central-1` (Frankfurt) is a very common secondary, `us-east-1` hosts the control planes of many global services and its outages cascade everywhere, `global` covers non-regional events such as CloudFront, Route 53 and IAM.

## Messages

Fixed texts are in Italian; the original AWS texts stay in English on purpose (official communications, an automatic translation would be ambiguous in an operational context). Opening messages start a thread; updates and the closure arrive as **replies** to the opening message of each topic. Multiple updates accumulated between two polls are merged into a single message. Messages are HTML-formatted, escaped, truncated at 4096 characters with `[...]`, timestamps converted to Europe/Rome.

## Rate limiting

Telegram allows ~20 messages per minute per group — reachable during a major incident. Countermeasures:

- per-run message cap (`MaxMessagesPerRun`, default 15); leftover events resume at the next poll;
- pause between consecutive sends;
- retry honoring `retry_after` on HTTP 429, exponential backoff on 5xx;
- merging of multiple updates of the same event.

Idempotency: DynamoDB state advances only **after** Telegram confirms the send. A failed send is retried at the next poll — a duplicate message is better than a lost update.

## Telegram prerequisites

- The bot must be an **administrator** of the supergroup.
- It needs the **Manage topics** permission (`can_manage_topics`), otherwise the API returns `TOPIC_CLOSED` on write-closed topics.
- The `chat_id` of a public group: `https://api.telegram.org/bot<TOKEN>/getChat?chat_id=@username`.
- The `message_thread_id` of a topic is the second numeric segment of the topic link (`t.me/c/<internal_id>/<thread_id>`).

## Deployment

Prerequisites: AWS CLI configured, an S3 bucket for the deployment package, the Telegram bot token.

```bash
TELEGRAM_BOT_TOKEN='123456:ABC...' ./scripts/deploy.sh \
  --bucket my-artifacts-bucket \
  --region eu-west-1 \
  --chat-id '-100xxxxxxxxxx'
```

The script runs the tests, validates the template, zips `src/`, uploads it to S3 and deploys `infrastructure/template.yaml`. The token is stored in **Secrets Manager** and read at runtime — never in an environment variable. On later deploys omit `TELEGRAM_BOT_TOKEN` to keep the stored one.

To avoid retyping the values on every deploy, copy `deploy.local.env.example` to `deploy.local.env` and fill it in: the script sources it automatically if present. The file is gitignored; command-line options and already-exported variables take precedence over it.

Every taggable resource carries a `CostCenter` tag valued with the stack name, for cost tracking (the EventBridge schedule is the only exception: CloudFormation does not support tags on it, and it has no direct cost).

### CloudFormation parameters

| Parameter | Default | Description |
|---|---|---|
| `BotName` | `another-aws-health-telegram-bot` | Base name for every resource |
| `TelegramBotToken` | — | Bot token (NoEcho, seeds the secret) |
| `TelegramChatId` | `-100xxxxxxxxxx` | Target supergroup |
| `RoutingRules` | the two rules above | Routing rules JSON |
| `ScheduleExpression` | `rate(3 minutes)` | Polling cadence (never below 1 minute) |
| `MaxMessagesPerRun` | `15` | Per-run Telegram message cap |
| `LogRetentionDays` | `30` | CloudWatch Logs retention |
| `StateTtlDays` | `90` | DynamoDB TTL of state records |
| `CodeS3Bucket` / `CodeS3Key` | — | Location of the Lambda package |

## Testing

```bash
pip install -r requirements.txt
python -m pytest tests/ -q
```

The suite covers the state machine against real-shaped fixtures: no duplicates across polls, merged updates, transitions, single closure, malformed payloads, 429 handling, message budget. See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the design details.

## Repository structure

```
src/                  Lambda source (stdlib only)
  handler.py          entry point, orchestration
  health_client.py    fetch + defensive parsing of the endpoint
  state.py            DynamoDB access, state machine records
  routing.py          declarative routing rules evaluation
  formatter.py        Telegram message composition (Italian)
  telegram.py         Bot API client, retry, rate limiting
  config.py           env vars reading and validation
infrastructure/       CloudFormation template
scripts/deploy.sh     package + deploy
tests/                pytest suite + real-shaped fixtures
docs/                 architecture notes
```

## Phase 2 — personal subscriptions (not implemented)

Per-user filtering inside a group topic is technically impossible (Telegram has no per-user visibility on group messages); the only route is private chat delivery, which needs an API Gateway webhook for incoming commands, a subscriptions table and per-user error handling. The hook is already in place: routing is a pure function from an event to a list of destinations, so adding "user" destinations next to "topic" ones is a contained change. Details in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## License

[Creative Commons Attribution-NonCommercial 4.0 International](LICENSE).
