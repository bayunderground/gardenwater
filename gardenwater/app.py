"""Orchestration: one run, then exit (CLAUDE.md — no daemon, no loop).

Flow: parse args → load .env + config → init DB → fetch weather (fallback) →
decide every plant → record decisions → one combined watering message →
reminder state only after a successful send → provider warning if needed →
exit code (0 ok, 1 weather/insufficient, 2 config, 3 telegram).
"""

from __future__ import annotations

import argparse
import dataclasses
import logging
import os
import sys
from collections.abc import Callable, Sequence
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from gardenwater.config import AppConfig, ConfigError, load_config, load_dotenv, redact
from gardenwater.database import (
    clear_reminder,
    connect,
    get_provider_status,
    get_reminder,
    init_schema,
    mark_notifications,
    notification_sent_on,
    read_window,
    save_provider_status,
    set_reminder,
    utc_now_iso,
    upsert_decision,
)
from gardenwater.messages import (
    all_failed_message,
    insufficient_data_message,
    provider_warning,
    watering_message,
)
from gardenwater.models import Decision, PlantDecision, ReminderState
from gardenwater.telegram import TelegramError, send_message
from gardenwater.weather.base import WeatherProvider
from gardenwater.weather.open_meteo import OpenMeteoProvider
from gardenwater.weather.service import AllProvidersFailed, fetch_weather
from gardenwater.weather.weatherapi import WeatherAPIProvider
from gardenwater.watering import InsufficientDataError, decide_plant

log = logging.getLogger("gardenwater")

DEFAULT_CONFIG = "config.yaml"
DEFAULT_DB = "garden.db"

SendFn = Callable[[str, str, str], None]


def default_providers() -> list[WeatherProvider]:
    """Open-Meteo first (keyless), then WeatherAPI (D21: OpenWeather off)."""
    return [
        OpenMeteoProvider(),
        WeatherAPIProvider(os.environ.get("WEATHERAPI_KEY", "")),
    ]


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    _configure_logging(args.verbose)
    load_dotenv(Path(".env"))
    try:
        config = load_config(Path(args.config))
    except ConfigError as exc:
        print(redact(str(exc)), file=sys.stderr)
        return 2
    log.info(
        "garden-water starting: config=%s db=%s dry_run=%s secrets=%s",
        args.config, args.db, args.dry_run, _secrets_note(),
    )
    return run(
        config,
        args.db,
        dry_run=args.dry_run,
        today=_parse_today(args.today),
    )


def run(
    config: AppConfig,
    db_path: str | Path,
    *,
    dry_run: bool = False,
    today: date | None = None,
    providers: list[WeatherProvider] | None = None,
    send: SendFn | None = None,
) -> int:
    """Execute one complete run and return the process exit code."""
    send = send or send_message
    providers = providers or default_providers()
    conn = connect(db_path)
    init_schema(conn)

    zone = ZoneInfo(config.location.timezone)
    today = today or datetime.now(zone).date()
    season = config.season_for_month(today.month)
    log.info("date=%s season=%s timezone=%s", today.isoformat(), season,
             config.location.timezone)

    # 1. Weather (fallback chain; persistence happens inside).
    try:
        report = fetch_weather(
            providers=providers, conn=conn,
            lat=config.location.latitude, lon=config.location.longitude,
            tz=config.location.timezone, today=today,
            past_days=config.thresholds.recent_days,
        )
    except AllProvidersFailed as exc:
        for failure in exc.failures:
            log.error("weather: %s — %s", failure.provider, failure.error)
        log.error("all weather providers failed")
        if dry_run:
            print(all_failed_message())
        else:
            _try_send(send, all_failed_message(), "all-failed")
        return 1

    # 2. Decide every plant (pure logic; aborts the whole run on D6).
    window = read_window(conn, today, config.thresholds.recent_days)
    tomorrow_mm = report.data.forecast[0].precipitation_mm
    log.info("history=%d day(s), tomorrow=%.1f mm", len(window), tomorrow_mm)
    try:
        decisions = _decide_all(config, conn, report.chosen, today,
                                window, tomorrow_mm)
    except InsufficientDataError as exc:
        log.error("insufficient data: %s", exc)
        if dry_run:
            print(insufficient_data_message())
        else:
            _try_send(send, insufficient_data_message(), "insufficient-data")
        return 1

    for decision in decisions:
        log.info("%s: %s — %s", decision.plant, decision.decision,
                 decision.reason)

    # 3. Dry-run: show everything, write nothing decision-wise, send nothing.
    if dry_run:
        print(_dry_run_text(config, decisions, tomorrow_mm))
        log.info("dry run complete (no message, no decisions, no reminders)")
        return 0

    # 4. Record decisions, clear reminders of plants that need no action.
    for decision in decisions:
        upsert_decision(conn, decision)
    for decision in decisions:
        if decision.decision == Decision.NO_ACTION:
            clear_reminder(conn, decision.plant)

    # 5. One combined watering message (D12: at most once per day).
    if any(d.notifies for d in decisions) and not notification_sent_on(conn, today):
        text = watering_message(decisions, _reminders_for(conn, decisions))
        try:
            send(_telegram_token(), _telegram_chat_id(), text)
        except TelegramError as exc:
            mark_notifications(conn, today, "failed")
            log.error("telegram: watering message failed: %s", exc)
            return 3
        mark_notifications(conn, today, "sent")
        _remember_sent(conn, decisions, today)
        log.info("telegram: watering message sent (%d plant(s))",
                 sum(1 for d in decisions if d.notifies))

    # 6. Provider warning: one message, only on the ok→failed transition (D8).
    if report.warnings:
        text = "\n".join(
            provider_warning(f.provider, f.error) for f in report.warnings
        )
        try:
            send(_telegram_token(), _telegram_chat_id(), text)
        except TelegramError as exc:
            log.error("telegram: provider warning failed: %s", exc)
            return 3
        for failure in report.warnings:
            _mark_notified(conn, failure.provider)
        log.info("telegram: provider warning sent for %s",
                 ", ".join(f.provider for f in report.warnings))

    log.info("run finished: exit 0")
    return 0


# --- decision helpers --------------------------------------------------------


def _decide_all(
    config: AppConfig,
    conn,
    provider_name: str,
    today: date,
    window,
    tomorrow_mm: float,
) -> list[PlantDecision]:
    season = config.season_for_month(today.month)
    decisions: list[PlantDecision] = []
    for plant in config.plants:
        requirement = config.requirement(plant.name, season)
        reminder = get_reminder(conn, plant.name)
        decision = decide_plant(
            day=today, plant=plant.name,
            month_to_season=config.month_to_season,
            requirement=requirement, window=window,
            tomorrow_mm=tomorrow_mm, thresholds=config.thresholds,
            reminder=reminder,
        )
        decisions.append(
            dataclasses.replace(
                decision,
                provider=provider_name,
                notification_type=(
                    decision.decision.value if decision.notifies else None
                ),
            )
        )
    return decisions


def _reminders_for(conn, decisions: Sequence[PlantDecision]) -> dict[str, ReminderState]:
    out: dict[str, ReminderState] = {}
    for decision in decisions:
        if decision.notifies:
            reminder = get_reminder(conn, decision.plant)
            if reminder is not None:
                out[decision.plant] = reminder
    return out


def _remember_sent(conn, decisions: Sequence[PlantDecision], today: date) -> None:
    """Reminder rows change only after a successful send (plan D11)."""
    for decision in decisions:
        if decision.decision == Decision.WATER:
            set_reminder(conn, ReminderState(decision.plant, today, 1))
        elif decision.decision == Decision.FOLLOW_UP:
            previous = get_reminder(conn, decision.plant)
            count = previous.reminders_count + 1 if previous else 1
            set_reminder(conn, ReminderState(decision.plant, today, count))


def _mark_notified(conn, provider: str) -> None:
    status = get_provider_status(conn, provider)
    if status is not None:
        save_provider_status(
            conn,
            dataclasses.replace(status, last_notified_at=utc_now_iso()),
        )


# --- dry-run printout --------------------------------------------------------


def _dry_run_text(config: AppConfig, decisions: Sequence[PlantDecision],
                  tomorrow_mm: float) -> str:
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


# --- telegram plumbing -------------------------------------------------------


def _telegram_token() -> str:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if not token:
        raise TelegramError("telegram: TELEGRAM_BOT_TOKEN is not set")
    return token


def _telegram_chat_id() -> str:
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not chat_id:
        raise TelegramError("telegram: TELEGRAM_CHAT_ID is not set")
    return chat_id


def _try_send(send: SendFn, text: str, what: str) -> None:
    """Best-effort send for error messages; failures are logged, never raised."""
    try:
        send(_telegram_token(), _telegram_chat_id(), text)
    except TelegramError as exc:
        log.error("telegram: %s message failed: %s", what, exc)


# --- CLI and logging ---------------------------------------------------------


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="garden_water",
        description="Check the garden once and send one Telegram reminder.",
    )
    parser.add_argument("--config", default=DEFAULT_CONFIG,
                        help="path to config.yaml")
    parser.add_argument("--db", default=DEFAULT_DB, help="SQLite database path")
    parser.add_argument("--dry-run", action="store_true",
                        help="live weather, but print instead of sending")
    parser.add_argument("--today", metavar="YYYY-MM-DD",
                        help="override today's date (testing only)")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="debug logging")
    return parser.parse_args(argv)


def _parse_today(raw: str | None) -> date | None:
    if raw is None:
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError:
        raise SystemExit(f"invalid --today value: {raw!r} (want YYYY-MM-DD)")


def _configure_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stdout,
        force=True,
    )


def _secrets_note() -> str:
    from gardenwater.config import secrets_present

    present = secrets_present()
    return ", ".join(present) if present else "none"
