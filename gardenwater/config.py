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
    count: float


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
