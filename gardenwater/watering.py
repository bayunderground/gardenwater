"""Pure watering decision logic (plan §2, §4).

No HTTP, no SQLite, no `datetime.now()`, no provider names (CLAUDE.md rule 1):
every input is passed in, every output is returned. `decide_plant()` reads top
to bottom and mirrors the plan's `# Step 1 … # Step 8`.
"""

from __future__ import annotations

from datetime import date
from typing import Mapping, Sequence

from gardenwater.config import Thresholds
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


def _mean(values: Sequence[float]) -> float:
    return sum(values) / len(values)


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))
