"""SQLite access: schema plus small query functions.

Stdlib `sqlite3` only — one file, no ORM, no server (CLAUDE.md constraints).
All functions take an open connection so tests can use `:memory:`.
Dates are stored as TEXT `YYYY-MM-DD` (garden-local); timestamps are ISO 8601 UTC.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from gardenwater.weather.models import DailyWeather

# Kept identical to DEVELOPMENT_PLAN.md §3; CREATE IF NOT EXISTS = idempotent.
SCHEMA = """
CREATE TABLE IF NOT EXISTS weather_daily (
  date TEXT PRIMARY KEY,            -- YYYY-MM-DD, garden timezone; today's row is provisional
  provider TEXT NOT NULL,
  precipitation_mm REAL NOT NULL,
  temperature_avg_c REAL,
  temperature_max_c REAL,
  et0_mm REAL,
  fetched_at TEXT NOT NULL          -- ISO 8601 UTC
);

CREATE TABLE IF NOT EXISTS provider_status (
  provider TEXT PRIMARY KEY,
  state TEXT NOT NULL,              -- 'ok' | 'failed'
  failed_since TEXT,
  last_error TEXT,                  -- redacted, short
  last_notified_at TEXT,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS watering_decisions (
  decision_date TEXT NOT NULL,
  plant TEXT NOT NULL,
  season TEXT NOT NULL,
  water_need TEXT NOT NULL,
  decision TEXT NOT NULL,           -- NO_ACTION | POSTPONE | WATER | FOLLOW_UP
  reason TEXT NOT NULL,
  rain_7d_mm REAL NOT NULL,
  effective_target_mm REAL NOT NULL,
  rain_fraction REAL NOT NULL,
  deficit_mm REAL NOT NULL,
  heat_factor REAL NOT NULL,
  tomorrow_rain_mm REAL NOT NULL,
  rain_since_reminder_mm REAL,
  provider TEXT NOT NULL,
  notification_type TEXT,           -- NULL | 'WATER' | 'FOLLOW_UP'
  notification_status TEXT NOT NULL DEFAULT 'none',  -- none | sent | failed
  decided_at TEXT NOT NULL,
  PRIMARY KEY (decision_date, plant)
);

CREATE TABLE IF NOT EXISTS reminder_state (
  plant TEXT PRIMARY KEY,
  last_notified_on TEXT NOT NULL,
  reminders_count INTEGER NOT NULL  -- notifications in the current streak
);
"""


def utc_now_iso() -> str:
    """Current UTC time as ISO 8601 — the one clock format in the DB."""
    return datetime.now(UTC).isoformat(timespec="seconds")


def connect(db_path: str | Path) -> sqlite3.Connection:
    """Open (or create) the database and ensure the schema exists."""
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    init_schema(conn)
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    """Create any missing tables. Safe to call on every start (plan §3)."""
    conn.executescript(SCHEMA)


# ---------------------------------------------------------------------------
# weather_daily — normalized daily weather only, never raw API responses.
# ---------------------------------------------------------------------------
def upsert_weather(conn: sqlite3.Connection, daily: "DailyWeather", provider: str) -> None:
    """Insert one day, or overwrite it (latest write wins — plan D4).

    Today's row is provisional: the next run replaces it with the final value.
    """
    conn.execute(
        """
        INSERT INTO weather_daily
          (date, provider, precipitation_mm, temperature_avg_c, temperature_max_c,
           et0_mm, fetched_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(date) DO UPDATE SET
          provider = excluded.provider,
          precipitation_mm = excluded.precipitation_mm,
          temperature_avg_c = excluded.temperature_avg_c,
          temperature_max_c = excluded.temperature_max_c,
          et0_mm = excluded.et0_mm,
          fetched_at = excluded.fetched_at
        """,
        (
            daily.date.isoformat(),
            provider,
            daily.precipitation_mm,
            daily.temperature_avg_c,
            daily.temperature_max_c,
            daily.et0_mm,
            utc_now_iso(),
        ),
    )
    conn.commit()


def read_window(conn: sqlite3.Connection, end_date: "date", days: int) -> list["DailyWeather"]:
    """Rows for the `days`-day window ending on `end_date` (inclusive), oldest first.

    Days missing from the table are simply absent — the algorithm scales or
    aborts on incomplete windows (plan D6), never treats them as dry.
    """
    start = end_date - timedelta(days=days - 1)
    rows = conn.execute(
        "SELECT * FROM weather_daily WHERE date BETWEEN ? AND ? ORDER BY date",
        (start.isoformat(), end_date.isoformat()),
    ).fetchall()
    return [_row_to_daily(row) for row in rows]


def _row_to_daily(row: sqlite3.Row) -> "DailyWeather":
    return DailyWeather(
        date=date.fromisoformat(row["date"]),
        precipitation_mm=row["precipitation_mm"],
        temperature_avg_c=row["temperature_avg_c"],
        temperature_max_c=row["temperature_max_c"],
        et0_mm=row["et0_mm"],
    )
