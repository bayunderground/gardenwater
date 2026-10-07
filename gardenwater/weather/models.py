"""Normalized daily weather — the only shape providers may return.

Providers convert their raw JSON into these dataclasses; nothing outside
`weather/<provider>.py` ever touches provider-specific field names (CLAUDE.md
design rule 2). All values are in mm / °C and dates are garden-local.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date


@dataclass
class DailyWeather:
    """One calendar day. Optional fields are None when the provider lacks them."""

    date: date
    precipitation_mm: float
    temperature_avg_c: float | None = None
    temperature_max_c: float | None = None
    et0_mm: float | None = None


@dataclass
class WeatherData:
    """What one provider fetch produced for this run.

    `history` holds dates strictly before today, `today` is the provisional
    daily total (observed so far + rest-of-day forecast, plan D3), and
    `forecast` starts with tomorrow (look-ahead needs at least one entry).
    """

    provider: str
    history: list[DailyWeather]
    today: DailyWeather
    forecast: list[DailyWeather]
