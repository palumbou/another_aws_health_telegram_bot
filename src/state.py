"""DynamoDB-backed persistence for the per-event state machine.

Unlike a feed reader, an AWS Health event is a mutable entity: same
identity (the ARN), N successive updates, status transitions, a closure.
This module stores what has already been published for each ARN so the
handler can compute the delta at every poll.

The table uses ``event_arn`` as partition key and native TTL on the
``ttl`` attribute. The Lambda runs with reserved concurrency 1, so there
is a single writer and plain ``put_item`` full-record writes are safe.

State must be written only AFTER Telegram has confirmed the send: if the
send fails the state does not advance and the attempt is retried at the
next poll (a duplicate message is better than a lost update).
"""

import time
from dataclasses import dataclass, field


@dataclass
class EventRecord:
    """Published-so-far state of one AWS Health event."""

    event_arn: str
    last_log_timestamp: int  # timestamp of the last event_log entry published
    last_status: str  # last status published
    telegram_messages: dict = field(default_factory=dict)  # topic_id (str) -> message_id
    closed: bool = False  # closure already announced
    first_seen: int = 0  # timestamp of the first observation


class StateStore:
    """Thin data-access layer over the DynamoDB table.

    The boto3 Table object is injected so tests can pass an in-memory fake.
    """

    def __init__(self, table, ttl_days, clock=time.time):
        self._table = table
        self._ttl_days = ttl_days
        self._clock = clock

    def get(self, event_arn):
        """Return the EventRecord for an ARN, or None if never tracked."""
        response = self._table.get_item(Key={"event_arn": event_arn})
        item = response.get("Item")
        if not item:
            return None
        # DynamoDB returns numbers as Decimal: normalize to int.
        messages = {
            str(topic_id): int(message_id)
            for topic_id, message_id in (item.get("telegram_messages") or {}).items()
        }
        return EventRecord(
            event_arn=str(item["event_arn"]),
            last_log_timestamp=int(item.get("last_log_timestamp", 0)),
            last_status=str(item.get("last_status", "")),
            telegram_messages=messages,
            closed=bool(item.get("closed", False)),
            first_seen=int(item.get("first_seen", 0)),
        )

    def put(self, record):
        """Persist the full record, refreshing the TTL."""
        self._table.put_item(
            Item={
                "event_arn": record.event_arn,
                "last_log_timestamp": int(record.last_log_timestamp),
                "last_status": str(record.last_status),
                "telegram_messages": {
                    str(topic_id): int(message_id)
                    for topic_id, message_id in record.telegram_messages.items()
                },
                "closed": bool(record.closed),
                "first_seen": int(record.first_seen),
                "ttl": int(self._clock()) + self._ttl_days * 86400,
            }
        )
