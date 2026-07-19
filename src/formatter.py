"""Compose Telegram messages (HTML) for openings, updates and closures.

Fixed texts are in Italian (the target community's language); the original
AWS texts stay in English on purpose: they are official communications and
an automatic translation would introduce ambiguity in an operational
context.

Formatting constraints implemented here:

- ``parse_mode: HTML`` with escaping of ``<``, ``>``, ``&`` in every text
  coming from AWS;
- truncation at 4096 characters with ``[...]`` (never split into multiple
  messages); only the plain-text body is truncated, so HTML tags around
  the header stay balanced;
- timestamps converted to Europe/Rome via ``zoneinfo``.
"""

import re
from datetime import datetime
from zoneinfo import ZoneInfo

# Status codes as observed on the public endpoint (to be confirmed in the
# field): keep this map here, in one place, never inline in message code.
STATUS_EMOJI = {
    "0": "\U0001f7e2",  # green circle: resolved / operational
    "1": "\U0001f535",  # blue circle: informational / investigating
    "2": "\U0001f7e1",  # yellow circle: performance degradation
    "3": "\U0001f534",  # red circle: outage
}
STATUS_LABEL = {
    "0": "Risolto",
    "1": "In indagine",
    "2": "Degrado",
    "3": "Disservizio",
}
UNKNOWN_EMOJI = "⚪"  # white circle for statuses outside the known map
UNKNOWN_LABEL = "Stato sconosciuto"

TIMEZONE = ZoneInfo("Europe/Rome")
DASHBOARD_URL = "https://health.aws.amazon.com/health/status"
MAX_MESSAGE_LENGTH = 4096
TRUNCATION_MARKER = " [...]"

# A truncation cut can leave a dangling partial HTML entity ("&am");
# strip it so Telegram never sees malformed HTML.
_PARTIAL_ENTITY = re.compile(r"&[A-Za-z#0-9]{0,9}$")


def escape_html(text):
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def status_emoji(status):
    return STATUS_EMOJI.get(str(status), UNKNOWN_EMOJI)


def status_badge(status):
    """"🟡 Degrado" — emoji plus Italian label for a status code."""
    return f"{status_emoji(status)} {STATUS_LABEL.get(str(status), UNKNOWN_LABEL)}"


def format_timestamp(timestamp):
    """Epoch seconds → "19/07/2026 09:44 CEST" in Europe/Rome."""
    moment = datetime.fromtimestamp(int(timestamp), tz=TIMEZONE)
    return moment.strftime("%d/%m/%Y %H:%M %Z")


def format_duration(seconds):
    """Seconds → "3h 24min" (or "24min" under one hour)."""
    minutes = max(0, int(seconds)) // 60
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h {minutes}min"
    if minutes:
        return f"{minutes}min"
    return "meno di 1 minuto"


def region_display(event):
    """Human-readable region: "Global", "Frankfurt", or the region code."""
    if event.region == "global":
        return "Global"
    return event.region_name or event.region


def _truncate(body, allowed):
    if len(body) <= allowed:
        return body
    cut = body[: max(0, allowed - len(TRUNCATION_MARKER))]
    cut = _PARTIAL_ENTITY.sub("", cut).rstrip()
    return cut + TRUNCATION_MARKER


def _compose(header, body, footer):
    """Join the three blocks, truncating only the body to fit the limit."""
    frame_length = len(header) + len(footer) + 4  # the joining blank lines
    body = _truncate(body, MAX_MESSAGE_LENGTH - frame_length)
    blocks = [header] + ([body] if body else []) + [footer]
    return "\n\n".join(blocks)


def _latest_log_timestamp(event):
    if event.event_log:
        return event.event_log[-1].timestamp
    return event.date


def opening_message(event):
    """First message for a newly observed event."""
    header = (
        f"{status_emoji(event.status)} "
        f"<b>{escape_html(event.service_name)} — {escape_html(region_display(event))}</b>"
    )
    parts = []
    if event.summary:
        parts.append(f"<b>{escape_html(event.summary)}</b>")
    if event.event_log and event.event_log[-1].message:
        parts.append(escape_html(event.event_log[-1].message))
    body = "\n\n".join(parts)
    footer = (
        f"\U0001f550 {format_timestamp(_latest_log_timestamp(event))}\n"
        f"\U0001f517 {DASHBOARD_URL}"
    )
    return _compose(header, body, footer)


def update_message(event, new_entries, previous_status):
    """Update sent as a reply to the opening message of each topic.

    Multiple entries accumulated between two polls are merged into one
    message. A status transition is highlighted in the header.
    """
    header = f"{status_emoji(event.status)} <b>Aggiornamento</b>"
    if str(previous_status) != str(event.status):
        header += f" — {status_badge(previous_status)} → {status_badge(event.status)}"
    body = "\n\n".join(
        escape_html(entry.message) for entry in new_entries if entry.message
    )
    latest = new_entries[-1].timestamp if new_entries else _latest_log_timestamp(event)
    footer = f"\U0001f550 {format_timestamp(latest)}"
    return _compose(header, body, footer)


def _impacted_service_names(event):
    """Unique display names from impacted_service_status_changes, in order."""
    names = []
    for change in event.status_changes:
        name = str(change.get("service_name") or change.get("service") or "").strip()
        if name and name not in names:
            names.append(name)
    return names


def closure_message(event, first_seen):
    """Final message with total duration and involved services."""
    header = (
        f"{STATUS_EMOJI['0']} <b>RISOLTO — {escape_html(event.service_name)} "
        f"({escape_html(region_display(event))})</b>"
    )
    start = event.date or first_seen
    end = event.end_time or _latest_log_timestamp(event)
    parts = [f"Durata: {format_duration(end - start)}"]
    services = _impacted_service_names(event)
    if services:
        listing = "\n".join(f"• {escape_html(name)}" for name in services)
        parts.append(f"Servizi coinvolti durante l'evento:\n{listing}")
    body = "\n\n".join(parts)
    footer = f"\U0001f550 {format_timestamp(end)}"
    return _compose(header, body, footer)
