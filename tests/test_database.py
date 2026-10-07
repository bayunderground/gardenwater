"""Database tests (plan Phase 2). Uses :memory: — never touches the network."""

from __future__ import annotations

from datetime import date

from gardenwater import database as db
from gardenwater.models import Decision, PlantDecision, ReminderState
from gardenwater.weather.models import DailyWeather


def fresh_conn():
    return db.connect(":memory:")


def make_decision(
    day: date,
    plant: str = "peach",
    decision: Decision = Decision.WATER,
    notification_type: str | None = "WATER",
) -> PlantDecision:
    return PlantDecision(
        decision_date=day,
        plant=plant,
        season="summer",
        water_need="high",
        decision=decision,
        reason="got 13% of target, below 65%",
        rain_7d_mm=5.0,
        effective_target_mm=40.0,
        rain_fraction=0.125,
        deficit_mm=35.0,
        heat_factor=1.0,
        tomorrow_rain_mm=0.0,
        rain_since_reminder_mm=None,
        provider="open-meteo",
        notification_type=notification_type,
    )


def test_schema_idempotent() -> None:
    conn = fresh_conn()
    db.init_schema(conn)
    db.init_schema(conn)  # second call must be a no-op, not an error
    tables = {
        row["name"]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert {"weather_daily", "provider_status", "watering_decisions", "reminder_state"} <= tables


def test_connect_creates_file_database(tmp_path) -> None:
    path = tmp_path / "garden.db"
    conn = db.connect(path)
    conn.close()
    assert path.exists()
    conn2 = db.connect(path)  # reopening the existing file works
    assert conn2.execute("SELECT count(*) AS n FROM weather_daily").fetchone()["n"] == 0
    conn2.close()


def test_upsert_overwrites_same_date() -> None:
    """Today's provisional row is replaced by the next fetch (plan D4)."""
    conn = fresh_conn()
    db.upsert_weather(conn, DailyWeather(date(2026, 10, 7), 1.0, 15.0, 20.0, 1.0), "open-meteo")
    db.upsert_weather(conn, DailyWeather(date(2026, 10, 7), 4.2, 16.0, 21.0, 1.4), "weatherapi")
    rows = db.read_window(conn, date(2026, 10, 7), 7)
    assert len(rows) == 1
    assert rows[0].precipitation_mm == 4.2
    assert rows[0].et0_mm == 1.4
    provider = conn.execute("SELECT provider FROM weather_daily").fetchone()["provider"]
    assert provider == "weatherapi"
    fetched_at = conn.execute("SELECT fetched_at FROM weather_daily").fetchone()["fetched_at"]
    assert "T" in fetched_at  # ISO timestamp written


def test_window_query_orders_and_limits_dates() -> None:
    conn = fresh_conn()
    for day in range(1, 11):
        db.upsert_weather(conn, DailyWeather(date(2026, 10, day), float(day)), "open-meteo")
    rows = db.read_window(conn, date(2026, 10, 8), 7)
    assert [row.date.day for row in rows] == [2, 3, 4, 5, 6, 7, 8]
    assert rows[0].precipitation_mm == 2.0  # oldest first


def test_window_excludes_missing_and_out_of_range_days() -> None:
    """Missing days are absent (caller scales, plan D6); far days are excluded."""
    conn = fresh_conn()
    db.upsert_weather(conn, DailyWeather(date(2026, 10, 1), 1.0), "open-meteo")
    db.upsert_weather(conn, DailyWeather(date(2026, 10, 3), 3.0), "open-meteo")
    db.upsert_weather(conn, DailyWeather(date(2026, 10, 4), 4.0), "open-meteo")
    db.upsert_weather(conn, DailyWeather(date(2026, 9, 20), 9.0), "open-meteo")  # before window
    rows = db.read_window(conn, date(2026, 10, 4), 4)  # 2026-10-01..04
    assert [row.date.isoformat() for row in rows] == ["2026-10-01", "2026-10-03", "2026-10-04"]
    assert all(row.date != date(2026, 9, 20) for row in rows)


def test_decision_roundtrip() -> None:
    conn = fresh_conn()
    decision = make_decision(date(2026, 10, 7))
    db.upsert_decision(conn, decision)
    rows = db.decisions_for_date(conn, date(2026, 10, 7))
    assert len(rows) == 1
    got = rows[0]
    assert got.decision == Decision.WATER
    assert got.reason == "got 13% of target, below 65%"
    assert got.rain_fraction == 0.125
    assert got.notification_type == "WATER"
    assert got.notification_status == "none"


def test_decision_upsert_replaces_same_day_same_plant() -> None:
    conn = fresh_conn()
    day = date(2026, 10, 7)
    db.upsert_decision(conn, make_decision(day, decision=Decision.WATER))
    db.upsert_decision(conn, make_decision(day, decision=Decision.NO_ACTION,
                                           notification_type=None))
    rows = db.decisions_for_date(conn, day)
    assert len(rows) == 1
    assert rows[0].decision == Decision.NO_ACTION
    assert rows[0].notification_type is None


def test_notification_sent_lifecycle() -> None:
    conn = fresh_conn()
    day = date(2026, 10, 7)
    db.upsert_decision(conn, make_decision(day))
    assert not db.notification_sent_on(conn, day)
    db.mark_notifications(conn, day, "sent")
    assert db.notification_sent_on(conn, day)
    # only notifying rows are marked; a non-notifying neighbour stays 'none'
    db.upsert_decision(conn, make_decision(day, plant="tomato",
                                           decision=Decision.POSTPONE,
                                           notification_type=None))
    statuses = {
        row["plant"]: row["notification_status"]
        for row in conn.execute("SELECT plant, notification_status FROM watering_decisions")
    }
    assert statuses == {"peach": "sent", "tomato": "none"}
    # a different day is unaffected
    assert not db.notification_sent_on(conn, date(2026, 10, 8))


def test_sent_status_survives_same_day_redecide() -> None:
    """Plan D12: re-running the same day never re-sends, even if the decision flips."""
    conn = fresh_conn()
    day = date(2026, 10, 7)
    db.upsert_decision(conn, make_decision(day, decision=Decision.WATER))
    db.mark_notifications(conn, day, "sent")
    db.upsert_decision(conn, make_decision(day, decision=Decision.POSTPONE,
                                           notification_type=None))
    assert db.notification_sent_on(conn, day)


def test_failed_status_allows_retry() -> None:
    """Plan D11: a failed send must not count as delivered."""
    conn = fresh_conn()
    day = date(2026, 10, 7)
    db.upsert_decision(conn, make_decision(day))
    db.mark_notifications(conn, day, "failed")
    assert not db.notification_sent_on(conn, day)


def test_reminder_lifecycle() -> None:
    conn = fresh_conn()
    assert db.get_reminder(conn, "peach") is None
    db.set_reminder(conn, ReminderState("peach", date(2026, 10, 5), 1))
    reminder = db.get_reminder(conn, "peach")
    assert reminder == ReminderState("peach", date(2026, 10, 5), 1)
    db.set_reminder(conn, ReminderState("peach", date(2026, 10, 7), 2))
    assert db.get_reminder(conn, "peach").reminders_count == 2
    db.clear_reminder(conn, "peach")
    assert db.get_reminder(conn, "peach") is None


def test_provider_status_transitions() -> None:
    conn = fresh_conn()
    assert db.get_provider_status(conn, "open-meteo") is None

    db.save_provider_status(conn, db.ProviderStatus(
        provider="open-meteo", state="failed", failed_since="2026-10-07T19:00:00+00:00",
        last_error="HTTP 500", updated_at="2026-10-07T19:00:00+00:00"))
    status = db.get_provider_status(conn, "open-meteo")
    assert status.state == "failed"
    assert status.failed_since == "2026-10-07T19:00:00+00:00"
    assert status.last_error == "HTTP 500"
    assert status.last_notified_at is None

    # still failing: failure start is kept, error refreshed
    db.save_provider_status(conn, db.ProviderStatus(
        provider="open-meteo", state="failed", failed_since="2026-10-07T19:00:00+00:00",
        last_error="timeout", updated_at="2026-10-08T19:00:00+00:00"))
    status = db.get_provider_status(conn, "open-meteo")
    assert status.failed_since == "2026-10-07T19:00:00+00:00"
    assert status.last_error == "timeout"

    # recovered: back to ok, failure start cleared, notification state kept
    db.save_provider_status(conn, db.ProviderStatus(
        provider="open-meteo", state="ok", last_notified_at="2026-10-07T19:00:01+00:00",
        updated_at="2026-10-09T19:00:00+00:00"))
    status = db.get_provider_status(conn, "open-meteo")
    assert status.state == "ok"
    assert status.failed_since is None
    assert status.last_notified_at == "2026-10-07T19:00:01+00:00"
