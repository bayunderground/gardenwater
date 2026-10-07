"""Telegram sender: one function, plain text, no polling (CLAUDE.md layout).

`sendMessage` only (D15: no `parse_mode` — plain text avoids escaping bugs).
The bot token travels in the URL, so every error passes through `redact()`.
"""

from __future__ import annotations

import os

import requests

from gardenwater.config import redact

API_URL = "https://api.telegram.org"
TIMEOUT_SECONDS = 15


class TelegramError(Exception):
    """Telegram refused or could not be reached; message already redacted."""


def send_message(token: str, chat_id: str, text: str, session=None) -> None:
    """Send one plain-text message; raise TelegramError on any problem."""
    url = f"{API_URL}/bot{token}/sendMessage"
    payload = {"chat_id": chat_id, "text": text}
    http = session or requests
    try:
        response = http.post(url, json=payload, timeout=TIMEOUT_SECONDS)
    except requests.RequestException as exc:
        raise TelegramError(redact(f"telegram: request failed: {exc}")) from None

    if response.status_code != 200:
        body = response.text[:200] if response.text else ""
        raise TelegramError(
            redact(f"telegram: HTTP {response.status_code}: {body}")
        )
    try:
        body = response.json()
    except ValueError:
        raise TelegramError("telegram: response is not JSON") from None
    if not body.get("ok"):
        description = body.get("description", "unknown error")
        raise TelegramError(redact(f"telegram: {description}"))


def require_token() -> str:
    """The bot token from the environment; missing counts as a send failure."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if not token:
        raise TelegramError("telegram: TELEGRAM_BOT_TOKEN is not set")
    return token


def require_chat_id() -> str:
    """The target chat id from the environment."""
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not chat_id:
        raise TelegramError("telegram: TELEGRAM_CHAT_ID is not set")
    return chat_id
