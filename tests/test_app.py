"""App orchestration tests — fake providers, fake Telegram, real SQLite.

Covers plan Phase 7 / test matrix items #14, #15, D11, D12 and D8.
"""

from __future__ import annotations

import textwrap
from datetime import date, timedelta

import pytest

from gardenwater.app import run
from gardenwater.config import load_config
from gardenwater.database import (
    connect,
    decisions_for_date,
    get_reminder,
    init_schema,
    notification_sent_on,
)
from gardenwater.telegram import TelegramError
from gardenwater.weather.base import ProviderError
from gardenwater.weather.models import DailyWeather
from tests.fakes import FakeProvider, make_weather_data

DAY1 = date(2026, 7, 15)  # a summer day
CONFIG_YAML = textwrap.dedent(
    """
    location:
      latitude: 52.5
      longitude: 13.4
      timezone: Europe/Berlin
    season_calendar:
      spring: [3, 4, 5]
      summer: [6, 7, 8]
      autumn: [9, 10, 11]
      winter: [12, 1, 2]
    plants:
      - name: peach
        type: fruit-tree
        count: 1
    water_requirements:
      peach:
        spring: {water_need: medium, rain_target_mm_7d: 30}
        summer: {water_need: high, rain_target_mm_7d: 40}
        autumn: {water_need: low, rain_target_mm_7d: 20}
        winter: {water_need: low, rain_target_mm_7d: 10}
    """
)


class FakeSend:
    def __init__(self, fail: bool = False):
        self.calls: list[str] = []
        self.fail = fail

    def __call__(self, token: str, chat_id: str, text: str) -> None:
        if self.fail:
            raise TelegramError("telegram: boom")
        self.calls.append(text)


@pytest.fixture()
def config(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(CONFIG_YAML)
    return load_config(path)


@pytest.fixture()
def env(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")


def dry_weather(today: date, history_dates: list[date], tomorrow: float = 0.0,
                today_rain: float = 0.0):
    return make_weather_data(
        provider="fake",
        history=[DailyWeather(date=d, precipitation_mm=0.0) for d in history_dates],
        today=DailyWeather(date=today, precipitation_mm=today_rain),
        tomorrow_rain=tomorrow,
    )


def seed_days(before: date, count: int = 5) -> list[date]:
    """`count` dry days strictly before `before` (enough history for D6)."""
    return [before - timedelta(days=i) for i in range(count, 0, -1)]


def test_dry_run_sends_nothing_and_writes_no_decisions(config, env, tmp_path,
                                                       capsys):
    db = tmp_path / "garden.db"
    send = FakeSend()
    provider = FakeProvider(data=dry_weather(DAY1, seed_days(DAY1)))

    exit_code = run(config, db, dry_run=True, today=DAY1,
                    providers=[provider], send=send)

    assert exit_code == 0
    assert send.calls == []
    out = capsys.readouterr().out
    assert "Decision: WATER" in out
    assert "Reason:" in out
    assert "nothing sent, nothing written" in out
    conn = connect(db)
    init_schema(conn)
    assert decisions_for_date(conn, DAY1) == []
    assert get_reminder(conn, "peach") is None


def test_all_providers_failing_exits_1_with_all_failed_message(config, env, tmp_path):
    db = tmp_path / "garden.db"
    send = FakeSend()
    providers = [
        FakeProvider(name="one", error=ProviderError("HTTP 500")),
        FakeProvider(name="two", configured=False),
    ]

    exit_code = run(config, db, today=DAY1, providers=providers, send=send)

    assert exit_code == 1
    assert send.calls == [
        "⚠️ Garden watering check failed: no weather data available."
    ]


def test_telegram_failure_exits_3_without_reminder_state(config, env, tmp_path):
    db = tmp_path / "garden.db"
    provider = FakeProvider(data=dry_weather(DAY1, seed_days(DAY1)))

    exit_code = run(config, db, today=DAY1, providers=[provider],
                    send=FakeSend(fail=True))

    assert exit_code == 3
    conn = connect(db)
    init_schema(conn)
    assert get_reminder(conn, "peach") is None  # D11: only after a successful send
    rows = decisions_for_date(conn, DAY1)
    assert len(rows) == 1
    assert rows[0].notification_status == "failed"
    assert notification_sent_on(conn, DAY1) is False


def test_three_day_scenario_water_follow_up_then_no_action(config, env, tmp_path):
    db = tmp_path / "garden.db"
    send = FakeSend()

    # Day 1: dry everywhere → WATER, one message, streak starts at 1.
    exit1 = run(config, db, today=DAY1,
                providers=[FakeProvider(data=dry_weather(DAY1, seed_days(DAY1)))],
                send=send)
    assert exit1 == 0
    assert len(send.calls) == 1
    assert send.calls[0].startswith("🌱 Garden watering needed")
    assert "watering recommended" in send.calls[0]
    assert get_reminder(_conn(db), "peach").reminders_count == 1

    # Day 2: still dry, reminder open → FOLLOW_UP (second message).
    day2 = DAY1 + timedelta(days=1)
    exit2 = run(config, db, today=day2,
                providers=[FakeProvider(data=dry_weather(day2, []))],
                send=send)
    assert exit2 == 0
    assert len(send.calls) == 2
    assert send.calls[1].startswith("🌱 Garden watering reminder")
    assert "still appears to need watering" in send.calls[1]
    assert get_reminder(_conn(db), "peach").reminders_count == 2

    # Day 3: 30 mm of rain (75% of target) → NO_ACTION, no message, streak gone.
    day3 = DAY1 + timedelta(days=2)
    rainy = dry_weather(day3, [], today_rain=30.0)
    exit3 = run(config, db, today=day3, providers=[FakeProvider(data=rainy)],
                send=send)
    assert exit3 == 0
    assert len(send.calls) == 2  # nothing new
    assert get_reminder(_conn(db), "peach") is None


def test_same_day_rerun_never_resends(config, env, tmp_path):
    """Plan D12 / matrix #15: re-running the same day sends nothing."""
    db = tmp_path / "garden.db"
    send = FakeSend()

    run(config, db, today=DAY1,
        providers=[FakeProvider(data=dry_weather(DAY1, seed_days(DAY1)))],
        send=send)
    assert len(send.calls) == 1

    run(config, db, today=DAY1,
        providers=[FakeProvider(data=dry_weather(DAY1, seed_days(DAY1)))],
        send=send)
    assert len(send.calls) == 1
    assert notification_sent_on(connect(db), DAY1) is True


def test_provider_failure_warning_sent_once_across_two_runs(config, env, tmp_path):
    """Plan D8 / matrix #13: warning on ok→failed, silent while still failing."""
    db = tmp_path / "garden.db"
    send = FakeSend()
    day2 = DAY1 + timedelta(days=1)

    # Run 1: primary provider fails, fallback works → watering + warning.
    exit1 = run(
        config, db, today=DAY1,
        providers=[
            FakeProvider(name="primary", error=ProviderError("down")),
            FakeProvider(name="backup", data=dry_weather(DAY1, seed_days(DAY1))),
        ],
        send=send,
    )
    assert exit1 == 0
    assert len(send.calls) == 2
    assert send.calls[0].startswith("🌱 Garden watering needed")
    assert send.calls[1] == "⚠️ primary failed: down (using fallback)"

    # Run 2: primary still failing → fallback works, but no second warning.
    exit2 = run(
        config, db, today=day2,
        providers=[
            FakeProvider(name="primary", error=ProviderError("down")),
            FakeProvider(name="backup", data=dry_weather(day2, [])),
        ],
        send=send,
    )
    assert exit2 == 0
    assert len(send.calls) == 3  # only the watering message
    assert not send.calls[2].startswith("⚠️")


def _conn(db_path):
    conn = connect(db_path)
    init_schema(conn)
    return conn
