"""Telegram sender tests — payload shape and error redaction, no network."""

from __future__ import annotations

import pytest
import requests

from gardenwater.telegram import TelegramError, send_message
from tests.fakes import FakeResponse, FakeSession

TOKEN = "123456:SECRET-TOKEN"
CHAT = "4242"


def test_success_sends_plain_text_payload() -> None:
    session = FakeSession(FakeResponse(payload={"ok": True}))
    send_message(TOKEN, CHAT, "hello garden", session=session)
    url, kwargs = session.calls[0]
    assert session.methods == ["POST"]
    assert url == f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    assert kwargs["json"] == {"chat_id": CHAT, "text": "hello garden"}
    assert "parse_mode" not in kwargs["json"]  # D15: plain text only
    assert kwargs["timeout"] == 15


def test_ok_false_raises_with_description() -> None:
    session = FakeSession(
        FakeResponse(payload={"ok": False, "description": "chat not found"})
    )
    with pytest.raises(TelegramError, match="chat not found"):
        send_message(TOKEN, CHAT, "hi", session=session)


def test_http_error_body_is_redacted() -> None:
    session = FakeSession(
        FakeResponse(status_code=400, text=f"bad request /bot{TOKEN}/ oops")
    )
    with pytest.raises(TelegramError) as info:
        send_message(TOKEN, CHAT, "hi", session=session)
    assert "HTTP 400" in str(info.value)
    assert "SECRET-TOKEN" not in str(info.value)


def test_network_error_is_redacted() -> None:
    exc = requests.ConnectionError(
        f"Max retries exceeded with url: /bot{TOKEN}/sendMessage"
    )
    session = FakeSession(exception=exc)
    with pytest.raises(TelegramError) as info:
        send_message(TOKEN, CHAT, "hi", session=session)
    assert "request failed" in str(info.value)
    assert "SECRET-TOKEN" not in str(info.value)


def test_non_json_response_raises() -> None:
    session = FakeSession(FakeResponse(json_error=True))
    with pytest.raises(TelegramError, match="not JSON"):
        send_message(TOKEN, CHAT, "hi", session=session)
