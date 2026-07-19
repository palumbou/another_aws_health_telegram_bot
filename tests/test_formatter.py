"""Message composition: escaping, truncation, timezone, transitions."""

import dataclasses

from src import formatter
from tests.conftest import load_fixture_events


def _ongoing_event():
    return load_fixture_events("currentevents_ongoing.json")[0]


def _resolved_event():
    return load_fixture_events("currentevents_resolved.json")[0]


def test_timestamp_in_europe_rome():
    # 1784447040 = 2026-07-19 07:44 UTC = 09:44 CEST
    assert formatter.format_timestamp(1784447040) == "19/07/2026 09:44 CEST"


def test_duration_formats():
    assert formatter.format_duration(12240) == "3h 24min"
    assert formatter.format_duration(1500) == "25min"
    assert formatter.format_duration(30) == "meno di 1 minuto"


def test_opening_message_content():
    text = formatter.opening_message(_ongoing_event())
    assert "Amazon CloudFront — Global" in text
    assert "Increased 5xx Errors" in text
    assert "packet processing subsystem" in text  # latest log entry
    assert formatter.DASHBOARD_URL in text
    assert text.startswith(formatter.STATUS_EMOJI["2"])


def test_html_escaping_of_aws_texts():
    event = dataclasses.replace(_ongoing_event(), service_name="a <b> & c")
    text = formatter.opening_message(event)
    assert "a &lt;b&gt; &amp; c" in text
    assert "<b> & c" not in text


def test_update_message_merges_entries_and_shows_transition():
    event = dataclasses.replace(_ongoing_event(), status="3")
    entries = event.event_log
    text = formatter.update_message(event, list(entries), previous_status="2")
    assert "Aggiornamento" in text
    assert f"{formatter.STATUS_EMOJI['2']} Degrado → {formatter.STATUS_EMOJI['3']} Disservizio" in text
    assert "We are investigating" in text
    assert "packet processing subsystem" in text


def test_update_message_without_transition():
    event = _ongoing_event()
    text = formatter.update_message(event, list(event.event_log[-1:]), previous_status="2")
    assert "→" not in text


def test_closure_message_duration_and_services():
    text = formatter.closure_message(_resolved_event(), first_seen=0)
    assert "RISOLTO" in text
    assert "Amazon CloudFront (Global)" in text
    assert "Durata: 3h 24min" in text
    assert "• Amazon CloudFront" in text
    assert "• AWS Global Accelerator" in text
    assert "19/07/2026 13:08 CEST" in text


def test_truncation_keeps_message_under_limit():
    event = _ongoing_event()
    huge_entry = dataclasses.replace(event.event_log[-1], message="x" * 10000)
    event = dataclasses.replace(event, event_log=event.event_log[:-1] + (huge_entry,))
    text = formatter.opening_message(event)
    assert len(text) <= formatter.MAX_MESSAGE_LENGTH
    assert "[...]" in text
    # Header tags stay balanced: truncation only touches the body.
    assert text.count("<b>") == text.count("</b>")


def test_truncation_strips_partial_html_entity():
    # The cut lands in the middle of "&amp;": the dangling "&" is stripped.
    truncated = formatter._truncate("abc&amp;def", 10)
    assert truncated == "abc [...]"
