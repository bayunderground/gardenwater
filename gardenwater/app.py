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
from datetime import date, datetime, timedelta
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
    prune_old_records,
    read_window,
    save_provider_status,
    set_reminder,
    utc_now_iso,
    upsert_decision,
)
from gardenwater.messages import (
    all_failed_message,
    dry_run_report,
    insufficient_data_message,
    provider_warning,
    watering_message,
)
from gardenwater.models import Decision, PlantDecision, ReminderState
from gardenwater.telegram import (
    TelegramError,
    require_chat_id as telegram_require_chat_id,
    require_token as telegram_require_token,
    send_message,
)
from gardenwater.weather.base import WeatherProvider
from gardenwater.weather.open_meteo import OpenMeteoProvider
from gardenwater.weather.service import AllProvidersFailed, fetch_weather
from gardenwater.weather.weatherapi import WeatherAPIProvider
from gardenwater.watering import InsufficientDataError, decide_plant

log = logging.getLogger("gardenwater")

# Defaults resolve next to the package (the repo root where garden_water.py
# lives), so cron works from any working directory (plan Phase 8): `.env`,
# `config.yaml` and the database all sit beside the script.
ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = str(ROOT / "config.yaml")
DEFAULT_DB = str(ROOT / "garden.db")
DOTENV = ROOT / ".env"

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
    load_dotenv(DOTENV)
    try:
        config = load_config(Path(args.config))
    except ConfigError as exc:
        print(redact(str(exc)), file=sys.stderr)
        return 2
    log.info(
        "garden-water starting: config=%s db=%s dry_run=%s secrets=%s",
        args.config, args.db, args.dry_run, _secrets_note(),
    )
    if args.prune:
        return prune(
            config, args.db, keep_days=args.keep_days,
            today=_parse_today(args.today),
        )
    return run(
        config,
        args.db,
        dry_run=args.dry_run,
        today=_parse_today(args.today),
    )


def prune(
    config: AppConfig,
    db_path: str | Path,
    *,
    keep_days: int = 30,
    today: date | None = None,
) -> int:
    """Delete old history rows and exit — no weather, no Telegram (cron-able).

    Keeps the newest `keep_days` days (today included), never fewer than
    `recent_days`, so the rain window can't be destroyed by an over-eager
    `--keep-days`. `reminder_state` and `provider_status` are never touched.
    """
    keep = max(keep_days, config.thresholds.recent_days)
    conn = connect(db_path)
    init_schema(conn)
    zone = ZoneInfo(config.location.timezone)
    today = today or datetime.now(zone).date()
    cutoff = today - timedelta(days=keep - 1)  # keep = days from cutoff..today
    weather_rows, decision_rows = prune_old_records(conn, cutoff)
    log.info(
        "prune: kept %d day(s) (from %s); removed %d weather row(s), "
        "%d decision row(s)",
        keep, cutoff.isoformat(), weather_rows, decision_rows,
    )
    return 0


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
        print(dry_run_report(config, decisions, tomorrow_mm))
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
            send(telegram_require_token(), telegram_require_chat_id(), text)
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
            send(telegram_require_token(), telegram_require_chat_id(), text)
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


# --- telegram plumbing -------------------------------------------------------


def _try_send(send: SendFn, text: str, what: str) -> None:
    """Best-effort send for error messages; failures are logged, never raised."""
    try:
        send(telegram_require_token(), telegram_require_chat_id(), text)
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
    parser.add_argument("--prune", action="store_true",
                        help="delete old weather/decision history and exit")
    parser.add_argument("--keep-days", type=int, default=30, metavar="N",
                        help="days to keep with --prune "
                             "(default 30, never fewer than recent_days)")
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
