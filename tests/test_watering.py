"""Watering algorithm tests — worked examples from DEVELOPMENT_PLAN.md §2.

Pure dataclasses only: no DB, no network, no clock (CLAUDE.md testing rules).
Heat factor is 1.0 everywhere unless a test provides temperature/ET₀ data.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from gardenwater.config import SeasonRequirement, load_config
from gardenwater.models import Decision, ReminderState
from gardenwater.watering import InsufficientDataError, decide_plant
from gardenwater.weather.models import DailyWeather

REPO_ROOT = Path(__file__).resolve().parent.parent
THRESHOLDS = load_config(REPO_ROOT / "config.example.yaml").thresholds

TODAY = date(2026, 7, 15)          # a summer day → season "summer"
WINDOW_START = date(2026, 7, 9)    # 7 days ending today, today included
MONTH_TO_SEASON = {3: "spring", 4: "spring", 5: "spring",
                   6: "summer", 7: "summer", 8: "summer",
                   9: "autumn", 10: "autumn", 11: "autumn",
                   12: "winter", 1: "winter", 2: "winter"}

MEDIUM = SeasonRequirement("medium", 40.0)
HIGH = SeasonRequirement("high", 40.0)
LOW = SeasonRequirement("low", 40.0)


def window(rains: list[float]) -> list[DailyWeather]:
    """Build the 7-day window ending TODAY; `rains` is one value per day.

    No temperature and no ET₀ → heat factor 1.0, matching the plan's tables.
    """
    assert len(rains) == THRESHOLDS.recent_days
    return [
        DailyWeather(date=WINDOW_START.fromordinal(WINDOW_START.toordinal() + i),
                     precipitation_mm=rain)
        for i, rain in enumerate(rains)
    ]


def decide(
    requirement: SeasonRequirement,
    rains: list[float],
    tomorrow_mm: float,
    reminder: ReminderState | None = None,
) -> object:
    return decide_plant(
        day=TODAY,
        plant="peach",
        month_to_season=MONTH_TO_SEASON,
        requirement=requirement,
        window=window(rains),
        tomorrow_mm=tomorrow_mm,
        thresholds=THRESHOLDS,
        reminder=reminder,
    )


def test_example_a_enough_rain_no_action() -> None:
    """A: summer, medium, 45/40 mm, tomorrow 0 → NO_ACTION."""
    decision = decide(MEDIUM, [45, 0, 0, 0, 0, 0, 0], tomorrow_mm=0)
    assert decision.decision == Decision.NO_ACTION
    assert "112% of target" in decision.reason or "113% of target" in decision.reason
    assert "at or above the 50% threshold" in decision.reason


def test_example_b_dry_high_waters() -> None:
    """B: summer, high, 5/40 mm, tomorrow 0 → WATER."""
    decision = decide(HIGH, [5, 0, 0, 0, 0, 0, 0], tomorrow_mm=0)
    assert decision.decision == Decision.WATER
    assert decision.notifies
    assert "below 65%" in decision.reason
    assert "12 mm" in decision.reason or "12.0 mm" in decision.reason


def test_example_c_low_need_postpones_for_tomorrow_rain() -> None:
    """C: summer, low, 8/40 mm, tomorrow 12 → POSTPONE."""
    decision = decide(LOW, [8, 0, 0, 0, 0, 0, 0], tomorrow_mm=12)
    assert decision.decision == Decision.POSTPONE
    assert not decision.notifies
    assert "12.0 mm is at or above the 5.0 mm wait limit" in decision.reason


def test_example_d_follow_up_when_still_dry() -> None:
    """D: open reminder, rain since < 5 mm, tomorrow 0 → FOLLOW_UP."""
    reminder = ReminderState("peach", date(2026, 7, 13), reminders_count=1)
    decision = decide(LOW, [8, 0, 0, 0, 0, 0, 0], tomorrow_mm=0, reminder=reminder)
    assert decision.decision == Decision.FOLLOW_UP
    assert decision.notifies
    assert decision.rain_since_reminder_mm == 0.0
    assert "since the reminder on 2026-07-13" in decision.reason
    assert "under 5.0 mm" in decision.reason


def test_example_e_reminder_but_rain_tomorrow_postpones() -> None:
    """E: open reminder, low need, tomorrow 12 → POSTPONE (reminder untouched)."""
    reminder = ReminderState("peach", date(2026, 7, 13), reminders_count=1)
    decision = decide(LOW, [8, 0, 0, 0, 0, 0, 0], tomorrow_mm=12, reminder=reminder)
    assert decision.decision == Decision.POSTPONE
    assert not decision.notifies


def test_severe_doubles_wait_limit_so_no_postpone() -> None:
    """Severe: high, 5% of target, 2 reminders, tomorrow 14 → message (D22: FOLLOW_UP).

    Without the severe doubling the same day would POSTPONE (14 ≥ 12).
    """
    reminder = ReminderState("peach", date(2026, 7, 13), reminders_count=2)
    decision = decide(HIGH, [2, 0, 0, 0, 0, 0, 0], tomorrow_mm=14, reminder=reminder)
    assert decision.decision == Decision.FOLLOW_UP  # plan table said WATER — see D22
    assert decision.notifies
    assert "severe (2 reminders)" in decision.reason
    assert "24.0 mm wait limit" in decision.reason


def test_not_severe_uses_normal_wait_limit() -> None:
    """Same tomorrow rain, but only 1 reminder → gate not met → POSTPONE at 12 mm."""
    reminder = ReminderState("peach", date(2026, 7, 13), reminders_count=1)
    decision = decide(HIGH, [2, 0, 0, 0, 0, 0, 0], tomorrow_mm=14, reminder=reminder)
    assert decision.decision == Decision.POSTPONE
    assert "severe" not in decision.reason


def test_severe_low_need_still_waits_for_big_soaking() -> None:
    """Severe-low: low, 5% of target, 3 reminders, tomorrow 12 → 12 ≥ 5×2 → POSTPONE."""
    reminder = ReminderState("peach", date(2026, 7, 13), reminders_count=3)
    decision = decide(LOW, [2, 0, 0, 0, 0, 0, 0], tomorrow_mm=12, reminder=reminder)
    assert decision.decision == Decision.POSTPONE
    assert "severe (3 reminders)" in decision.reason
    assert "10.0 mm wait limit" in decision.reason


def test_fraction_below_severe_uses_plain_threshold() -> None:
    """Plan's parenthetical: fraction 0.20 is not severe, so 14 mm postpones."""
    reminder = ReminderState("peach", date(2026, 7, 13), reminders_count=2)
    decision = decide(HIGH, [8, 0, 0, 0, 0, 0, 0], tomorrow_mm=14, reminder=reminder)
    assert decision.decision == Decision.POSTPONE
    assert "severe" not in decision.reason


def test_insufficient_data_raises() -> None:
    """Plan D6: fewer than min_history_days in the window aborts, never 'dry'."""
    short = [DailyWeather(date=date(2026, 7, 13), precipitation_mm=1.0)] * 3
    with pytest.raises(InsufficientDataError):
        decide_plant(
            day=TODAY, plant="peach", month_to_season=MONTH_TO_SEASON,
            requirement=LOW, window=short, tomorrow_mm=0.0,
            thresholds=THRESHOLDS, reminder=None,
        )
