"""Lambda entry point: orchestrates fetch → routing → state → Telegram.

Per-event algorithm (the state machine, see docs/ARCHITECTURE.md):

1. apply routing rules; an event matching no topic and never tracked is
   ignored without touching DynamoDB;
2. unknown ARN → opening message to every destination topic, then the
   record is stored with the returned message ids;
3. known ARN → every ``event_log`` entry newer than ``last_log_timestamp``
   is an unpublished update; multiple entries are merged into one reply to
   the opening message of each topic;
4. a ``status`` transition is highlighted in the update header;
5. ``status == "0"`` or ``end_time`` present → closure message (duration
   plus involved services), then ``closed = True``;
6. already-closed events are ignored at subsequent polls.

Idempotency rule: DynamoDB state advances only AFTER Telegram confirms
the send. A failed send leaves the state untouched and the attempt is
repeated at the next poll: a duplicate message is better than a lost one.
"""

import json
import logging
import os
import time

from src import formatter, routing
from src.config import Config
from src.health_client import FetchError, SchemaError, fetch_events
from src.state import EventRecord, StateStore
from src.telegram import TelegramClient, TelegramError

logger = logging.getLogger(__name__)

METRIC_NAMESPACE = "AnotherAwsHealthTelegramBot"
PARSE_FAILURE_METRIC = "SchemaParseFailures"

# Standard LogRecord attributes, used to extract `extra` fields.
_LOG_RECORD_ATTRS = frozenset(
    logging.LogRecord("", 0, "", 0, "", (), None).__dict__
) | {"message", "asctime", "taskName"}

# Cached across warm invocations.
_token_cache = None
_logging_configured = False


class _JsonFormatter(logging.Formatter):
    """Structured JSON logging; never log the bot token."""

    def format(self, record):
        entry = {
            "timestamp": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in _LOG_RECORD_ATTRS:
                entry[key] = value
        if record.exc_info:
            entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(entry, ensure_ascii=False, default=str)


def _configure_logging():
    global _logging_configured
    if _logging_configured:
        return
    root = logging.getLogger()
    root.setLevel(os.environ.get("LOG_LEVEL", "INFO").upper())
    if not root.handlers:
        root.addHandler(logging.StreamHandler())
    for handler in root.handlers:
        handler.setFormatter(_JsonFormatter())
    _logging_configured = True


def _emit_parse_failure_metric(bot_name):
    """CloudWatch Embedded Metric Format record feeding the schema alarm.

    Printed to stdout so it needs no extra IAM permission: CloudWatch Logs
    extracts the metric automatically.
    """
    print(
        json.dumps(
            {
                "_aws": {
                    "Timestamp": int(time.time() * 1000),
                    "CloudWatchMetrics": [
                        {
                            "Namespace": METRIC_NAMESPACE,
                            "Dimensions": [["BotName"]],
                            "Metrics": [
                                {"Name": PARSE_FAILURE_METRIC, "Unit": "Count"}
                            ],
                        }
                    ],
                },
                "BotName": bot_name,
                PARSE_FAILURE_METRIC: 1,
            }
        )
    )


def _get_token(secret_arn):
    global _token_cache
    if _token_cache is None:
        import boto3  # available in the Lambda runtime; lazy for local tests

        client = boto3.client("secretsmanager")
        _token_cache = client.get_secret_value(SecretId=secret_arn)["SecretString"]
    return _token_cache


class _Budget:
    """Per-run message cap; unprocessed events resume at the next poll."""

    def __init__(self, limit):
        self._remaining = limit

    def take(self):
        if self._remaining <= 0:
            return False
        self._remaining -= 1
        return True

    @property
    def exhausted(self):
        return self._remaining <= 0


def _is_closed(event):
    return event.status == "0" or event.end_time is not None


def _latest_timestamp(event, floor=0):
    latest = max((entry.timestamp for entry in event.event_log), default=0)
    return max(latest, event.date, floor)


def _open_new_event(event, topics, state_store, telegram_client, budget, stats):
    text = formatter.opening_message(event)
    messages = {}
    for topic_id in topics:
        if not budget.take():
            break
        message_id = telegram_client.send_message(text, topic_id)
        messages[str(topic_id)] = message_id
        stats["messages_sent"] += 1
    if not messages:
        return  # budget exhausted before any send: retried at the next poll
    state_store.put(
        EventRecord(
            event_arn=event.arn,
            last_log_timestamp=_latest_timestamp(event),
            last_status=event.status,
            telegram_messages=messages,
            closed=False,
            first_seen=int(time.time()),
        )
    )
    stats["events_opened"] += 1


def _process_tracked_event(
    event, record, matched_topics, state_store, telegram_client, budget, stats
):
    closed = _is_closed(event)
    fresh_topics = set()

    # An event can escalate past a rule's min_status threshold mid-life:
    # topics matched now but never notified get an opening message first.
    if not closed:
        for topic_id in matched_topics:
            key = str(topic_id)
            if key in record.telegram_messages or not budget.take():
                continue
            message_id = telegram_client.send_message(
                formatter.opening_message(event), topic_id
            )
            record.telegram_messages[key] = message_id
            fresh_topics.add(key)
            stats["messages_sent"] += 1

    if closed:
        text = formatter.closure_message(event, record.first_seen)
        all_sent = True
        for key, opening_id in record.telegram_messages.items():
            if not budget.take():
                all_sent = False
                break
            telegram_client.send_message(text, int(key), reply_to_message_id=opening_id)
            stats["messages_sent"] += 1
        if all_sent:
            record.closed = True
            record.last_status = event.status
            record.last_log_timestamp = _latest_timestamp(event, record.last_log_timestamp)
            state_store.put(record)
            stats["events_closed"] += 1
        return

    new_entries = [
        entry
        for entry in event.event_log
        if entry.timestamp > record.last_log_timestamp
    ]
    status_changed = str(event.status) != str(record.last_status)
    if not new_entries and not status_changed:
        if fresh_topics:
            state_store.put(record)
        return

    text = formatter.update_message(event, new_entries, record.last_status)
    # Freshly opened topics already carry the latest content in their
    # opening message: skip them for this update.
    targets = {
        key: message_id
        for key, message_id in record.telegram_messages.items()
        if key not in fresh_topics
    }
    all_sent = True
    for key, opening_id in targets.items():
        if not budget.take():
            all_sent = False
            break
        telegram_client.send_message(text, int(key), reply_to_message_id=opening_id)
        stats["messages_sent"] += 1
    if all_sent:
        record.last_log_timestamp = _latest_timestamp(event, record.last_log_timestamp)
        record.last_status = event.status
        state_store.put(record)
        stats["events_updated"] += 1
    elif fresh_topics:
        # Persist at least the new opening ids to avoid duplicate openings.
        state_store.put(record)


def process_event(event, config, state_store, telegram_client, budget, stats):
    record = state_store.get(event.arn)
    if record is not None and record.closed:
        return
    matched_topics = [rule.topic_id for rule in routing.evaluate(event, config.rules)]
    if record is None:
        if not matched_topics:
            return  # not destined to any topic: no state written
        if _is_closed(event):
            return  # resolved before we ever saw it: nothing worth posting
        _open_new_event(event, matched_topics, state_store, telegram_client, budget, stats)
    else:
        _process_tracked_event(
            event, record, matched_topics, state_store, telegram_client, budget, stats
        )


def run(config, state_store, telegram_client, fetcher=fetch_events):
    """One polling cycle. Never raises for payload problems: a broken or
    unrecognizable payload produces a structured ERROR log plus a metric
    and the run ends without sending anything."""
    try:
        events = fetcher(config.health_endpoint)
    except SchemaError as exc:
        logger.error(
            "AWS Health payload schema unrecognized",
            extra={"error": str(exc), "endpoint": config.health_endpoint},
        )
        _emit_parse_failure_metric(config.bot_name)
        return {"status": "schema_error", "messages_sent": 0}
    except FetchError as exc:
        logger.error(
            "AWS Health endpoint unreachable",
            extra={"error": str(exc), "endpoint": config.health_endpoint},
        )
        return {"status": "fetch_error", "messages_sent": 0}

    stats = {
        "status": "ok",
        "events_seen": len(events),
        "events_opened": 0,
        "events_updated": 0,
        "events_closed": 0,
        "messages_sent": 0,
        "deferred": False,
    }
    budget = _Budget(config.max_messages_per_run)
    for event in sorted(events, key=lambda item: item.date):
        if budget.exhausted:
            stats["deferred"] = True
            logger.info(
                "message budget exhausted, deferring remaining events",
                extra={"max_messages_per_run": config.max_messages_per_run},
            )
            break
        try:
            process_event(event, config, state_store, telegram_client, budget, stats)
        except TelegramError as exc:
            # State was not advanced for this event: retried at next poll.
            logger.error(
                "telegram send failed, event will be retried",
                extra={"arn": event.arn, "error": str(exc)},
            )
    logger.info("run completed", extra=stats)
    return stats


def lambda_handler(lambda_event, context):
    _configure_logging()
    config = Config.from_env()
    token = _get_token(config.bot_token_secret_arn)

    import boto3  # lazy: keeps unit tests free of the AWS SDK

    table = boto3.resource("dynamodb").Table(config.table_name)
    state_store = StateStore(table, ttl_days=config.state_ttl_days)
    telegram_client = TelegramClient(token, config.chat_id)
    return run(config, state_store, telegram_client)
