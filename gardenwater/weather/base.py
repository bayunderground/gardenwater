"""Provider contract: what every weather provider must look like.

`ProviderError` is the ONLY failure signal a provider may raise (CLAUDE.md
design rule 2). Its message must be short and already redacted — the caller
logs it and may send it to Telegram as-is.
"""

from __future__ import annotations

from datetime import date
from typing import Protocol, Sequence, runtime_checkable

from gardenwater.weather.models import WeatherData


class ProviderError(Exception):
    """A provider failed: timeout, HTTP error, auth error, malformed payload."""


@runtime_checkable
class WeatherProvider(Protocol):
    """A source of normalized daily weather for one fixed location."""

    name: str

    def is_configured(self) -> bool:
        """False when the provider cannot run at all (e.g. no API key).

        Unconfigured providers are *skipped*, never treated as failures (D7).
        """
        ...

    def fetch(
        self,
        lat: float,
        lon: float,
        tz: str,
        today: date,
        past_days: int,
        missing_dates: Sequence[date] = (),
    ) -> WeatherData:
        """Return history + today + tomorrow for the location.

        `missing_dates` lists window days the caller has NOT persisted yet —
        providers that must request history day-by-day (WeatherAPI free plan,
        docs/api-notes.md) fetch only those; the rest may be ignored.

        Raises ProviderError on any problem (D7: timeout, network, HTTP, auth,
        malformed JSON, or missing today's/tomorrow's precipitation).
        """
        ...
