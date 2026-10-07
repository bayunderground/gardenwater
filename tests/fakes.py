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
        self.methods = []

    def get(self, url, **kwargs):
        return self._request("GET", url, kwargs)

    def post(self, url, **kwargs):
        return self._request("POST", url, kwargs)

    def _request(self, method, url, kwargs):
        self.calls.append((url, kwargs))
        self.methods.append(method)
        if self._exception is not None:
            raise self._exception
        if not self._responses:
            raise AssertionError("FakeSession: no response left")
        return self._responses.pop(0)


class FakeProvider:
    """Canned WeatherProvider for service/app tests (no network, no real API)."""

    def __init__(self, name="fake", configured=True, data=None, error=None):
        self.name = name
        self._configured = configured
        self._data = data
        self._error = error
        self.calls = []

    def is_configured(self) -> bool:
        return self._configured

    def fetch(self, lat, lon, tz, today, past_days, missing_dates=()):
        self.calls.append({
            "lat": lat, "lon": lon, "tz": tz, "today": today,
            "past_days": past_days, "missing_dates": list(missing_dates),
        })
        if self._error is not None:
            raise self._error
        return self._data


def make_weather_data(provider="fake", history=(), today=None, tomorrow_rain=3.0):
    """Build a WeatherData with sane defaults for tests."""
    from datetime import date as _date

    from gardenwater.weather.models import DailyWeather, WeatherData

    today_weather = today or DailyWeather(
        date=_date(2026, 10, 7), precipitation_mm=0.0
    )
    forecast = [
        DailyWeather(date=_date(2026, 10, 8), precipitation_mm=tomorrow_rain)
    ]
    return WeatherData(
        provider=provider, history=list(history), today=today_weather,
        forecast=forecast,
    )
