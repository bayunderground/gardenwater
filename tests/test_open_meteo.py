"""Open-Meteo provider tests — recorded fixture + fake session, no network."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest
import requests

from gardenwater.weather.base import ProviderError
from gardenwater.weather.open_meteo import OpenMeteoProvider
from tests.fakes import FakeResponse, FakeSession

FIXTURE = Path(__file__).parent / "fixtures" / "open_meteo_forecast.json"
TODAY = date(2026, 10, 7)


def fixture_payload() -> dict:
    return json.loads(FIXTURE.read_text())


def provider_with(payload=None, **session_kwargs) -> tuple[OpenMeteoProvider, FakeSession]:
    session = FakeSession(
        FakeResponse(payload=payload if payload is not None else fixture_payload()),
        **session_kwargs,
    )
    return OpenMeteoProvider(session=session), session


def fetch(provider: OpenMeteoProvider):
    return provider.fetch(lat=52.51, lon=13.42, tz="Europe/Berlin",
                          today=TODAY, past_days=10)


def test_fixture_parses_into_history_today_forecast() -> None:
    provider, _ = provider_with()
    data = fetch(provider)
    assert len(data.history) == 10
    assert data.history[0].date == date(2026, 9, 27)
    assert data.history[0].precipitation_mm == pytest.approx(0.0)
    assert data.history[0].temperature_avg_c == pytest.approx(17.6)
    assert data.history[0].temperature_max_c == pytest.approx(23.8)
    assert data.history[0].et0_mm == pytest.approx(3.35)
    assert data.history[-1].date == date(2026, 10, 6)
    assert data.today.date == TODAY
    assert data.today.precipitation_mm == pytest.approx(0.0)
    assert data.today.temperature_max_c == pytest.approx(21.5)
    assert len(data.forecast) == 1
    assert data.forecast[0].date == date(2026, 10, 8)
    assert data.forecast[0].precipitation_mm == pytest.approx(3.6)


def test_request_has_expected_params() -> None:
    provider, session = provider_with()
    fetch(provider)
    url, kwargs = session.calls[0]
    params = kwargs["params"]
    assert url == "https://api.open-meteo.com/v1/forecast"
    assert params["latitude"] == 52.51
    assert params["longitude"] == 13.42
    assert params["timezone"] == "Europe/Berlin"
    assert params["past_days"] == 10
    assert params["forecast_days"] == 2
    assert "precipitation_sum" in params["daily"]
    assert kwargs["timeout"] == 10


def test_http_500_raises_provider_error() -> None:
    provider, _ = provider_with()
    provider._session = FakeSession(FakeResponse(status_code=500, text="boom"))
    with pytest.raises(ProviderError, match="HTTP 500"):
        fetch(provider)


def test_non_json_response_raises() -> None:
    provider, _ = provider_with()
    provider._session = FakeSession(FakeResponse(json_error=True))
    with pytest.raises(ProviderError, match="not JSON"):
        fetch(provider)


def test_network_failure_raises_provider_error() -> None:
    provider, _ = provider_with()
    provider._session = FakeSession(exception=requests.Timeout("timed out"))
    with pytest.raises(ProviderError, match="request failed"):
        fetch(provider)


def test_error_payload_raises_with_reason() -> None:
    provider, _ = provider_with(
        payload={"error": True, "reason": "Latitude or longitude invalid"}
    )
    with pytest.raises(ProviderError, match="Latitude or longitude invalid"):
        fetch(provider)


def test_missing_today_raises() -> None:
    payload = fixture_payload()
    daily = payload["daily"]
    index = daily["time"].index("2026-10-07")
    for key in daily:
        if isinstance(daily[key], list):
            daily[key] = daily[key][:index] + daily[key][index + 1:]
    provider, _ = provider_with(payload=payload)
    with pytest.raises(ProviderError, match="today missing"):
        fetch(provider)


def test_missing_tomorrow_raises() -> None:
    payload = fixture_payload()
    for key, values in payload["daily"].items():
        if isinstance(values, list):
            payload["daily"][key] = values[:-1]
    provider, _ = provider_with(payload=payload)
    with pytest.raises(ProviderError, match="tomorrow's forecast missing"):
        fetch(provider)


def test_null_precipitation_today_raises() -> None:
    payload = fixture_payload()
    index = payload["daily"]["time"].index("2026-10-07")
    payload["daily"]["precipitation_sum"][index] = None
    provider, _ = provider_with(payload=payload)
    with pytest.raises(ProviderError, match="today's precipitation"):
        fetch(provider)


def test_null_precipitation_in_history_is_skipped() -> None:
    payload = fixture_payload()
    payload["daily"]["precipitation_sum"][0] = None
    provider, _ = provider_with(payload=payload)
    data = fetch(provider)
    assert len(data.history) == 9
    assert data.history[0].date == date(2026, 9, 28)


def test_mismatched_arrays_raise() -> None:
    payload = fixture_payload()
    payload["daily"]["precipitation_sum"] = payload["daily"]["precipitation_sum"][:-1]
    provider, _ = provider_with(payload=payload)
    with pytest.raises(ProviderError, match="mismatched"):
        fetch(provider)


def test_missing_daily_block_raises() -> None:
    provider, _ = provider_with(payload={"latitude": 1.0})
    with pytest.raises(ProviderError, match="missing"):
        fetch(provider)
