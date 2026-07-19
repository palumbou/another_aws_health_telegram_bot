"""Minimal Telegram Bot API client: sendMessage with retry and pacing.

Telegram allows roughly 20 messages per minute per group, a limit that is
reachable during a major AWS event. Countermeasures implemented here:

- a pause between consecutive sends (``min_interval``);
- retry with the ``retry_after`` value from HTTP 429 responses;
- exponential backoff on 5xx responses.

The per-run message cap and the merging of multiple updates into one
message live in the handler, not here.

The bot token is part of the request URL and must never be logged:
error messages carry only the HTTP status and Telegram's ``description``.
"""

import json
import logging
import time
import urllib.error
import urllib.request

logger = logging.getLogger(__name__)

API_BASE = "https://api.telegram.org"
DEFAULT_TIMEOUT = 15


class TelegramError(Exception):
    """A sendMessage call failed permanently (retries exhausted or 4xx)."""

    def __init__(self, message, status_code=None):
        super().__init__(message)
        self.status_code = status_code


class TelegramClient:
    def __init__(
        self,
        token,
        chat_id,
        transport=None,
        sleep=time.sleep,
        min_interval=1.5,
        max_retries=3,
    ):
        self._token = token
        self._chat_id = chat_id
        self._transport = transport or self._http_post
        self._sleep = sleep
        self._min_interval = min_interval
        self._max_retries = max_retries
        self._sent_count = 0

    def send_message(self, text, message_thread_id, reply_to_message_id=None):
        """Send one message to a forum topic; return its message_id.

        ``message_thread_id`` is always set so the message lands in the
        right topic. Updates and closures pass ``reply_to_message_id`` to
        thread them under the opening message.
        """
        payload = {
            "chat_id": self._chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
            "message_thread_id": int(message_thread_id),
        }
        if reply_to_message_id is not None:
            payload["reply_to_message_id"] = int(reply_to_message_id)

        if self._sent_count:
            self._sleep(self._min_interval)

        url = f"{API_BASE}/bot{self._token}/sendMessage"
        backoff = 1.0
        last_error = None
        for attempt in range(self._max_retries + 1):
            status, body = self._transport(url, payload)
            if status == 200:
                message_id = self._parse_success(body)
                self._sent_count += 1
                return message_id
            last_error = f"HTTP {status}: {_description(body)}"
            if attempt >= self._max_retries:
                break
            if status == 429:
                retry_after = _retry_after(body, default=backoff)
                logger.warning(
                    "telegram rate limited, retrying in %ss", retry_after
                )
                self._sleep(retry_after)
                backoff *= 2
                continue
            if 500 <= status < 600:
                self._sleep(backoff)
                backoff *= 2
                continue
            # Other 4xx errors are permanent: retrying would not help.
            break
        raise TelegramError(f"sendMessage failed: {last_error}", status_code=status)

    @staticmethod
    def _parse_success(body):
        try:
            data = json.loads(body)
            if data.get("ok"):
                return int(data["result"]["message_id"])
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            pass
        raise TelegramError(f"unexpected sendMessage response: {body[:200]}")

    @staticmethod
    def _http_post(url, payload):
        """Return (status_code, body_text). Never raises on HTTP errors."""
        request = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=DEFAULT_TIMEOUT) as response:
                return response.status, response.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode("utf-8", "replace")


def _description(body):
    try:
        return str(json.loads(body).get("description", ""))[:200]
    except (json.JSONDecodeError, AttributeError):
        return str(body)[:200]


def _retry_after(body, default):
    try:
        return max(1, int(json.loads(body)["parameters"]["retry_after"]))
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        return default
