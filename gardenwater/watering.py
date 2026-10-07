"""Pure watering decision logic (plan §2, §4).

No HTTP, no SQLite, no `datetime.now()`, no provider names (CLAUDE.md rule 1):
every input is passed in, every output is returned. `decide_plant()` reads top
to bottom and mirrors the plan's `# Step 1 … # Step 8`.
"""

from __future__ import annotations

from datetime import date
from typing import Mapping, Sequence

from gardenwater.config import SeasonRequirement, Thresholds
from gardenwater.models import Decision, PlantDecision, ReminderState
from gardenwater.weather.models import DailyWeather


class InsufficientDataError(Exception):
    """Fewer days in the rain window than `min_history_days` (plan D6).

    Raised before any number is used, so missing data is never treated as dry.
    The caller turns this into exit code 1 and the "check failed" message.
    """


def season_for(day: date, month_to_season: Mapping[int, str]) -> str:
    """Which season `day` falls in — the calendar maps months to seasons."""
    return month_to_season[day.month]


def rain_in_window(
    window: Sequence[DailyWeather], thresholds: Thresholds
) -> tuple[float, int]:
    """Total rain over the window plus how many days actually have data (§2 step 2).

    Days missing from the window are excluded and the sum is scaled to the full
    window length (`sum × recent_days / n`, plan D6) — never treated as dry.
    Returns `(rain_7d_mm, days_found)`.
    """
    days_found = len(window)
    total_mm = sum(day.precipitation_mm for day in window)
    if days_found and days_found < thresholds.recent_days:
        total_mm = total_mm * thresholds.recent_days / days_found
    return total_mm, days_found


def heat_factor(window: Sequence[DailyWeather], thresholds: Thresholds) -> float:
    """Demand multiplier from heat, always in `[1.0, max_factor]` (§2 step 3).

    ET₀ is preferred when at least half the window days have it; otherwise the
    mean daily max temperature is used. No data (or adjustment disabled) means
    normal demand: 1.0. Cool weather never *lowers* demand below 1.0.
    """
    heat = thresholds.heat
    if not heat.enabled or not window:
        return 1.0

    et0_values = [day.et0_mm for day in window if day.et0_mm is not None]
    if len(et0_values) * 2 >= len(window):
        factor = _mean(et0_values) / heat.et0_baseline_mm_day
        return _clamp(factor, 1.0, heat.max_factor)

    tmax_values = [day.temperature_max_c for day in window if day.temperature_max_c is not None]
    if tmax_values:
        factor = 1.0 + (_mean(tmax_values) - heat.temp_threshold_c) * heat.temp_step_per_c
        return _clamp(factor, 1.0, heat.max_factor)

    return 1.0


def decide_plant(
    day: date,
    plant: str,
    month_to_season: Mapping[int, str],
    requirement: SeasonRequirement,
    window: Sequence[DailyWeather],
    tomorrow_mm: float,
    thresholds: Thresholds,
    reminder: ReminderState | None,
) -> PlantDecision:
    """Decide what to do about one plant today (plan §2, steps 1-8).

    `window` holds the `recent_days` days ending today (today included).
    `tomorrow_mm` is tomorrow's forecast rain. `reminder` is the plant's open
    reminder streak, or None. Returns a PlantDecision with a plain-sentence
    `reason`; never touches I/O (CLAUDE.md rule 1).
    """
    # Step 1: season -> this plant's water_need and rain target.
    season = season_for(day, month_to_season)
    water_need = requirement.water_need
    rain_target = requirement.rain_target_mm_7d

    # Step 2: rain over the window, scaled if days are missing (never "dry").
    rain_7d, days_found = rain_in_window(window, thresholds)
    if days_found < thresholds.min_history_days:
        raise InsufficientDataError(
            f"{days_found} day(s) of weather in the window, "
            f"need {thresholds.min_history_days}"
        )

    # Step 3: heat raises demand, never lowers it.
    factor = heat_factor(window, thresholds)

    # Step 4: the target the plant would like to see over the window.
    effective_target = rain_target * factor

    # Step 5: how much of that target actually arrived?
    if effective_target <= 0:
        rain_fraction = 1.0  # zero target: nothing can be needed
    else:
        rain_fraction = rain_7d / effective_target
    deficit = max(0.0, effective_target - rain_7d)

    profile = thresholds.profile(water_need)
    min_fraction = profile.min_rain_fraction
    got = (
        f"got {rain_fraction:.0%} of target ({rain_7d:.1f}/{effective_target:.1f} mm)"
    )

    # Step 6: wet enough -> no action (the app clears any open reminder).
    if rain_fraction >= min_fraction:
        return _make(
            day, plant, season, water_need, Decision.NO_ACTION,
            reason=f"{got}, at or above the {min_fraction:.0%} threshold for {water_need}",
            rain_7d=rain_7d, effective_target=effective_target,
            rain_fraction=rain_fraction, deficit=deficit, factor=factor,
            tomorrow_mm=tomorrow_mm, rain_since=None,
        )

    # Step 7: is rain coming that justifies waiting one more day?
    wait_threshold = profile.skip_if_tomorrow_rain_mm
    severe = thresholds.severe
    is_severe = (
        rain_fraction <= severe.rain_fraction
        and reminder is not None
        and reminder.reminders_count >= severe.min_reminders
    )
    if is_severe:
        wait_threshold *= severe.wait_multiplier
        severe_note = f"; severe ({reminders_text(reminder)} reminders)"
    else:
        severe_note = ""
    if tomorrow_mm >= wait_threshold:
        return _make(
            day, plant, season, water_need, Decision.POSTPONE,
            reason=(
                f"{got}, below {min_fraction:.0%}; tomorrow {tomorrow_mm:.1f} mm is at or "
                f"above the {wait_threshold:.1f} mm wait limit{severe_note}"
            ),
            rain_7d=rain_7d, effective_target=effective_target,
            rain_fraction=rain_fraction, deficit=deficit, factor=factor,
            tomorrow_mm=tomorrow_mm,
            rain_since=_rain_since_reminder(window, reminder),
        )

    # Step 8: it's time to water -> WATER or FOLLOW_UP (both notify).
    coming = (
        f"{got}, below {min_fraction:.0%}; tomorrow {tomorrow_mm:.1f} mm is under the "
        f"{wait_threshold:.1f} mm wait limit{severe_note}"
    )
    rain_since = _rain_since_reminder(window, reminder)
    if reminder is None:
        return _make(
            day, plant, season, water_need, Decision.WATER, reason=coming,
            rain_7d=rain_7d, effective_target=effective_target,
            rain_fraction=rain_fraction, deficit=deficit, factor=factor,
            tomorrow_mm=tomorrow_mm, rain_since=None,
        )
    if rain_since is not None and rain_since < thresholds.meaningful_rain_mm:
        return _make(
            day, plant, season, water_need, Decision.FOLLOW_UP,
            reason=(
                f"{coming}; {rain_since:.1f} mm since the reminder on "
                f"{reminder.last_notified_on.isoformat()} is under "
                f"{thresholds.meaningful_rain_mm:.1f} mm"
            ),
            rain_7d=rain_7d, effective_target=effective_target,
            rain_fraction=rain_fraction, deficit=deficit, factor=factor,
            tomorrow_mm=tomorrow_mm, rain_since=rain_since,
        )
    return _make(
        day, plant, season, water_need, Decision.WATER,
        reason=(
            f"{coming}; {rain_since:.1f} mm since the reminder on "
            f"{reminder.last_notified_on.isoformat()} ended the streak"
        ),
        rain_7d=rain_7d, effective_target=effective_target,
        rain_fraction=rain_fraction, deficit=deficit, factor=factor,
        tomorrow_mm=tomorrow_mm, rain_since=rain_since,
    )


def _rain_since_reminder(
    window: Sequence[DailyWeather], reminder: ReminderState | None
) -> float | None:
    """Rain on dates *after* the reminder day, through today (plan D19)."""
    if reminder is None:
        return None
    return sum(
        day.precipitation_mm
        for day in window
        if day.date > reminder.last_notified_on
    )


def _make(
    day: date,
    plant: str,
    season: str,
    water_need: str,
    decision: Decision,
    reason: str,
    rain_7d: float,
    effective_target: float,
    rain_fraction: float,
    deficit: float,
    factor: float,
    tomorrow_mm: float,
    rain_since: float | None,
) -> PlantDecision:
    """Assemble the decision row; notification fields are set after sending."""
    return PlantDecision(
        decision_date=day,
        plant=plant,
        season=season,
        water_need=water_need,
        decision=decision,
        reason=reason,
        rain_7d_mm=rain_7d,
        effective_target_mm=effective_target,
        rain_fraction=rain_fraction,
        deficit_mm=deficit,
        heat_factor=factor,
        tomorrow_rain_mm=tomorrow_mm,
        rain_since_reminder_mm=rain_since,
    )


def reminders_text(reminder: ReminderState | None) -> str:
    """How many reminders are in the streak (for reason sentences)."""
    return str(reminder.reminders_count) if reminder else "0"


def _mean(values: Sequence[float]) -> float:
    return sum(values) / len(values)


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))
