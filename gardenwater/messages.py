"""Pure message formatting — no I/O, no clock (CLAUDE.md design rule).

Telegram texts are in Russian (D27) — the only user-facing surface; the
`--dry-run` report, log lines and DB `reason` stay English (developer-facing).
Wording rule: never claim the user did or did not water. Follow-up plants say
"всё ещё, похоже, требует полива" (plan §Messages, D24/D27 for the texts).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date
from typing import TYPE_CHECKING

from gardenwater.models import Decision, PlantDecision, ReminderState

if TYPE_CHECKING:
    from gardenwater.config import AppConfig

WATERING_HEADER = "🌱 Нужен полив"
FOLLOW_UP_HEADER = "🌱 Напоминание о поливе"
STATUS_WATER = "• Статус: рекомендуется полив"
STATUS_FOLLOW_UP = "• Статус: всё ещё, похоже, требует полива"

# Config enums are English identifiers; translate at display time (D27).
SEASON_RU = {"spring": "весна", "summer": "лето", "autumn": "осень", "winter": "зима"}
WATER_NEED_RU = {"low": "низкая", "medium": "средняя", "high": "высокая"}


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
        f"• Сезон: {SEASON_RU.get(decision.season, decision.season)}",
        f"• Потребность в поливе: "
        f"{WATER_NEED_RU.get(decision.water_need, decision.water_need)}",
        f"• Дождь за 7 дней: {decision.rain_7d_mm:g} мм",
        f"• Дождь завтра: {decision.tomorrow_rain_mm:g} мм",
    ]
    if decision.decision == Decision.FOLLOW_UP:
        lines.append(STATUS_FOLLOW_UP)
        if reminder is not None and decision.rain_since_reminder_mm is not None:
            lines.append(
                f"• Без существенного дождя ({decision.rain_since_reminder_mm:g} мм) "
                f"с предыдущего напоминания ({reminder.last_notified_on.isoformat()})"
            )
    else:
        lines.append(STATUS_WATER)
    return "\n".join(lines)


def provider_warning(name: str, error: str) -> str:
    """Warn-once one-liner for a provider that went ok → failed (D24)."""
    return f"⚠️ {name}: ошибка — {_clip(error)} (используется резервный источник)"


def all_failed_message() -> str:
    """Every provider failed — the system is blind (D9, D24, D27)."""
    return "⚠️ Проверка полива не выполнена: данные о погоде недоступны."


def insufficient_data_message() -> str:
    """Window below `min_history_days` (D6)."""
    return (
        "⚠️ Проверка полива не выполнена\n"
        "Недостаточно свежих данных о погоде, чтобы решить, нужен ли полив."
    )


def _clip(text: str, limit: int = 200) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def dry_run_report(
    config: "AppConfig", decisions: Sequence[PlantDecision], tomorrow_mm: float
) -> str:
    """Human-readable `--dry-run` printout with inputs and reason (plan Phase 7).

    Pure: the caller prints it; no clock, no I/O.
    """
    blocks: list[str] = []
    for decision in decisions:
        requirement = config.requirement(decision.plant, decision.season)
        profile = config.thresholds.profile(decision.water_need)
        target = requirement.rain_target_mm_7d
        heat = (
            f" (×{decision.heat_factor:.2f} for heat = "
            f"{decision.effective_target_mm:g} mm)"
            if decision.heat_factor != 1.0 else ""
        )
        blocks.append(
            f"Plant: {decision.plant:<18} Season: {decision.season:<8} "
            f"Water need: {decision.water_need}\n"
            f"Rain last 7 days: {decision.rain_7d_mm:g} mm\n"
            f"Target: {target:g} mm{heat}\n"
            f"Plant got {decision.rain_fraction:.0%} of its target; it gets "
            f"watered below {profile.min_rain_fraction:.0%}\n"
            f"Tomorrow rain: {tomorrow_mm:g} mm (waits only at "
            f"{profile.skip_if_tomorrow_rain_mm:g} mm or more)\n"
            f"Decision: {decision.decision}\n"
            f"Reason: {decision.reason}"
        )
    header = "— dry run: nothing sent, nothing written —\n"
    return header + "\n\n" + "\n\n".join(blocks) + "\n"
