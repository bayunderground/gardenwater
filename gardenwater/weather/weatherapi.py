"""WeatherAPI.com provider (second in the fallback chain, `WEATHERAPI_KEY`).

API doc: https://www.weatherapi.com/docs/ (verified 2026-10-07,
docs/api-notes.md). Fields used: forecastday[].date, day.totalprecip_mm (mm),
day.avgtemp_c (°C), day.maxtemp_c (°C). ET₀ is Enterprise-only → None.

Free plan: history is one date per request, so `/history.json` is called only
for `missing_dates` (dates not yet in SQLite) — never a date range.

Flow: build request → check response → parse → convert to WeatherData.
⚠️ The key travels in the URL: every error passes through `redact()`.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Sequence

import requests

from gardenwater.config import redact
from gardenwater.weather.base import ProviderError
from gardenwater.weather.models import DailyWeather, WeatherData

BASE_URL = "https://api.weatherapi.com/v1"
TIMEOUT_SECONDS = 10


class WeatherAPIProvider:
    name = "weatherapi"

    def __init__(self, api_key: str, session=None):
        self._api_key = api_key
        self._session = session or requests

    def is_configured(self) -> bool:
        return bool(self._api_key)

    def fetch(
        self,
        lat: float,
        lon: float,
        tz: str,
        today: date,
        past_days: int,
        missing_dates: Sequence[date] = (),
    ) -> WeatherData:
        location = f"{lat},{lon}"

        # 1. Build requests: one forecast call + one call per missing history day.
        forecast_payload = self._get(
            "forecast.json", {"q": location, "days": 2}
        )
        history: list[DailyWeather] = []
        for day in sorted(missing_dates):
            if day >= today:
                continue
            payload = self._get(
                "history.json", {"q": location, "dt": day.isoformat()}
            )
            history.append(self._parse_day(payload, day))

        # 2-4. Parse the forecast into today + tomorrow.
        today_weather, tomorrow_weather = self._parse_forecast(forecast_payload, today)
        history = sorted(history, key=lambda w: w.date)
        return WeatherData(
            history=history,
            today=today_weather,
            forecast=[tomorrow_weather],
            provider=self.name,
        )

    # --- request + response checking ----------------------------------------

    def _get(self, method: str, params: dict) -> dict:
        url = f"{BASE_URL}/{method}"
        request_params = {**params, "key": self._api_key}
        try:
            response = self._session.get(
                url, params=request_params, timeout=TIMEOUT_SECONDS
            )
        except requests.RequestException as exc:
            raise ProviderError(redact(f"weatherapi: request failed: {exc}")) from None
        if response.status_code != 200:
            body = response.text[:200] if response.text else ""
            raise ProviderError(
                redact(
                    f"weatherapi: HTTP {response.status_code}: {body}"
                )
            )
        try:
            payload = response.json()
        except ValueError:
            raise ProviderError("weatherapi: response is not JSON") from None
        error = payload.get("error")
        if error:
            raise ProviderError(
                redact(f"weatherapi: {error.get('message', 'unknown error')}")
            )
        return payload

    # --- parsing --------------------------------------------------------------

    def _parse_forecast(self, payload: dict, today: date) -> tuple[DailyWeather, DailyWeather]:
        forecastday = ((payload.get("forecast") or {}).get("forecastday")) or []
        if len(forecastday) < 2:
            raise ProviderError("weatherapi: forecast needs today and tomorrow")
        day0, day1 = forecastday[0], forecastday[1]
        today_weather = self._day_entry(day0, "today")
        tomorrow_weather = self._day_entry(day1, "tomorrow")
        if today_weather.date != today:
            raise ProviderError(
                f"weatherapi: forecast starts {today_weather.date}, expected {today}"
            )
        for weather in (today_weather, tomorrow_weather):
            if weather.precipitation_mm is None:
                raise ProviderError(
                    f"weatherapi: {weather.date} precipitation is missing"
                )
        return today_weather, tomorrow_weather

    def _parse_day(self, payload: dict, expected: date) -> DailyWeather:
        forecastday = ((payload.get("forecast") or {}).get("forecastday")) or []
        if not forecastday:
            raise ProviderError(f"weatherapi: no history for {expected}")
        weather = self._day_entry(forecastday[0], "history")
        if weather.date != expected:
            raise ProviderError(
                f"weatherapi: history returned {weather.date}, expected {expected}"
            )
        if weather.precipitation_mm is None:
            raise ProviderError(
                f"weatherapi: {weather.date} precipitation is missing"
            )
        return weather

    def _day_entry(self, entry: dict, what: str) -> DailyWeather:
        raw_date = entry.get("date")
        try:
            day = datetime.strptime(raw_date, "%Y-%m-%d").date()
        except (TypeError, ValueError):
            raise ProviderError(f"weatherapi: bad {what} date {raw_date!r}") from None
        day_data = entry.get("day") or {}
        return DailyWeather(
            date=day,
            precipitation_mm=day_data.get("totalprecip_mm"),
            temperature_avg_c=day_data.get("avgtemp_c"),
            temperature_max_c=day_data.get("maxtemp_c"),
            et0_mm=None,  # ET₀ is Business/Enterprise only
        )
