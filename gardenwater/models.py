"""Core domain types shared by watering decisions, the database and messages."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from enum import StrEnum


class Decision(StrEnum):
    """The four possible outcomes for a plant on a given day (CLAUDE.md rule 5).

    Only WATER and FOLLOW_UP produce Telegram text; POSTPONE is log/DB only.
    """

    NO_ACTION = "NO_ACTION"
    POSTPONE = "POSTPONE"
    WATER = "WATER"
    FOLLOW_UP = "FOLLOW_UP"


@dataclass(frozen=True)
class ReminderState:
    """The open reminder streak for one plant (row in `reminder_state`).

    Created/updated only after a Telegram message was actually received
    (plan D11), deleted when the plant needs no action.
    """

    plant: str
    last_notified_on: date
    reminders_count: int


@dataclass(frozen=True)
class PlantDecision:
    """Everything decided about one plant today; maps 1:1 to a `watering_decisions` row.

    Carries all numbers needed for the DB row, the `--dry-run` printout and the
    Telegram message, plus a plain-sentence `reason` for explainability.
    """

    decision_date: date
    plant: str
    season: str
    water_need: str
    decision: Decision
    reason: str
    rain_7d_mm: float
    effective_target_mm: float
    rain_fraction: float
    deficit_mm: float
    heat_factor: float
    tomorrow_rain_mm: float
    rain_since_reminder_mm: float | None
    # Filled by the app AFTER deciding (watering.py must not know providers,
    # CLAUDE.md rule 1): `dataclasses.replace(decision, provider=...)`.
    provider: str = ""
    notification_type: str | None = None  # None | "WATER" | "FOLLOW_UP"
    notification_status: str = "none"     # "none" | "sent" | "failed"

    @property
    def notifies(self) -> bool:
        """True when this decision is supposed to produce Telegram text."""
        return self.decision in (Decision.WATER, Decision.FOLLOW_UP)
