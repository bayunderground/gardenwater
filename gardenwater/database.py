"""SQLite access: schema plus small query functions.

Stdlib `sqlite3` only — one file, no ORM, no server (CLAUDE.md constraints).
All functions take an open connection so tests can use `:memory:`.
Dates are stored as TEXT `YYYY-MM-DD` (garden-local); timestamps are ISO 8601 UTC.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from gardenwater.models import Decision, PlantDecision, ReminderState
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


# ---------------------------------------------------------------------------
# provider_status — drives the notify-once failure warning (plan D8).
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ProviderStatus:
    """One row of `provider_status`. All timestamps are ISO 8601 UTC."""

    provider: str
    state: str                       # 'ok' | 'failed'
    failed_since: str | None = None  # when it first failed (kept while failing)
    last_error: str | None = None    # redacted, short
    last_notified_at: str | None = None  # when we last warned about it
    updated_at: str = ""


def get_provider_status(conn: sqlite3.Connection, provider: str) -> ProviderStatus | None:
    row = conn.execute(
        "SELECT * FROM provider_status WHERE provider = ?", (provider,)
    ).fetchone()
    if row is None:
        return None
    return ProviderStatus(
        provider=row["provider"],
        state=row["state"],
        failed_since=row["failed_since"],
        last_error=row["last_error"],
        last_notified_at=row["last_notified_at"],
        updated_at=row["updated_at"],
    )


def save_provider_status(conn: sqlite3.Connection, status: ProviderStatus) -> None:
    conn.execute(
        """
        INSERT INTO provider_status
          (provider, state, failed_since, last_error, last_notified_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(provider) DO UPDATE SET
          state = excluded.state,
          failed_since = excluded.failed_since,
          last_error = excluded.last_error,
          last_notified_at = excluded.last_notified_at,
          updated_at = excluded.updated_at
        """,
        (
            status.provider,
            status.state,
            status.failed_since,
            status.last_error,
            status.last_notified_at,
            status.updated_at or utc_now_iso(),
        ),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# watering_decisions — one row per (date, plant), last write wins.
# ---------------------------------------------------------------------------
def upsert_decision(
    conn: sqlite3.Connection, decision: PlantDecision, decided_at: str | None = None
) -> None:
    conn.execute(
        """
        INSERT INTO watering_decisions
          (decision_date, plant, season, water_need, decision, reason, rain_7d_mm,
           effective_target_mm, rain_fraction, deficit_mm, heat_factor,
           tomorrow_rain_mm, rain_since_reminder_mm, provider,
           notification_type, notification_status, decided_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(decision_date, plant) DO UPDATE SET
          season = excluded.season,
          water_need = excluded.water_need,
          decision = excluded.decision,
          reason = excluded.reason,
          rain_7d_mm = excluded.rain_7d_mm,
          effective_target_mm = excluded.effective_target_mm,
          rain_fraction = excluded.rain_fraction,
          deficit_mm = excluded.deficit_mm,
          heat_factor = excluded.heat_factor,
          tomorrow_rain_mm = excluded.tomorrow_rain_mm,
          rain_since_reminder_mm = excluded.rain_since_reminder_mm,
          provider = excluded.provider,
          notification_type = excluded.notification_type,
          notification_status = CASE
            WHEN watering_decisions.notification_status = 'sent' THEN 'sent'
            ELSE excluded.notification_status
          END,
          decided_at = excluded.decided_at
        """,
        (
            decision.decision_date.isoformat(),
            decision.plant,
            decision.season,
            decision.water_need,
            str(decision.decision),
            decision.reason,
            decision.rain_7d_mm,
            decision.effective_target_mm,
            decision.rain_fraction,
            decision.deficit_mm,
            decision.heat_factor,
            decision.tomorrow_rain_mm,
            decision.rain_since_reminder_mm,
            decision.provider,
            decision.notification_type,
            decision.notification_status,
            decided_at or utc_now_iso(),
        ),
    )
    conn.commit()


def decisions_for_date(conn: sqlite3.Connection, decision_date: date) -> list[PlantDecision]:
    rows = conn.execute(
        "SELECT * FROM watering_decisions WHERE decision_date = ? ORDER BY plant",
        (decision_date.isoformat(),),
    ).fetchall()
    return [
        PlantDecision(
            decision_date=date.fromisoformat(row["decision_date"]),
            plant=row["plant"],
            season=row["season"],
            water_need=row["water_need"],
            decision=Decision(row["decision"]),
            reason=row["reason"],
            rain_7d_mm=row["rain_7d_mm"],
            effective_target_mm=row["effective_target_mm"],
            rain_fraction=row["rain_fraction"],
            deficit_mm=row["deficit_mm"],
            heat_factor=row["heat_factor"],
            tomorrow_rain_mm=row["tomorrow_rain_mm"],
            rain_since_reminder_mm=row["rain_since_reminder_mm"],
            provider=row["provider"],
            notification_type=row["notification_type"],
            notification_status=row["notification_status"],
        )
        for row in rows
    ]


def notification_sent_on(conn: sqlite3.Connection, decision_date: date) -> bool:
    """Has a watering message already been delivered on this date? (plan D12)"""
    row = conn.execute(
        "SELECT 1 FROM watering_decisions WHERE decision_date = ? "
        "AND notification_status = 'sent' LIMIT 1",
        (decision_date.isoformat(),),
    ).fetchone()
    return row is not None


def mark_notifications(conn: sqlite3.Connection, decision_date: date, status: str) -> None:
    """Set `notification_status` ('sent'|'failed') on every notifying row of the day.

    The watering message is combined (max one per run), so all its plants share
    the same outcome (plan D11).
    """
    conn.execute(
        "UPDATE watering_decisions SET notification_status = ? "
        "WHERE decision_date = ? AND notification_type IS NOT NULL",
        (status, decision_date.isoformat()),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# reminder_state — only written after a successful send (plan D11/D12).
# ---------------------------------------------------------------------------
def get_reminder(conn: sqlite3.Connection, plant: str) -> ReminderState | None:
    row = conn.execute(
        "SELECT * FROM reminder_state WHERE plant = ?", (plant,)
    ).fetchone()
    if row is None:
        return None
    return ReminderState(
        plant=row["plant"],
        last_notified_on=date.fromisoformat(row["last_notified_on"]),
        reminders_count=row["reminders_count"],
    )


def set_reminder(conn: sqlite3.Connection, reminder: ReminderState) -> None:
    conn.execute(
        """
        INSERT INTO reminder_state (plant, last_notified_on, reminders_count)
        VALUES (?, ?, ?)
        ON CONFLICT(plant) DO UPDATE SET
          last_notified_on = excluded.last_notified_on,
          reminders_count = excluded.reminders_count
        """,
        (reminder.plant, reminder.last_notified_on.isoformat(), reminder.reminders_count),
    )
    conn.commit()


def clear_reminder(conn: sqlite3.Connection, plant: str) -> None:
    conn.execute("DELETE FROM reminder_state WHERE plant = ?", (plant,))
    conn.commit()


# ---------------------------------------------------------------------------
# Pruning — remove old history; current state is never touched.
# ---------------------------------------------------------------------------
def prune_old_records(conn: sqlite3.Connection, cutoff: date) -> tuple[int, int]:
    """Delete history strictly before `cutoff`; return `(weather, decisions)` counts.

    Only the two history tables are pruned. `reminder_state` (open streaks) and
    `provider_status` (outage bookkeeping) hold *current* state and are kept.
    """
    weather = conn.execute(
        "DELETE FROM weather_daily WHERE date < ?", (cutoff.isoformat(),)
    ).rowcount
    decisions = conn.execute(
        "DELETE FROM watering_decisions WHERE decision_date < ?",
        (cutoff.isoformat(),),
    ).rowcount
    conn.commit()
    return weather, decisions
