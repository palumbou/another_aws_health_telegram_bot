"""Telegram client: retry on 429/5xx, pacing, opaque token."""

import json

import pytest

from src.telegram import TelegramClient, TelegramError

TOKEN = "123456:SECRET-TOKEN-VALUE"
OK_BODY = json.dumps({"ok": True, "result": {"message_id": 42}})


class ScriptedTransport:
    """Returns pre-programmed (status, body) responses in order."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, url, payload):
        self.calls.append({"url": url, "payload": payload})
        return self.responses.pop(0)


def _client(transport, sleeps=None, **kwargs):
    recorder = sleeps if sleeps is not None else []
    return TelegramClient(
        TOKEN,
        "-1001234567890",
        transport=transport,
        sleep=recorder.append,
        **kwargs,
    ), recorder


def test_successful_send_returns_message_id():
    transport = ScriptedTransport([(200, OK_BODY)])
    client, _ = _client(transport)
    assert client.send_message("hello", message_thread_id=16) == 42
    payload = transport.calls[0]["payload"]
    assert payload["message_thread_id"] == 16
    assert payload["parse_mode"] == "HTML"
    assert "reply_to_message_id" not in payload


def test_reply_to_is_passed_through():
    transport = ScriptedTransport([(200, OK_BODY)])
    client, _ = _client(transport)
    client.send_message("update", message_thread_id=16, reply_to_message_id=101)
    assert transport.calls[0]["payload"]["reply_to_message_id"] == 101


def test_429_retries_with_retry_after():
    rate_limited = json.dumps(
        {"ok": False, "error_code": 429, "parameters": {"retry_after": 7}}
    )
    transport = ScriptedTransport([(429, rate_limited), (200, OK_BODY)])
    client, sleeps = _client(transport)
    assert client.send_message("hello", message_thread_id=16) == 42
    assert len(transport.calls) == 2
    assert 7 in sleeps


def test_5xx_retries_then_gives_up():
    transport = ScriptedTransport([(502, "bad gateway")] * 4)
    client, _ = _client(transport, max_retries=3)
    with pytest.raises(TelegramError):
        client.send_message("hello", message_thread_id=16)
    assert len(transport.calls) == 4


def test_permanent_4xx_does_not_retry():
    body = json.dumps({"ok": False, "description": "Bad Request: message is too long"})
    transport = ScriptedTransport([(400, body)])
    client, _ = _client(transport)
    with pytest.raises(TelegramError) as excinfo:
        client.send_message("hello", message_thread_id=16)
    assert len(transport.calls) == 1
    assert "message is too long" in str(excinfo.value)


def test_errors_never_leak_the_token():
    transport = ScriptedTransport([(400, "irrelevant")])
    client, _ = _client(transport)
    with pytest.raises(TelegramError) as excinfo:
        client.send_message("hello", message_thread_id=16)
    assert TOKEN not in str(excinfo.value)


def test_pacing_between_consecutive_sends():
    transport = ScriptedTransport([(200, OK_BODY), (200, OK_BODY)])
    client, sleeps = _client(transport, min_interval=1.5)
    client.send_message("one", message_thread_id=16)
    assert sleeps == []  # no pause before the first send
    client.send_message("two", message_thread_id=16)
    assert 1.5 in sleeps
