"""Shared test helpers: fixture loading and in-memory fakes.

The fakes mimic the exact interfaces of their real counterparts (boto3
Table, TelegramClient) so the handler is exercised through the real
StateStore and the real orchestration code.
"""

import copy
import json
import sys
from decimal import Decimal
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import Config  # noqa: E402
from src import routing  # noqa: E402
from src.health_client import parse_events  # noqa: E402

FIXTURES_DIR = Path(__file__).parent / "fixtures"

RULES_JSON = json.dumps(
    [
        {
            "name": "key-regions",
            "topic_id": 16,
            "regions": ["eu-south-1", "eu-west-1", "eu-central-1", "us-east-1", "global"],
            "min_status": 1,
        },
        {"name": "all", "topic_id": 144, "regions": "*", "min_status": 2},
    ]
)


def load_fixture_payload(name):
    return json.loads((FIXTURES_DIR / name).read_text(encoding="utf-8"))


def load_fixture_events(name):
    return parse_events(load_fixture_payload(name))


def _to_dynamo(value):
    """Mimic boto3's number handling: ints come back as Decimal."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return Decimal(str(value))
    if isinstance(value, dict):
        return {key: _to_dynamo(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_to_dynamo(item) for item in value]
    return value


class FakeTable:
    """In-memory stand-in for a boto3 DynamoDB Table."""

    def __init__(self):
        self.items = {}

    def get_item(self, Key):
        item = self.items.get(Key["event_arn"])
        return {"Item": copy.deepcopy(item)} if item else {}

    def put_item(self, Item):
        self.items[Item["event_arn"]] = _to_dynamo(copy.deepcopy(Item))


class FakeTelegram:
    """Records sends and hands out incrementing message ids."""

    def __init__(self):
        self.sent = []
        self._next_id = 100

    def send_message(self, text, message_thread_id, reply_to_message_id=None):
        self._next_id += 1
        self.sent.append(
            {
                "text": text,
                "thread": int(message_thread_id),
                "reply_to": reply_to_message_id,
                "message_id": self._next_id,
            }
        )
        return self._next_id


def make_config(**overrides):
    defaults = {
        "table_name": "test-table",
        "chat_id": "-1001234567890",
        "rules": routing.parse_rules(RULES_JSON),
        "bot_token_secret_arn": "arn:aws:secretsmanager:eu-west-1:123456789012:secret:test",
        "max_messages_per_run": 15,
        "state_ttl_days": 90,
    }
    defaults.update(overrides)
    return Config(**defaults)


@pytest.fixture
def fake_table():
    return FakeTable()


@pytest.fixture
def fake_telegram():
    return FakeTelegram()
