"""Weather service tests — fallback order, persistence, notify-once (plan D8)."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from gardenwater.database import (
    connect,
    get_provider_status,
    init_schema,
    upsert_weather,
)
from gardenwater.weather.base import ProviderError
from gardenwater.weather.models import DailyWeather
from gardenwater.weather.service import (
    AllProvidersFailed,
    fetch_weather,
)
from tests.fakes import FakeProvider, make_weather_data

TODAY = date(2026, 10, 7)
PAST_DAYS = 7


def make_conn(tmp_path) -> object:
    conn = connect(tmp_path / "test.db")
    init_schema(conn)
    return conn


def run(conn, providers):
    return fetch_weather(
        providers=providers, conn=conn, lat=52.5, lon=13.4,
        tz="Europe/Berlin", today=TODAY, past_days=PAST_DAYS,
    )


def test_fallback_second_provider_wins(tmp_path) -> None:
    conn = make_conn(tmp_path)
    first = FakeProvider(name="first", error=ProviderError("boom"))
    second = FakeProvider(name="second", data=make_weather_data(provider="second"))
    report = run(conn, [first, second])
    assert report.chosen == "second"
    assert [f.provider for f in report.failures] == ["first"]
    assert len(first.calls) == 1


def test_unconfigured_provider_is_skipped_not_failed(tmp_path) -> None:
    conn = make_conn(tmp_path)
    off = FakeProvider(name="off", configured=False)
    on = FakeProvider(name="on", data=make_weather_data(provider="on"))
    report = run(conn, [off, on])
    assert report.chosen == "on"
    assert off.calls == []
    assert report.failures == []
    assert get_provider_status(conn, "off") is None  # skipped silently (D7)


def test_persists_history_and_today_but_not_forecast(tmp_path) -> None:
    conn = make_conn(tmp_path)
    history = [
        DailyWeather(date=TODAY - timedelta(days=2), precipitation_mm=4.0),
        DailyWeather(date=TODAY - timedelta(days=1), precipitation_mm=1.5),
    ]
    ok = FakeProvider(name="ok", data=make_weather_data(history=history))
    run(conn, [ok])
    rows = conn.execute(
        "SELECT date FROM weather_daily ORDER BY date"
    ).fetchall()
    dates = [r["date"] for r in rows]
    assert dates == [
        (TODAY - timedelta(days=2)).isoformat(),
        (TODAY - timedelta(days=1)).isoformat(),
        TODAY.isoformat(),
    ]
    assert (TODAY + timedelta(days=1)).isoformat() not in dates  # no forecast row


def test_missing_dates_excludes_days_already_stored(tmp_path) -> None:
    conn = make_conn(tmp_path)
    already = TODAY - timedelta(days=3)
    upsert_weather(
        conn,
        DailyWeather(date=already, precipitation_mm=9.0),
        provider="earlier-run",
    )
    ok = FakeProvider(name="ok", data=make_weather_data(provider="ok"))
    report = run(conn, [ok])
    missing = ok.calls[0]["missing_dates"]
    assert already not in missing
    assert TODAY not in missing
    assert len(missing) == PAST_DAYS - 2  # 7-day window, minus today and stored day
    # the earlier run's row survives with its own provider (mixed days in DB)
    row = conn.execute(
        "SELECT provider FROM weather_daily WHERE date = ?",
        (already.isoformat(),),
    ).fetchone()
    assert row["provider"] == "earlier-run"
    assert report.chosen == "ok"


def test_all_providers_fail_raises_with_failures(tmp_path) -> None:
    conn = make_conn(tmp_path)
    one = FakeProvider(name="one", error=ProviderError("e1"))
    two = FakeProvider(name="two", error=ProviderError("e2"))
    with pytest.raises(AllProvidersFailed) as info:
        run(conn, [one, two])
    assert [f.provider for f in info.value.failures] == ["one", "two"]
    for name in ("one", "two"):
        status = get_provider_status(conn, name)
        assert status.state == "failed"
        assert status.failed_since is not None


def test_nothing_configured_also_raises(tmp_path) -> None:
    conn = make_conn(tmp_path)
    with pytest.raises(AllProvidersFailed):
        run(conn, [FakeProvider(name="off", configured=False)])


def test_failure_warns_once_then_recovers_then_warns_again(tmp_path) -> None:
    """Plan D8 / test matrix #13: ok→failed warns, still-failed is silent.

    A warning is only observable on a successful run (all-fail runs raise
    `AllProvidersFailed`, which the app turns into its own message).
    """
    conn = make_conn(tmp_path)
    good = lambda: FakeProvider(name="good", data=make_weather_data(provider="good"))

    first_run = run(conn, [FakeProvider(name="p", error=ProviderError("down")), good()])
    assert [f.provider for f in first_run.warnings] == ["p"]
    status = get_provider_status(conn, "p")
    assert status.state == "failed"
    first_failed_since = status.failed_since

    second_run = run(
        conn,
        [FakeProvider(name="p", error=ProviderError("still down")), good()],
    )
    assert second_run.warnings == []
    assert [f.provider for f in second_run.failures] == ["p"]
    status = get_provider_status(conn, "p")
    assert status.state == "failed"
    assert status.failed_since == first_failed_since  # first-failure time kept
    assert "still down" in status.last_error

    third_run = run(conn, [FakeProvider(name="p", data=make_weather_data())])
    assert third_run.recovered == ["p"]
    assert third_run.warnings == []
    status = get_provider_status(conn, "p")
    assert status.state == "ok"
    assert status.failed_since is None
    assert status.last_error is None

    fourth_run = run(
        conn,
        [FakeProvider(name="p", error=ProviderError("down again")), good()],
    )
    assert [f.provider for f in fourth_run.warnings] == ["p"]


def test_error_is_redacted_before_storage(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("WEATHERAPI_KEY", "supersecretkey")
    conn = make_conn(tmp_path)
    bad = FakeProvider(
        name="bad",
        error=ProviderError("HTTP 401: key=supersecretkey invalid"),
    )
    good = FakeProvider(name="good", data=make_weather_data(provider="good"))
    report = run(conn, [bad, good])
    assert "supersecretkey" not in report.failures[0].error
    stored = get_provider_status(conn, "bad").last_error
    assert stored is not None
    assert "supersecretkey" not in stored
