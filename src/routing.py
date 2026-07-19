"""Routing rules: decide which forum topics an event is published to.

Routing is expressed as declarative rules (the ``ROUTING_RULES`` JSON env
var), never as conditionals in the pipeline code. ``evaluate`` is a pure
function from (event, rules) to a list of matching rules: phase 2 (private
per-user subscriptions) will plug in here by adding destination types,
without touching the state machine.
"""

import json
from dataclasses import dataclass


@dataclass(frozen=True)
class RoutingRule:
    """One destination topic with its filters.

    ``regions`` is a frozenset of region codes, or None for the ``"*"``
    wildcard. ``min_status`` is the minimum event status (severity) that
    gets published to this topic.
    """

    name: str
    topic_id: int
    regions: frozenset | None
    min_status: int


def parse_rules(raw_json):
    """Parse and validate the ROUTING_RULES JSON. Raises ValueError."""
    try:
        data = json.loads(raw_json)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError(f"ROUTING_RULES is not valid JSON: {exc}") from exc
    if not isinstance(data, list) or not data:
        raise ValueError("ROUTING_RULES must be a non-empty JSON array")
    rules = []
    for index, item in enumerate(data):
        if not isinstance(item, dict):
            raise ValueError(f"ROUTING_RULES[{index}] must be an object")
        name = str(item.get("name") or f"rule-{index}")
        try:
            topic_id = int(item["topic_id"])
        except (KeyError, TypeError, ValueError):
            raise ValueError(f"ROUTING_RULES[{index}] ({name}): missing or invalid topic_id")
        regions_raw = item.get("regions", "*")
        if regions_raw == "*":
            regions = None
        elif isinstance(regions_raw, list) and regions_raw and all(
            isinstance(region, str) and region for region in regions_raw
        ):
            regions = frozenset(regions_raw)
        else:
            raise ValueError(
                f"ROUTING_RULES[{index}] ({name}): regions must be \"*\" "
                f"or a non-empty list of region codes"
            )
        try:
            min_status = int(item.get("min_status", 1))
        except (TypeError, ValueError):
            raise ValueError(f"ROUTING_RULES[{index}] ({name}): invalid min_status")
        rules.append(
            RoutingRule(name=name, topic_id=topic_id, regions=regions, min_status=min_status)
        )
    return tuple(rules)


def _status_value(status):
    try:
        return int(status)
    except (TypeError, ValueError):
        # An unknown status must not silence a potentially serious event:
        # treat it as the highest severity so it passes every threshold.
        return 3


def matches(rule, event):
    """Pure predicate: does this event belong to this rule's topic?"""
    if rule.regions is not None and event.region not in rule.regions:
        return False
    return _status_value(event.status) >= rule.min_status


def evaluate(event, rules):
    """Return the list of rules the event matches (possibly empty)."""
    return [rule for rule in rules if matches(rule, event)]
