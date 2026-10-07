"""WeatherAPI provider tests — recorded fixtures + fake session, no network.

Open-Meteo parser tests live in test_open_meteo.py (same rules).
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest
import requests

from gardenwater.weather.base import ProviderError
from gardenwater.weather.weatherapi import WeatherAPIProvider
from tests.fakes import FakeResponse, FakeSession

FORECAST_FIXTURE = Path(__file__).parent / "fixtures" / "weatherapi_forecast.json"
HISTORY_FIXTURE = Path(__file__).parent / "fixtures" / "weatherapi_history.json"
TODAY = date(2026, 10, 7)


def fixture(name: Path) -> dict:
    return json.loads(name.read_text())


def provider(*responses, key="test-key", **session_kwargs) -> tuple[WeatherAPIProvider, FakeSession]:
    session = FakeSession(*responses, **session_kwargs)
    return WeatherAPIProvider(api_key=key, session=session), session


def fetch(weatherapi: WeatherAPIProvider, missing=()):
    return weatherapi.fetch(lat=52.51, lon=13.42, tz="Europe/Berlin",
                            today=TODAY, past_days=10, missing_dates=missing)


def test_forecast_fixture_parses_today_and_tomorrow() -> None:
    p, _ = provider(FakeResponse(payload=fixture(FORECAST_FIXTURE)))
    data = fetch(p)
    assert data.history == []
    assert data.today.date == TODAY
    assert data.today.precipitation_mm == pytest.approx(0.01)
    assert data.today.temperature_avg_c == pytest.approx(17.9)
    assert data.today.temperature_max_c == pytest.approx(22.1)
    assert data.today.et0_mm is None
    assert len(data.forecast) == 1
    assert data.forecast[0].date == date(2026, 10, 8)
    assert data.forecast[0].precipitation_mm == pytest.approx(4.87)
    assert data.provider == "weatherapi"


def test_history_requested_only_for_missing_past_dates() -> None:
    second_history = fixture(HISTORY_FIXTURE)
    second_history["forecast"]["forecastday"][0]["date"] = "2026-10-01"
    p, session = provider(
        FakeResponse(payload=fixture(FORECAST_FIXTURE)),
        FakeResponse(payload=fixture(HISTORY_FIXTURE)),
        FakeResponse(payload=second_history),
    )
    data = fetch(p, missing=[date(2026, 9, 30), date(2026, 10, 7),
                             date(2026, 10, 1)])
    assert len(session.calls) == 3
    assert session.calls[0][0].endswith("/forecast.json")
    # today's date is skipped for history (it comes from the forecast call)
    history_calls = [c for c in session.calls if c[0].endswith("/history.json")]
    assert len(history_calls) == 2
    assert history_calls[0][1]["params"]["dt"] == "2026-09-30"
    assert history_calls[1][1]["params"]["dt"] == "2026-10-01"
    assert len(data.history) == 2
    assert data.history[0].date == date(2026, 9, 30)
    assert data.history[0].precipitation_mm == pytest.approx(0.0)
    assert data.history[0].temperature_avg_c == pytest.approx(18.5)
    assert data.history[0].temperature_max_c == pytest.approx(23.6)


def test_request_params_shape() -> None:
    p, session = provider(FakeResponse(payload=fixture(FORECAST_FIXTURE)))
    fetch(p)
    url, kwargs = session.calls[0]
    assert url == "https://api.weatherapi.com/v1/forecast.json"
    params = kwargs["params"]
    assert params["q"] == "52.51,13.42"
    assert params["days"] == 2
    assert params["key"] == "test-key"
    assert kwargs["timeout"] == 10


def test_is_configured_requires_key() -> None:
    assert WeatherAPIProvider("").is_configured() is False
    assert WeatherAPIProvider("k").is_configured() is True


def test_http_401_with_error_body_never_leaks_key() -> None:
    body = json.dumps({"error": {"code": 2006, "message": "API key is invalid"}})
    p, _ = provider(FakeResponse(status_code=401, text=body))
    with pytest.raises(ProviderError) as info:
        fetch(p)
    assert "API key is invalid" in str(info.value)
    assert "test-key" not in str(info.value)


def test_http_429_and_500_raise_provider_error() -> None:
    for status in (429, 500):
        p, _ = provider(FakeResponse(status_code=status, text="later"))
        with pytest.raises(ProviderError, match=f"HTTP {status}"):
            fetch(p)


def test_error_payload_on_http_200_raises() -> None:
    payload = {"error": {"code": 2007, "message": "Monthly call quota exceeded."}}
    p, _ = provider(FakeResponse(payload=payload))
    with pytest.raises(ProviderError, match="Monthly call quota"):
        fetch(p)


def test_network_exception_is_redacted() -> None:
    exc = requests.ConnectionError(
        "HTTPSConnectionPool: /v1/forecast.json?key=abc123secret&q=1"
    )
    p, _ = provider(exception=exc)
    with pytest.raises(ProviderError) as info:
        fetch(p)
    assert "abc123secret" not in str(info.value)
    assert "request failed" in str(info.value)


def test_non_json_response_raises() -> None:
    p, _ = provider(FakeResponse(json_error=True))
    with pytest.raises(ProviderError, match="not JSON"):
        fetch(p)


def test_missing_tomorrow_raises() -> None:
    payload = fixture(FORECAST_FIXTURE)
    payload["forecast"]["forecastday"] = payload["forecast"]["forecastday"][:1]
    p, _ = provider(FakeResponse(payload=payload))
    with pytest.raises(ProviderError, match="today and tomorrow"):
        fetch(p)


def test_null_precipitation_today_raises() -> None:
    payload = fixture(FORECAST_FIXTURE)
    payload["forecast"]["forecastday"][0]["day"]["totalprecip_mm"] = None
    p, _ = provider(FakeResponse(payload=payload))
    with pytest.raises(ProviderError, match="precipitation is missing"):
        fetch(p)


def test_forecast_starting_on_wrong_date_raises() -> None:
    payload = fixture(FORECAST_FIXTURE)
    payload["forecast"]["forecastday"][0]["date"] = "2026-10-06"
    p, _ = provider(FakeResponse(payload=payload))
    with pytest.raises(ProviderError, match="expected 2026-10-07"):
        fetch(p)


def test_empty_history_response_raises() -> None:
    p, _ = provider(
        FakeResponse(payload=fixture(FORECAST_FIXTURE)),
        FakeResponse(payload={"forecast": {"forecastday": []}}),
    )
    with pytest.raises(ProviderError, match="no history for 2026-09-30"):
        fetch(p, missing=[date(2026, 9, 30)])


def test_history_date_mismatch_raises() -> None:
    p, _ = provider(
        FakeResponse(payload=fixture(FORECAST_FIXTURE)),
        FakeResponse(payload=fixture(HISTORY_FIXTURE)),  # 2026-09-30
    )
    with pytest.raises(ProviderError, match="expected 2026-10-01"):
        fetch(p, missing=[date(2026, 10, 1)])
