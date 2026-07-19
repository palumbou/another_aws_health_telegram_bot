"""Routing rule parsing and pure evaluation."""

import json

import pytest

from src import routing
from tests.conftest import RULES_JSON, load_fixture_events


def _event(region="eu-central-1", status="2"):
    events = load_fixture_events("currentevents_ongoing.json")
    import dataclasses

    return dataclasses.replace(events[0], region=region, status=status)


def test_parse_rules_valid():
    rules = routing.parse_rules(RULES_JSON)
    assert len(rules) == 2
    assert rules[0].topic_id == 16
    assert rules[0].regions == frozenset(
        ["eu-south-1", "eu-west-1", "eu-central-1", "us-east-1", "global"]
    )
    assert rules[1].regions is None  # wildcard


@pytest.mark.parametrize(
    "raw",
    [
        "not json",
        "{}",
        "[]",
        json.dumps([{"name": "x"}]),  # missing topic_id
        json.dumps([{"topic_id": 1, "regions": []}]),  # empty regions list
        json.dumps([{"topic_id": 1, "regions": [1, 2]}]),  # non-string regions
        json.dumps([{"topic_id": 1, "min_status": "high"}]),
    ],
)
def test_parse_rules_invalid(raw):
    with pytest.raises(ValueError):
        routing.parse_rules(raw)


def test_region_filter():
    rules = routing.parse_rules(RULES_JSON)
    matched = routing.evaluate(_event(region="ap-southeast-4", status="1"), rules)
    assert matched == []


def test_wildcard_with_min_status():
    rules = routing.parse_rules(RULES_JSON)
    matched = routing.evaluate(_event(region="ap-southeast-4", status="2"), rules)
    assert [rule.topic_id for rule in matched] == [144]


def test_multi_topic_match():
    rules = routing.parse_rules(RULES_JSON)
    matched = routing.evaluate(_event(region="eu-central-1", status="3"), rules)
    assert [rule.topic_id for rule in matched] == [16, 144]


def test_resolved_status_matches_nothing():
    rules = routing.parse_rules(RULES_JSON)
    assert routing.evaluate(_event(status="0"), rules) == []


def test_unknown_status_is_treated_as_max_severity():
    rules = routing.parse_rules(RULES_JSON)
    matched = routing.evaluate(_event(region="sa-east-1", status="banana"), rules)
    assert [rule.topic_id for rule in matched] == [144]
