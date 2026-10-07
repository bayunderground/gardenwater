"""Load and validate the YAML config and the .env file.

Why this exists: every tuning knob lives in config, not in logic. The single
source of truth for threshold defaults is `DEFAULT_WATERING` below; the same
values appear (commented) in `config.example.yaml` and a test keeps them in sync.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TypeVar
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml

T = TypeVar("T")

# ---------------------------------------------------------------------------
# Defaults — the one place threshold numbers are defined (plan D20).
# Mirrored with comments in config.example.yaml (tested for equality).
# ---------------------------------------------------------------------------
DEFAULT_WATERING: dict = {
    "recent_days": 7,
    "meaningful_rain_mm": 5,       # rain at or above this ends a reminder streak
    "min_history_days": 5,         # minimum days of data inside the window
    "heat_adjustment": {
        "enabled": True,
        "et0_baseline_mm_day": 3.0,  # ET0 at/below this = normal demand
        "temp_threshold_c": 28,      # mean daily max above this = extra demand
        "temp_step_per_c": 0.03,     # +3% demand per °C above the threshold
        "max_factor": 1.5,           # demand is never raised by more than 50%
    },
    "need_profiles": {             # per water_need
        "low": {
            "min_rain_fraction": 0.30,         # water only if it got < 30% of target
            "skip_if_tomorrow_rain_mm": 5,     # a light shower is enough to wait
        },
        "medium": {
            "min_rain_fraction": 0.50,
            "skip_if_tomorrow_rain_mm": 8,
        },
        "high": {
            "min_rain_fraction": 0.65,
            "skip_if_tomorrow_rain_mm": 12,    # waits only for a real soaking
        },
    },
    "severe": {
        "rain_fraction": 0.15,     # got <= 15% of target
        "min_reminders": 2,        # at least this many reminders already sent
        "wait_multiplier": 2.0,    # then tomorrow's rain must be 2x larger to wait
    },
}

SEASONS = ("spring", "summer", "autumn", "winter")
WATER_NEEDS = ("low", "medium", "high")

ENV_KEYS = ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "WEATHERAPI_KEY", "OPENWEATHER_API_KEY")


class ConfigError(Exception):
    """Configuration is invalid. Message lists *all* problems found."""


# ---------------------------------------------------------------------------
# Config dataclasses (frozen: config is read once, never mutated).
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class LocationConfig:
    latitude: float
    longitude: float
    timezone: str  # IANA name for zoneinfo, e.g. "Europe/Berlin"


@dataclass(frozen=True)
class HeatAdjustment:
    enabled: bool
    et0_baseline_mm_day: float
    temp_threshold_c: float
    temp_step_per_c: float
    max_factor: float


@dataclass(frozen=True)
class NeedProfile:
    min_rain_fraction: float          # "water when it got less than this fraction"
    skip_if_tomorrow_rain_mm: float   # "wait if tomorrow brings at least this"


@dataclass(frozen=True)
class SevereProfile:
    rain_fraction: float
    min_reminders: int
    wait_multiplier: float


@dataclass(frozen=True)
class Thresholds:
    """The whole `watering:` section as one frozen object.

    `watering.py` receives this and contains no bare numbers (readability rule 2).
    """

    recent_days: int
    meaningful_rain_mm: float
    min_history_days: int
    heat: HeatAdjustment
    profiles: dict[str, NeedProfile]  # keyed by water_need
    severe: SevereProfile

    def profile(self, water_need: str) -> NeedProfile:
        return self.profiles[water_need]


@dataclass(frozen=True)
class PlantConfig:
    name: str
    type: str


@dataclass(frozen=True)
class SeasonRequirement:
    water_need: str
    rain_target_mm_7d: float


@dataclass(frozen=True)
class AppConfig:
    location: LocationConfig
    month_to_season: dict[int, str]   # 1..12 -> season name
    plants: tuple[PlantConfig, ...]
    water_requirements: dict[str, dict[str, SeasonRequirement]]  # plant -> season
    thresholds: Thresholds

    def season_for_month(self, month: int) -> str:
        return self.month_to_season[month]

    def requirement(self, plant: str, season: str) -> SeasonRequirement:
        return self.water_requirements[plant][season]


# ---------------------------------------------------------------------------
# .env loading (~10 lines of parsing on purpose — no python-dotenv, plan D16).
# ---------------------------------------------------------------------------
def load_dotenv(path: Path) -> dict[str, str]:
    """Parse `KEY=VALUE` lines into os.environ (existing env wins).

    Returns the parsed pairs so tests can assert without touching os.environ.
    """
    parsed: dict[str, str] = {}
    if not path.exists():
        return parsed
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        parsed[key.strip()] = value.strip().strip("'\"")
    for key, value in parsed.items():
        os.environ.setdefault(key, value)
    return parsed


# ---------------------------------------------------------------------------
# Redaction — API keys live in URLs, and requests exceptions include the URL.
# Every error must pass through redact() before logging or messaging (CLAUDE.md).
# ---------------------------------------------------------------------------
_SECRET_PARAM_RE = re.compile(r"(?i)\b(key|apikey|api_key|appid|token)=[^&\s\"']+")
_TELEGRAM_TOKEN_RE = re.compile(r"/bot[^/\s]+/")


def redact(text: object) -> str:
    """Return `text` with API keys/tokens replaced by `***`.

    Covers both patterns (`key=...`, `appid=...`, Telegram `/bot<token>/`) and
    the literal secret values from the environment, in case a secret appears
    somewhere we did not predict (e.g. a requests exception message).
    """
    out = _SECRET_PARAM_RE.sub(lambda m: f"{m.group(1)}=***", str(text))
    out = _TELEGRAM_TOKEN_RE.sub("/***/", out)
    for key in ENV_KEYS:
        secret = os.environ.get(key, "")
        if secret:
            out = out.replace(secret, "***")
    return out


def secrets_present() -> list[str]:
    """Names of env secrets that are set (used by logging, never their values)."""
    return [key for key in ENV_KEYS if os.environ.get(key)]


# ---------------------------------------------------------------------------
# Loading + validation (plan §3). All problems are collected and reported
# together in one ConfigError so a broken config is fixed in a single pass.
# ---------------------------------------------------------------------------
def load_config(path: Path) -> AppConfig:
    """Read, validate and convert `config.yaml` into an AppConfig."""
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ConfigError(f"config file not found: {path}") from None
    except yaml.YAMLError as exc:
        raise ConfigError(f"config file is not valid YAML: {exc}") from None
    if not isinstance(raw, dict):
        raise ConfigError("config file must be a YAML mapping at the top level")

    errors: list[str] = []

    location = _parse_location(raw.get("location"), errors)
    month_to_season = _parse_season_calendar(raw.get("season_calendar"), errors)
    plants, requirements = _parse_plants(
        raw.get("plants"), raw.get("water_requirements"), errors
    )
    thresholds = _parse_watering(raw.get("watering"), errors)

    if errors:
        raise ConfigError("\n".join(f"- {e}" for e in errors))
    return AppConfig(location, month_to_season, plants, requirements, thresholds)


def _number(
    value: object,
    label: str,
    errors: list[str],
    minimum: float | None = None,
    maximum: float | None = None,
) -> float | None:
    """Check `value` is a real number in range; record an error and return None if not."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        errors.append(f"{label}: must be a number, got {value!r}")
        return None
    number = float(value)
    if minimum is not None and number < minimum:
        errors.append(f"{label}: must be >= {minimum}, got {number}")
        return None
    if maximum is not None and number > maximum:
        errors.append(f"{label}: must be <= {maximum}, got {number}")
        return None
    return number


def _integer(
    value: object,
    label: str,
    errors: list[str],
    minimum: int | None = None,
    maximum: int | None = None,
) -> int | None:
    number = _number(value, label, errors, minimum, maximum)
    if number is None:
        return None
    if not number.is_integer():
        errors.append(f"{label}: must be a whole number, got {number}")
        return None
    return int(number)


def _val(value: T | None, default: T) -> T:
    """Keep a valid value (including 0.0!) and substitute a placeholder on error.

    Deliberately not `x or default`: a configured 0 must survive validation.
    Placeholders never leak — any recorded error aborts the load.
    """
    return default if value is None else value


def _parse_location(raw: object, errors: list[str]) -> LocationConfig:
    if not isinstance(raw, dict):
        errors.append("location: section is required")
        return LocationConfig(0.0, 0.0, "UTC")
    latitude = _val(_number(raw.get("latitude"), "location.latitude", errors, -90, 90), 0.0)
    longitude = _val(_number(raw.get("longitude"), "location.longitude", errors, -180, 180), 0.0)
    timezone = raw.get("timezone")
    if not isinstance(timezone, str) or not timezone:
        errors.append("location.timezone: must be a non-empty IANA name (e.g. Europe/Berlin)")
        timezone = "UTC"
    else:
        try:
            ZoneInfo(timezone)
        except (ZoneInfoNotFoundError, ValueError):
            errors.append(f"location.timezone: {timezone!r} is unknown to zoneinfo")
            timezone = "UTC"
    return LocationConfig(latitude, longitude, timezone)


def _parse_season_calendar(raw: object, errors: list[str]) -> dict[int, str]:
    """Require exactly the four seasons, each month 1..12 in exactly one season."""
    fallback = {month: "spring" for month in range(1, 13)}
    if not isinstance(raw, dict):
        errors.append("season_calendar: section is required with four seasons")
        return fallback
    missing = [season for season in SEASONS if season not in raw]
    extra = [key for key in raw if key not in SEASONS]
    if missing:
        errors.append(f"season_calendar: missing season(s): {', '.join(missing)}")
    if extra:
        errors.append(f"season_calendar: unknown season(s): {', '.join(map(str, extra))}")

    month_to_season: dict[int, str] = {}
    for season in SEASONS:
        if season not in raw:
            continue  # already reported by the `missing` check above
        months = raw.get(season)
        if not isinstance(months, list) or not months:
            errors.append(f"season_calendar.{season}: must be a non-empty list of months 1-12")
            continue
        for month in months:
            if isinstance(month, bool) or not isinstance(month, int) or not 1 <= month <= 12:
                errors.append(f"season_calendar.{season}: invalid month {month!r}")
                continue
            if month in month_to_season:
                errors.append(
                    f"season_calendar: month {month} appears in both "
                    f"{month_to_season[month]!r} and {season!r}"
                )
                continue
            month_to_season[month] = season
    for month in range(1, 13):
        if month not in month_to_season and not missing:
            errors.append(f"season_calendar: month {month} is not in any season")
    return month_to_season or fallback


def _parse_plants(
    raw_plants: object, raw_requirements: object, errors: list[str]
) -> tuple[tuple[PlantConfig, ...], dict[str, dict[str, SeasonRequirement]]]:
    plants: list[PlantConfig] = []
    if not isinstance(raw_plants, list) or not raw_plants:
        errors.append("plants: must be a non-empty list")
    else:
        seen: set[str] = set()
        for i, entry in enumerate(raw_plants):
            label = f"plants[{i}]"
            if not isinstance(entry, dict):
                errors.append(f"{label}: must be a mapping")
                continue
            name = entry.get("name")
            kind = entry.get("type")
            if not isinstance(name, str) or not name:
                errors.append(f"{label}.name: required non-empty string")
                continue
            label = f"plants[{name}]"
            if name in seen:
                errors.append(f"{label}: duplicate plant name")
            seen.add(name)
            if not isinstance(kind, str) or not kind:
                errors.append(f"{label}.type: required non-empty string")
                kind = ""
            plants.append(PlantConfig(name, kind))

    requirements: dict[str, dict[str, SeasonRequirement]] = {}
    if not isinstance(raw_requirements, dict):
        errors.append("water_requirements: section is required (mapping plant -> season)")
    else:
        for plant_name, seasons in raw_requirements.items():
            label = f"water_requirements.{plant_name}"
            if not isinstance(seasons, dict):
                errors.append(f"{label}: must be a mapping of the four seasons")
                continue
            missing = [season for season in SEASONS if season not in seasons]
            if missing:
                errors.append(f"{label}: missing season(s): {', '.join(missing)}")
            plant_seasons: dict[str, SeasonRequirement] = {}
            for season, req in seasons.items():
                if season not in SEASONS:
                    errors.append(f"{label}.{season}: unknown season")
                    continue
                req_label = f"{label}.{season}"
                if not isinstance(req, dict):
                    errors.append(f"{req_label}: must be a mapping")
                    continue
                need = req.get("water_need")
                if need not in WATER_NEEDS:
                    errors.append(
                        f"{req_label}.water_need: must be one of {WATER_NEEDS}, got {need!r}"
                    )
                    need = "medium"
                target = _number(
                    req.get("rain_target_mm_7d"), f"{req_label}.rain_target_mm_7d",
                    errors, minimum=0.0,
                )
                if target is None:
                    target = 0.0
                plant_seasons[season] = SeasonRequirement(need, target)
            requirements[plant_name] = plant_seasons

    # Plants and requirements must describe the same set (plan §3).
    plant_names = {plant.name for plant in plants}
    for name in sorted(plant_names - requirements.keys()):
        errors.append(f"water_requirements: missing entry for plant {name!r}")
    for name in sorted(requirements.keys() - plant_names):
        errors.append(f"water_requirements: entry {name!r} has no matching plant")
    return tuple(plants), requirements


def _parse_watering(raw: object, errors: list[str]) -> Thresholds:
    """Deep-merge the optional `watering:` section over DEFAULT_WATERING."""
    merged: dict = _deep_copy(DEFAULT_WATERING)
    if raw is not None:
        if not isinstance(raw, dict):
            errors.append("watering: must be a mapping (or omitted for defaults)")
        else:
            _deep_merge(merged, raw, "watering", errors)

    recent_days = _val(
        _integer(merged.get("recent_days"), "watering.recent_days", errors, 1, 92), 7
    )
    meaningful = _val(
        _number(merged.get("meaningful_rain_mm"), "watering.meaningful_rain_mm",
                errors, minimum=0.0),
        0.0,
    )
    min_history = _val(
        _integer(merged.get("min_history_days"), "watering.min_history_days", errors, 1, recent_days),
        5,
    )

    heat_raw = merged.get("heat_adjustment", {})
    enabled = heat_raw.get("enabled")
    if not isinstance(enabled, bool):
        errors.append(f"watering.heat_adjustment.enabled: must be true/false, got {enabled!r}")
        enabled = True
    heat = HeatAdjustment(
        enabled=enabled,
        et0_baseline_mm_day=_val(_number(
            heat_raw.get("et0_baseline_mm_day"),
            "watering.heat_adjustment.et0_baseline_mm_day", errors, minimum=0.001,
        ), 3.0),
        temp_threshold_c=_val(_number(
            heat_raw.get("temp_threshold_c"), "watering.heat_adjustment.temp_threshold_c", errors
        ), 28.0),
        temp_step_per_c=_val(_number(
            heat_raw.get("temp_step_per_c"), "watering.heat_adjustment.temp_step_per_c",
            errors, minimum=0.0,
        ), 0.03),
        max_factor=_val(_number(
            heat_raw.get("max_factor"), "watering.heat_adjustment.max_factor",
            errors, minimum=1.0,
        ), 1.5),
    )

    profiles_raw = merged.get("need_profiles", {})
    profiles: dict[str, NeedProfile] = {}
    for need in WATER_NEEDS:
        entry = profiles_raw.get(need)
        if not isinstance(entry, dict):
            errors.append(f"watering.need_profiles.{need}: section is required")
            profiles[need] = NeedProfile(0.5, 8.0)
            continue
        fraction = _number(
            entry.get("min_rain_fraction"), f"watering.need_profiles.{need}.min_rain_fraction",
            errors, 0.0, 1.0,
        )
        wait = _number(
            entry.get("skip_if_tomorrow_rain_mm"),
            f"watering.need_profiles.{need}.skip_if_tomorrow_rain_mm", errors, minimum=0.0,
        )
        profiles[need] = NeedProfile(fraction if fraction is not None else 0.5,
                                     wait if wait is not None else 8.0)

    severe_raw = merged.get("severe", {})
    severe = SevereProfile(
        rain_fraction=_val(_number(
            severe_raw.get("rain_fraction"), "watering.severe.rain_fraction", errors, 0.0, 1.0,
        ), 0.15),
        min_reminders=_val(_integer(
            severe_raw.get("min_reminders"), "watering.severe.min_reminders", errors, minimum=1,
        ), 2),
        wait_multiplier=_val(_number(
            severe_raw.get("wait_multiplier"), "watering.severe.wait_multiplier",
            errors, minimum=1.0,
        ), 2.0),
    )
    return Thresholds(recent_days, meaningful, min_history, heat, profiles, severe)


def _deep_copy(value: object) -> dict:
    if not isinstance(value, dict):
        return {}
    return {key: _deep_copy(item) if isinstance(item, dict) else item
            for key, item in value.items()}


def _deep_merge(base: dict, override: dict, prefix: str, errors: list[str]) -> None:
    """Merge `override` into `base`, rejecting unknown keys so typos are caught."""
    for key, value in override.items():
        if key not in base:
            errors.append(f"{prefix}: unknown setting {key!r}")
            continue
        if isinstance(base[key], dict):
            if not isinstance(value, dict):
                errors.append(f"{prefix}.{key}: must be a mapping")
                continue
            _deep_merge(base[key], value, f"{prefix}.{key}", errors)
        else:
            base[key] = value
