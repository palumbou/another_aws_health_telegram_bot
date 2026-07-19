"""Decoding and defensive parsing of the public endpoint payload."""

import json

import pytest

from src.health_client import (
    SchemaError,
    decode_payload,
    parse_events,
    region_from_arn,
)
from tests.conftest import load_fixture_payload

ARN_REGIONAL = "arn:aws:health:eu-central-1::event/EC2/AWS_EC2_ISSUE/AWS_EC2_ISSUE_X1"
ARN_GLOBAL = "arn:aws:health:global::event/CLOUDFRONT/AWS_CLOUDFRONT_ISSUE/AWS_CF_X1"


def test_decodes_utf16_payload():
    payload = load_fixture_payload("currentevents_ongoing.json")
    raw = json.dumps(payload).encode("utf-16")
    assert decode_payload(raw) == payload


def test_falls_back_to_utf8():
    payload = load_fixture_payload("currentevents_ongoing.json")
    raw = json.dumps(payload).encode("utf-8")
    assert decode_payload(raw) == payload


def test_undecodable_bytes_raise_schema_error():
    with pytest.raises(SchemaError):
        decode_payload(b"\xff\xfe\x00not json at all")


def test_non_json_text_raises_schema_error():
    with pytest.raises(SchemaError):
        decode_payload("plain text, no json".encode("utf-16"))


def test_parses_list_payload():
    events = parse_events(load_fixture_payload("currentevents_ongoing.json"))
    assert len(events) == 2
    assert events[0].region == "global"
    assert events[1].region == "ap-southeast-4"


def test_parses_dict_wrapped_payload():
    payload = {"currentevents": load_fixture_payload("currentevents_ongoing.json")}
    assert len(parse_events(payload)) == 2


def test_unrecognizable_shapes_raise_schema_error():
    with pytest.raises(SchemaError):
        parse_events("a string")
    with pytest.raises(SchemaError):
        parse_events({"something": "else"})


def test_missing_fields_degrade_to_defaults():
    events = parse_events([{"arn": ARN_REGIONAL}])
    assert len(events) == 1
    event = events[0]
    assert event.region == "eu-central-1"
    assert event.status == ""
    assert event.end_time is None
    assert event.event_log == ()
    assert event.impacted_services == {}


def test_items_without_arn_are_skipped():
    events = parse_events([{"status": "2"}, "garbage", {"arn": ARN_GLOBAL, "status": "1"}])
    assert [event.arn for event in events] == [ARN_GLOBAL]


def test_event_log_is_sorted_and_defensive():
    events = parse_events(
        [
            {
                "arn": ARN_REGIONAL,
                "event_log": [
                    {"message": "second", "status": 2, "timestamp": 200},
                    {"message": "first", "status": 1, "timestamp": 100},
                    {"message": "no timestamp, dropped", "status": 1},
                    "not a dict",
                ],
            }
        ]
    )
    log = events[0].event_log
    assert [entry.message for entry in log] == ["first", "second"]


def test_region_from_arn_variants():
    assert region_from_arn(ARN_REGIONAL) == "eu-central-1"
    assert region_from_arn(ARN_GLOBAL) == "global"
    assert region_from_arn("arn:aws:health:::event/IAM/X/Y") == "global"


def test_resolved_fixture_has_end_time():
    events = parse_events(load_fixture_payload("currentevents_resolved.json"))
    assert events[0].status == "0"
    assert events[0].end_time == 1784459280


def test_empty_fixture_parses_to_no_events():
    assert parse_events(load_fixture_payload("currentevents_empty.json")) == []
