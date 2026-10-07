"""Fallback chain: try providers in order, persist what succeeded (plan Phase 5).

Responsibilities:
- skip unconfigured providers (skipped ≠ failed, D7);
- compute which window days are still missing from SQLite and hand them to the
  provider (`missing_dates`, D23);
- on success: persist history + today (never the forecast) and record the
  provider as ok;
- on failure: record a redacted short error, remember when it first failed, and
  report a warn-once transition (`ok → failed` ⇒ warn, still failed ⇒ silent,
  `failed → ok` ⇒ recovered/reset);
- all configured providers failing raises `AllProvidersFailed` (exit 1 later).

Returns a `ServiceReport` (the plan's `(WeatherData, failures)` plus the
transition info the app needs for the notify-once warning).
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import date, timedelta

from gardenwater.config import redact
from gardenwater.database import (
    ProviderStatus,
    get_provider_status,
    read_window,
    save_provider_status,
    utc_now_iso,
    upsert_weather,
)
from gardenwater.weather.base import ProviderError, WeatherProvider
from gardenwater.weather.models import WeatherData

log = logging.getLogger(__name__)


class AllProvidersFailed(Exception):
    """Every configured provider failed — the run cannot continue (exit 1)."""

    def __init__(self, failures: list["ProviderFailure"]):
        self.failures = failures
        names = ", ".join(f.provider for f in failures) or "none configured"
        super().__init__(f"all weather providers failed: {names}")


@dataclass(frozen=True)
class ProviderFailure:
    provider: str
    error: str  # short, redacted


@dataclass(frozen=True)
class ServiceReport:
    data: WeatherData
    chosen: str
    failures: list[ProviderFailure] = field(default_factory=list)
    warnings: list[ProviderFailure] = field(default_factory=list)  # ok→failed
    recovered: list[str] = field(default_factory=list)


def fetch_weather(
    providers: list[WeatherProvider],
    conn: sqlite3.Connection,
    lat: float,
    lon: float,
    tz: str,
    today: date,
    past_days: int,
) -> ServiceReport:
    """Run the fallback chain once and persist the successful result."""
    missing = _missing_dates(conn, today, past_days)
    failures: list[ProviderFailure] = []
    warnings: list[ProviderFailure] = []
    recovered: list[str] = []

    for provider in providers:
        if not provider.is_configured():
            log.info("weather: skipping %s (not configured)", provider.name)
            continue
        try:
            data = provider.fetch(
                lat=lat, lon=lon, tz=tz, today=today,
                past_days=past_days, missing_dates=missing,
            )
        except ProviderError as exc:
            error = redact(str(exc))
            log.warning("weather: %s failed: %s", provider.name, error)
            failure = ProviderFailure(provider.name, error)
            failures.append(failure)
            if _record_failure(conn, provider.name, error):
                warnings.append(failure)
            continue

        _persist(conn, data, provider.name)
        _record_success(conn, provider.name, recovered)
        log.info(
            "weather: using %s (%d history day(s), tomorrow %.1f mm)",
            provider.name, len(data.history),
            data.forecast[0].precipitation_mm,
        )
        return ServiceReport(
            data=data, chosen=provider.name, failures=failures,
            warnings=warnings, recovered=recovered,
        )

    raise AllProvidersFailed(failures)


# --- helpers -----------------------------------------------------------------


def _missing_dates(conn: sqlite3.Connection, today: date, past_days: int) -> list[date]:
    """Window days before today that are not stored yet (plan D23)."""
    start = today - timedelta(days=past_days - 1)
    have = {w.date for w in read_window(conn, today, past_days)}
    return [
        start + timedelta(days=i)
        for i in range((today - start).days)  # start .. yesterday
        if (start + timedelta(days=i)) not in have
    ]


def _persist(conn: sqlite3.Connection, data: WeatherData, provider_name: str) -> None:
    """History + today only; the forecast is never stored (plan Phase 5)."""
    for daily in [*data.history, data.today]:
        upsert_weather(conn, daily, provider_name)


def _record_failure(conn: sqlite3.Connection, name: str, error: str) -> bool:
    """Store a failure; return True on the `ok → failed` transition (warn once)."""
    previous = get_provider_status(conn, name)
    should_warn = previous is None or previous.state != "failed"
    failed_since = (
        previous.failed_since
        if previous is not None and previous.state == "failed"
        else utc_now_iso()
    )
    save_provider_status(
        conn,
        ProviderStatus(
            provider=name, state="failed", failed_since=failed_since,
            last_error=error,
            last_notified_at=previous.last_notified_at if previous else None,
            updated_at=utc_now_iso(),
        ),
    )
    return should_warn


def _record_success(
    conn: sqlite3.Connection, name: str, recovered: list[str]
) -> None:
    """Store success; append to `recovered` when the provider had been failing."""
    previous = get_provider_status(conn, name)
    if previous is not None and previous.state == "failed":
        recovered.append(name)
        log.info("weather: %s recovered (was failing since %s)",
                 name, previous.failed_since)
    save_provider_status(
        conn,
        ProviderStatus(
            provider=name, state="ok", failed_since=None, last_error=None,
            last_notified_at=None, updated_at=utc_now_iso(),
        ),
    )
