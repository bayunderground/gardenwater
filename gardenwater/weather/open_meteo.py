"""Open-Meteo provider (keyless, first in the fallback chain).

API doc: https://open-meteo.com/en/docs (verified 2026-10-07, docs/api-notes.md).
Fields used: daily.time, daily.precipitation_sum (mm),
daily.temperature_2m_mean (°C), daily.temperature_2m_max (°C),
daily.et0_fao_evapotranspiration (mm), utc_offset_seconds (timezone check).

Flow: build request → check response → parse → convert to WeatherData.
"""

from __future__ import annotations

from datetime import date, datetime

import requests

from gardenwater.config import redact
from gardenwater.weather.base import ProviderError
from gardenwater.weather.models import DailyWeather, WeatherData

BASE_URL = "https://api.open-meteo.com/v1/forecast"
TIMEOUT_SECONDS = 10
DAILY_FIELDS = (
    "precipitation_sum,temperature_2m_mean,temperature_2m_max,"
    "et0_fao_evapotranspiration"
)


class OpenMeteoProvider:
    name = "open-meteo"

    def __init__(self, session=None):
        self._session = session or requests

    def is_configured(self) -> bool:
        return True  # no API key needed

    def fetch(
        self,
        lat: float,
        lon: float,
        tz: str,
        today: date,
        past_days: int,
        missing_dates=None,  # noqa: ARG002 — archived forecast covers the window
    ) -> WeatherData:
        # 1. Build request.
        params = {
            "latitude": lat,
            "longitude": lon,
            "daily": DAILY_FIELDS,
            "timezone": tz,
            "past_days": past_days,
            "forecast_days": 2,  # tomorrow minimum, plus slack
        }
        try:
            response = self._session.get(
                BASE_URL, params=params, timeout=TIMEOUT_SECONDS
            )
        except requests.RequestException as exc:
            raise ProviderError(redact(f"open-meteo: request failed: {exc}")) from None

        # 2. Check response.
        if response.status_code != 200:
            body = response.text[:200] if response.text else ""
            raise ProviderError(
                redact(f"open-meteo: HTTP {response.status_code}: {body}")
            )
        try:
            payload = response.json()
        except ValueError:
            raise ProviderError("open-meteo: response is not JSON") from None
        if payload.get("error"):
            reason = payload.get("reason", "unknown error")
            raise ProviderError(redact(f"open-meteo: {reason}"))

        # 3. Parse + 4. convert.
        return self._parse(payload, today)

    def _parse(self, payload: dict, today: date) -> WeatherData:
        daily = payload.get("daily") or {}
        times = daily.get("time")
        precip = daily.get("precipitation_sum")
        if not times or precip is None:
            raise ProviderError("open-meteo: daily.time/precipitation_sum missing")
        if len(precip) != len(times):
            raise ProviderError("open-meteo: mismatched daily arrays")

        tmax = daily.get("temperature_2m_max") or [None] * len(times)
        tmean = daily.get("temperature_2m_mean") or [None] * len(times)
        et0 = daily.get("et0_fao_evapotranspiration") or [None] * len(times)
        if not (len(tmax) == len(tmean) == len(et0) == len(times)):
            raise ProviderError("open-meteo: mismatched daily arrays")

        rows: list[tuple[date, DailyWeather]] = []
        for raw_day, day_rain, avg, mx, evap in zip(
            times, precip, tmean, tmax, et0
        ):
            try:
                day = datetime.strptime(raw_day, "%Y-%m-%d").date()
            except (TypeError, ValueError):
                raise ProviderError(f"open-meteo: bad date {raw_day!r}") from None
            rows.append(
                (
                    day,
                    DailyWeather(
                        date=day,
                        precipitation_mm=day_rain,  # type: ignore[arg-type]
                        temperature_avg_c=avg,
                        temperature_max_c=mx,
                        et0_mm=evap,
                    ),
                )
            )

        history = [
            w for day, w in rows if day < today and w.precipitation_mm is not None
        ]
        today_rows = [w for day, w in rows if day == today]
        forecast = [w for day, w in rows if day > today and w.precipitation_mm is not None]

        if len(today_rows) != 1:
            raise ProviderError("open-meteo: today missing from daily data")
        if today_rows[0].precipitation_mm is None:
            raise ProviderError("open-meteo: today's precipitation is missing")
        if not forecast:
            raise ProviderError("open-meteo: tomorrow's forecast missing")

        return WeatherData(
            history=history, today=today_rows[0], forecast=forecast,
            provider=self.name,
        )
