"""Fake HTTP session for provider tests — never hit the network (CLAUDE.md)."""

from __future__ import annotations


class FakeResponse:
    def __init__(self, status_code=200, payload=None, text="", json_error=False):
        self.status_code = status_code
        self._payload = payload
        self.text = text
        self._json_error = json_error

    def json(self):
        if self._json_error:
            raise ValueError("not json")
        return self._payload


class FakeSession:
    """Replays canned responses and records every call for assertions."""

    def __init__(self, *responses, exception=None):
        self._responses = list(responses)
        self._exception = exception
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if self._exception is not None:
            raise self._exception
        if not self._responses:
            raise AssertionError("FakeSession: no response left")
        return self._responses.pop(0)
