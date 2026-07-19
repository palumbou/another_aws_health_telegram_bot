"""StateStore round-trips against the fake DynamoDB table."""

from src.state import EventRecord, StateStore

ARN = "arn:aws:health:global::event/CLOUDFRONT/X/Y"


def _store(fake_table, now=1784447040, ttl_days=90):
    return StateStore(fake_table, ttl_days=ttl_days, clock=lambda: now)


def test_get_unknown_arn_returns_none(fake_table):
    assert _store(fake_table).get(ARN) is None


def test_put_get_round_trip_with_decimal_normalization(fake_table):
    store = _store(fake_table)
    record = EventRecord(
        event_arn=ARN,
        last_log_timestamp=1784449080,
        last_status="2",
        telegram_messages={"16": 101, "144": 102},
        closed=False,
        first_seen=1784447040,
    )
    store.put(record)
    loaded = store.get(ARN)
    assert loaded == record
    # DynamoDB Decimals must come back as plain ints.
    assert isinstance(loaded.last_log_timestamp, int)
    assert all(isinstance(value, int) for value in loaded.telegram_messages.values())


def test_ttl_is_written_from_clock(fake_table):
    now = 1784447040
    store = _store(fake_table, now=now, ttl_days=90)
    store.put(EventRecord(ARN, 0, "1", {"16": 1}, False, now))
    assert int(fake_table.items[ARN]["ttl"]) == now + 90 * 86400


def test_closed_flag_round_trip(fake_table):
    store = _store(fake_table)
    store.put(EventRecord(ARN, 10, "0", {"16": 1}, True, 5))
    assert store.get(ARN).closed is True
