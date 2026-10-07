"""Message formatting tests — exact text, wording rule, no I/O."""

from __future__ import annotations

from datetime import date

from gardenwater.messages import (
    all_failed_message,
    insufficient_data_message,
    provider_warning,
    watering_message,
)
from gardenwater.models import Decision, PlantDecision, ReminderState

TODAY = date(2026, 10, 7)


def make_decision(**overrides) -> PlantDecision:
    base = dict(
        decision_date=TODAY,
        plant="peach",
        season="summer",
        water_need="high",
        decision=Decision.WATER,
        reason="got 12% of target",
        rain_7d_mm=5.0,
        effective_target_mm=40.0,
        rain_fraction=0.125,
        deficit_mm=35.0,
        heat_factor=1.0,
        tomorrow_rain_mm=0.0,
        rain_since_reminder_mm=None,
    )
    base.update(overrides)
    return PlantDecision(**base)


def test_single_water_plant_exact_text() -> None:
    text = watering_message([make_decision()])
    assert text == (
        "🌱 Garden watering needed\n"
        "\n"
        "Peach\n"
        "• Season: summer\n"
        "• Water need: high\n"
        "• Rain last 7 days: 5 mm\n"
        "• Tomorrow: 0 mm\n"
        "• Status: watering recommended"
    )


def test_multi_plant_is_one_combined_message() -> None:
    text = watering_message([
        make_decision(plant="peach"),
        make_decision(plant="tomato", season="autumn", water_need="low",
                      rain_7d_mm=4.87, tomorrow_rain_mm=12.5),
    ])
    assert text.startswith("🌱 Garden watering needed\n\nPeach\n")
    assert "\n\nTomato\n• Season: autumn\n• Water need: low\n" in text
    assert "• Rain last 7 days: 4.87 mm\n• Tomorrow: 12.5 mm\n" in text


def test_follow_up_only_uses_reminder_header_and_rain_line() -> None:
    decision = make_decision(
        decision=Decision.FOLLOW_UP,
        rain_since_reminder_mm=1.5,
        tomorrow_rain_mm=0.0,
    )
    reminder = ReminderState("peach", date(2026, 10, 5), reminders_count=2)
    text = watering_message([decision], {"peach": reminder})
    assert text == (
        "🌱 Garden watering reminder\n"
        "\n"
        "Peach\n"
        "• Season: summer\n"
        "• Water need: high\n"
        "• Rain last 7 days: 5 mm\n"
        "• Tomorrow: 0 mm\n"
        "• Status: still appears to need watering\n"
        "• No meaningful rain (1.5 mm) since the previous reminder (2026-10-05)"
    )


def test_mixed_water_and_follow_up_uses_needed_header() -> None:
    text = watering_message([
        make_decision(plant="peach"),
        make_decision(plant="basil", decision=Decision.FOLLOW_UP,
                      rain_since_reminder_mm=0.0),
    ])
    assert text.startswith("🌱 Garden watering needed\n")
    assert "Peach\n" in text and "Basil\n" in text
    assert "watering recommended" in text
    assert "still appears to need watering" in text


def test_non_notifying_decisions_produce_no_message() -> None:
    assert watering_message([make_decision(decision=Decision.POSTPONE)]) is None
    assert watering_message([make_decision(decision=Decision.NO_ACTION)]) is None
    assert watering_message([]) is None


def test_messages_never_claim_the_user_did_or_did_not_water() -> None:
    texts = [
        watering_message([
            make_decision(),
            make_decision(plant="basil", decision=Decision.FOLLOW_UP,
                          rain_since_reminder_mm=0.0),
        ]),
        provider_warning("weatherapi", "HTTP 429"),
        all_failed_message(),
        insufficient_data_message(),
    ]
    for text in texts:
        lowered = text.lower()
        assert "didn't water" not in lowered
        assert "did not water" not in lowered
        assert "you didn" not in lowered


def test_provider_warning_one_liner() -> None:
    assert provider_warning("weatherapi", "HTTP 429: rate limit") == (
        "⚠️ weatherapi failed: HTTP 429: rate limit (using fallback)"
    )


def test_provider_warning_error_is_clipped_to_200_chars() -> None:
    text = provider_warning("p", "x" * 500)
    assert len(text) <= 200 + len("⚠️ p failed:  (using fallback)")
    error_part = text.removeprefix("⚠️ p failed: ").removesuffix(" (using fallback)")
    assert len(error_part) == 200
    assert error_part.endswith("…")


def test_all_failed_and_insufficient_texts() -> None:
    assert all_failed_message() == (
        "⚠️ Garden watering check failed: no weather data available."
    )
    assert insufficient_data_message() == (
        "⚠️ Garden watering check failed\n"
        "Not enough recent weather data to decide."
    )
