"""Pure message formatting — no I/O, no clock (CLAUDE.md design rule).

Wording rule: never claim the user did or did not water. Follow-up plants say
"still appears to need watering" (plan §Messages, D24 for the warning texts).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date

from gardenwater.models import Decision, PlantDecision, ReminderState

WATERING_HEADER = "🌱 Garden watering needed"
FOLLOW_UP_HEADER = "🌱 Garden watering reminder"
STATUS_WATER = "• Status: watering recommended"
STATUS_FOLLOW_UP = "• Status: still appears to need watering"


def watering_message(
    decisions: Sequence[PlantDecision],
    reminders: Mapping[str, ReminderState] | None = None,
) -> str | None:
    """One combined message for every notifying plant; None if there is none.

    `reminders` supplies the previous-reminder date for follow-up lines.
    """
    notifying = [d for d in decisions if d.notifies]
    if not notifying:
        return None
    reminders = reminders or {}
    all_follow_up = all(d.decision == Decision.FOLLOW_UP for d in notifying)
    header = FOLLOW_UP_HEADER if all_follow_up else WATERING_HEADER

    blocks = [_plant_block(d, reminders.get(d.plant)) for d in notifying]
    return header + "\n\n" + "\n\n".join(blocks)


def _plant_block(decision: PlantDecision, reminder: ReminderState | None) -> str:
    lines = [
        decision.plant.capitalize(),
        f"• Season: {decision.season}",
        f"• Water need: {decision.water_need}",
        f"• Rain last 7 days: {decision.rain_7d_mm:g} mm",
        f"• Tomorrow: {decision.tomorrow_rain_mm:g} mm",
    ]
    if decision.decision == Decision.FOLLOW_UP:
        lines.append(STATUS_FOLLOW_UP)
        if reminder is not None and decision.rain_since_reminder_mm is not None:
            lines.append(
                f"• No meaningful rain ({decision.rain_since_reminder_mm:g} mm) "
                f"since the previous reminder ({reminder.last_notified_on.isoformat()})"
            )
    else:
        lines.append(STATUS_WATER)
    return "\n".join(lines)


def provider_warning(name: str, error: str) -> str:
    """Warn-once one-liner for a provider that went ok → failed (D24)."""
    return f"⚠️ {name} failed: {_clip(error)} (using fallback)"


def all_failed_message() -> str:
    """Every provider failed — the system is blind (D9, D24)."""
    return "⚠️ Garden watering check failed: no weather data available."


def insufficient_data_message() -> str:
    """Window below `min_history_days` (D6)."""
    return (
        "⚠️ Garden watering check failed\n"
        "Not enough recent weather data to decide."
    )


def _clip(text: str, limit: int = 200) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"
