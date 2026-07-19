"""Fetch and defensively parse the public AWS Health events endpoint.

The endpoint (https://health.aws.amazon.com/public/currentevents) is the
one feeding the public AWS Health dashboard. It is NOT officially
documented: AWS recommends EventBridge for programmatic ingestion and the
payload format may change without notice. For this reason every field is
read defensively (``.get()`` with a default, never direct access) and a
malformed item degrades to defaults instead of raising. Only a payload
whose overall shape is unrecognizable raises ``SchemaError``, which the
handler turns into a structured error log plus a CloudWatch metric.

Known quirks of the endpoint:

- the response body is UTF-16 encoded (a UTF-8 fallback is kept in case
  AWS changes the encoding one day);
- timestamps in ``date``, ``end_time`` and ``event_log`` are in seconds,
  while the ones in ``impacted_service_status_changes`` are milliseconds;
- the region code must be taken from the third ARN segment, not from
  ``region_name`` (which is a display name and is null for global events).
"""

import json
import logging
import urllib.error
import urllib.request
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

DEFAULT_ENDPOINT = "https://health.aws.amazon.com/public/currentevents"
USER_AGENT = "another_aws_health_telegram_bot/1.0"

# Keys under which a dict-shaped payload may hold the events list, should
# AWS ever wrap the current top-level array in an object.
_LIST_KEYS = ("currentevents", "events", "items")


class FetchError(Exception):
    """The endpoint could not be reached (network / HTTP error)."""


class SchemaError(Exception):
    """The payload could not be decoded or its shape is unrecognizable."""


@dataclass(frozen=True)
class LogEntry:
    """A single entry of an event's ``event_log`` (timestamps in seconds)."""

    timestamp: int
    status: str
    summary: str
    message: str


@dataclass(frozen=True)
class HealthEvent:
    """Normalized view of one AWS Health public event."""

    arn: str
    region: str  # region code from the ARN, "global" for non-regional events
    region_name: str  # human-readable name ("Frankfurt"), may be empty
    service: str
    service_name: str
    summary: str
    status: str  # normalized to string: "0".."3"
    date: int  # start timestamp, seconds
    end_time: int | None  # set when the event is closed, seconds
    event_log: tuple = field(default_factory=tuple)  # LogEntry, chronological
    impacted_services: dict = field(default_factory=dict)
    status_changes: tuple = field(default_factory=tuple)  # raw dicts, ms timestamps


def _to_int(value, default=None):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def region_from_arn(arn):
    """Extract the region code from a Health event ARN.

    ``arn:aws:health:eu-central-1::event/...`` → ``eu-central-1``.
    Global events use either ``global`` or an empty segment.
    """
    parts = arn.split(":")
    region = parts[3].strip() if len(parts) > 3 else ""
    return region or "global"


def decode_payload(raw):
    """Decode the raw response body into a parsed JSON value.

    The endpoint currently serves UTF-16; UTF-8 is tried as a fallback so
    that an encoding change on AWS side does not break the bot.
    """
    for encoding in ("utf-16", "utf-8-sig"):
        try:
            text = raw.decode(encoding)
        except (UnicodeError, ValueError):
            continue
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            continue
    raise SchemaError("payload is not valid JSON in UTF-16 nor UTF-8")


def _parse_log_entries(raw_log):
    entries = []
    if not isinstance(raw_log, list):
        return ()
    for raw in raw_log:
        if not isinstance(raw, dict):
            continue
        timestamp = _to_int(raw.get("timestamp"))
        if timestamp is None:
            continue
        entries.append(
            LogEntry(
                timestamp=timestamp,
                status=str(raw.get("status", "")),
                summary=str(raw.get("summary") or ""),
                message=str(raw.get("message") or ""),
            )
        )
    entries.sort(key=lambda entry: entry.timestamp)
    return tuple(entries)


def _parse_event(item):
    """Normalize one raw event dict; return None if it has no usable ARN."""
    arn = str(item.get("arn") or "").strip()
    if not arn:
        return None
    event_log = _parse_log_entries(item.get("event_log"))
    raw_changes = item.get("impacted_service_status_changes")
    status_changes = tuple(
        change for change in raw_changes if isinstance(change, dict)
    ) if isinstance(raw_changes, list) else ()
    impacted = item.get("impacted_services")
    date = _to_int(item.get("date"))
    if date is None:
        date = event_log[0].timestamp if event_log else 0
    return HealthEvent(
        arn=arn,
        region=region_from_arn(arn),
        region_name=str(item.get("region_name") or ""),
        service=str(item.get("service") or ""),
        service_name=str(item.get("service_name") or item.get("service") or ""),
        summary=str(item.get("summary") or ""),
        status=str(item.get("status", "")),
        date=date,
        end_time=_to_int(item.get("end_time")),
        event_log=event_log,
        impacted_services=impacted if isinstance(impacted, dict) else {},
        status_changes=status_changes,
    )


def parse_events(payload):
    """Turn the decoded payload into a list of HealthEvent.

    Accepts both the current top-level array and a possible future object
    wrapping the array under a known key. Anything else raises SchemaError.
    """
    if isinstance(payload, dict):
        for key in _LIST_KEYS:
            if isinstance(payload.get(key), list):
                payload = payload[key]
                break
        else:
            raise SchemaError(
                f"payload is an object without a recognizable events list "
                f"(keys: {sorted(payload.keys())[:10]})"
            )
    if not isinstance(payload, list):
        raise SchemaError(f"payload has unexpected type {type(payload).__name__}")
    events = []
    for item in payload:
        if not isinstance(item, dict):
            logger.warning("skipping non-object item in events payload")
            continue
        event = _parse_event(item)
        if event is None:
            logger.warning("skipping event item without an ARN")
            continue
        events.append(event)
    return events


def fetch_raw(endpoint=DEFAULT_ENDPOINT, timeout=20, opener=None):
    """Return the raw response body of the endpoint as bytes."""
    opener = opener or urllib.request.urlopen
    request = urllib.request.Request(
        endpoint,
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
    )
    try:
        with opener(request, timeout=timeout) as response:
            return response.read()
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise FetchError(f"cannot reach {endpoint}: {exc}") from exc


def fetch_events(endpoint=DEFAULT_ENDPOINT, timeout=20, opener=None):
    """Fetch, decode and parse the endpoint into HealthEvent objects.

    Raises FetchError on network problems and SchemaError when the payload
    cannot be interpreted.
    """
    raw = fetch_raw(endpoint, timeout=timeout, opener=opener)
    return parse_events(decode_payload(raw))
