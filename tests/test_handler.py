"""End-to-end orchestration through the real StateStore and fakes.

These are the acceptance scenarios: the state machine must never produce
duplicate messages across polls, and every update must land as a reply to
the right opening message.
"""

import dataclasses

from src.handler import run
from src.health_client import LogEntry, SchemaError
from src.state import StateStore
from tests.conftest import load_fixture_events, make_config

ARN_CLOUDFRONT = (
    "arn:aws:health:global::event/CLOUDFRONT/AWS_CLOUDFRONT_OPERATIONAL_ISSUE/"
    "AWS_CLOUDFRONT_OPERATIONAL_ISSUE_TEST_2EF1D48B613"
)
ARN_MELBOURNE = (
    "arn:aws:health:ap-southeast-4::event/EC2/AWS_EC2_OPERATIONAL_ISSUE/"
    "AWS_EC2_OPERATIONAL_ISSUE_TEST_9AB2C31F001"
)


def _run(events, config, store, telegram):
    return run(config, store, telegram, fetcher=lambda endpoint: list(events))


def _with_new_entries(event, count=1, base_timestamp=1784450100, status=None):
    entries = tuple(
        LogEntry(
            timestamp=base_timestamp + index * 60,
            status=str(status or event.status),
            summary=event.summary,
            message=f"Follow-up update number {index + 1}.",
        )
        for index in range(count)
    )
    return dataclasses.replace(
        event,
        status=str(status or event.status),
        event_log=event.event_log + entries,
    )


def test_new_event_opens_once_per_matching_topic(fake_table, fake_telegram):
    config = make_config()
    store = StateStore(fake_table, ttl_days=90)
    events = load_fixture_events("currentevents_ongoing.json")

    result = _run(events, config, store, fake_telegram)

    # CloudFront (global, status 2) matches both rules; Melbourne (status 1) none.
    assert result["messages_sent"] == 2
    assert sorted(send["thread"] for send in fake_telegram.sent) == [16, 144]
    assert all(send["reply_to"] is None for send in fake_telegram.sent)

    record = store.get(ARN_CLOUDFRONT)
    assert set(record.telegram_messages) == {"16", "144"}
    # Distinct message ids per topic.
    assert len(set(record.telegram_messages.values())) == 2
    # The filtered-out event leaves no state behind.
    assert store.get(ARN_MELBOURNE) is None


def test_same_poll_repeated_sends_nothing(fake_table, fake_telegram):
    config = make_config()
    store = StateStore(fake_table, ttl_days=90)
    events = load_fixture_events("currentevents_ongoing.json")

    _run(events, config, store, fake_telegram)
    first_count = len(fake_telegram.sent)
    result = _run(events, config, store, fake_telegram)

    assert result["messages_sent"] == 0
    assert len(fake_telegram.sent) == first_count


def test_new_log_entry_becomes_one_reply_per_topic(fake_table, fake_telegram):
    config = make_config()
    store = StateStore(fake_table, ttl_days=90)
    events = load_fixture_events("currentevents_ongoing.json")
    _run(events, config, store, fake_telegram)
    opening_ids = dict(store.get(ARN_CLOUDFRONT).telegram_messages)
    fake_telegram.sent.clear()

    updated = [_with_new_entries(events[0], count=1), events[1]]
    _run(updated, config, store, fake_telegram)

    assert len(fake_telegram.sent) == 2  # one reply per topic, not per entry
    for send in fake_telegram.sent:
        assert send["reply_to"] == opening_ids[str(send["thread"])]
        assert "Follow-up update number 1." in send["text"]
        # Old, already-published entries are not repeated.
        assert "We are investigating increased 5xx" not in send["text"]


def test_multiple_new_entries_are_merged_into_one_reply(fake_table, fake_telegram):
    config = make_config()
    store = StateStore(fake_table, ttl_days=90)
    events = load_fixture_events("currentevents_ongoing.json")
    _run(events, config, store, fake_telegram)
    fake_telegram.sent.clear()

    updated = [_with_new_entries(events[0], count=3), events[1]]
    _run(updated, config, store, fake_telegram)

    assert len(fake_telegram.sent) == 2  # still one message per topic
    for send in fake_telegram.sent:
        for index in (1, 2, 3):
            assert f"Follow-up update number {index}." in send["text"]


def test_status_transition_is_mentioned(fake_table, fake_telegram):
    config = make_config()
    store = StateStore(fake_table, ttl_days=90)
    events = load_fixture_events("currentevents_ongoing.json")
    _run(events, config, store, fake_telegram)
    fake_telegram.sent.clear()

    escalated = [_with_new_entries(events[0], count=1, status="3"), events[1]]
    _run(escalated, config, store, fake_telegram)

    assert fake_telegram.sent
    for send in fake_telegram.sent:
        assert "Degrado" in send["text"]
        assert "Disservizio" in send["text"]
        assert "→" in send["text"]
    assert store.get(ARN_CLOUDFRONT).last_status == "3"


def test_closure_announced_once_then_silence(fake_table, fake_telegram):
    config = make_config()
    store = StateStore(fake_table, ttl_days=90)
    _run(load_fixture_events("currentevents_ongoing.json"), config, store, fake_telegram)
    opening_ids = dict(store.get(ARN_CLOUDFRONT).telegram_messages)
    fake_telegram.sent.clear()

    resolved = load_fixture_events("currentevents_resolved.json")
    result = _run(resolved, config, store, fake_telegram)

    assert result["messages_sent"] == 2  # one closure per topic
    for send in fake_telegram.sent:
        assert "RISOLTO" in send["text"]
        assert "3h 24min" in send["text"]
        assert "AWS Global Accelerator" in send["text"]
        assert send["reply_to"] == opening_ids[str(send["thread"])]
    assert store.get(ARN_CLOUDFRONT).closed is True

    fake_telegram.sent.clear()
    result = _run(resolved, config, store, fake_telegram)
    assert result["messages_sent"] == 0
    assert fake_telegram.sent == []


def test_event_resolved_before_first_sighting_is_ignored(fake_table, fake_telegram):
    config = make_config()
    store = StateStore(fake_table, ttl_days=90)

    result = _run(
        load_fixture_events("currentevents_resolved.json"), config, store, fake_telegram
    )

    assert result["messages_sent"] == 0
    assert store.get(ARN_CLOUDFRONT) is None


def test_schema_error_logs_and_sends_nothing(fake_table, fake_telegram, capsys):
    config = make_config()
    store = StateStore(fake_table, ttl_days=90)

    def broken_fetcher(endpoint):
        raise SchemaError("unrecognizable payload")

    result = run(config, store, fake_telegram, fetcher=broken_fetcher)

    assert result["status"] == "schema_error"
    assert result["messages_sent"] == 0
    assert fake_telegram.sent == []
    # The EMF metric record feeding the CloudWatch alarm is emitted.
    assert "SchemaParseFailures" in capsys.readouterr().out


def test_empty_payload_is_a_quiet_run(fake_table, fake_telegram):
    config = make_config()
    store = StateStore(fake_table, ttl_days=90)
    result = _run(
        load_fixture_events("currentevents_empty.json"), config, store, fake_telegram
    )
    assert result == {
        "status": "ok",
        "events_seen": 0,
        "events_opened": 0,
        "events_updated": 0,
        "events_closed": 0,
        "messages_sent": 0,
        "deferred": False,
    }


def test_message_budget_defers_and_recovers(fake_table, fake_telegram):
    config = make_config(max_messages_per_run=1)
    store = StateStore(fake_table, ttl_days=90)
    events = load_fixture_events("currentevents_ongoing.json")

    result = _run(events, config, store, fake_telegram)
    assert result["messages_sent"] == 1
    assert result["deferred"] is True
    assert set(store.get(ARN_CLOUDFRONT).telegram_messages) == {"16"}

    # Next poll (default budget): the second topic gets its opening message.
    fake_telegram.sent.clear()
    _run(events, make_config(), store, fake_telegram)
    assert [send["thread"] for send in fake_telegram.sent] == [144]
    assert set(store.get(ARN_CLOUDFRONT).telegram_messages) == {"16", "144"}
