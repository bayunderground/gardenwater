"""Tests for config loading, validation and redaction (plan Phase 1, §3)."""

from __future__ import annotations

from pathlib import Path

import yaml

from gardenwater.config import (
    DEFAULT_WATERING,
    ConfigError,
    load_config,
)

REPO_ROOT = Path(__file__).resolve().parent.parent


def valid_config() -> dict:
    """A minimal config that must always load cleanly."""
    return {
        "location": {"latitude": 52.52, "longitude": 13.41, "timezone": "Europe/Berlin"},
        "season_calendar": {
            "spring": [3, 4, 5],
            "summer": [6, 7, 8],
            "autumn": [9, 10, 11],
            "winter": [12, 1, 2],
        },
        "plants": [
            {"name": "peach", "type": "fruit-tree", "count": 1},
            {"name": "tomato", "type": "vegetable", "count": 4},
        ],
        "water_requirements": {
            "peach": {
                "spring": {"water_need": "medium", "rain_target_mm_7d": 30},
                "summer": {"water_need": "high", "rain_target_mm_7d": 40},
                "autumn": {"water_need": "low", "rain_target_mm_7d": 20},
                "winter": {"water_need": "low", "rain_target_mm_7d": 10},
            },
            "tomato": {
                "spring": {"water_need": "medium", "rain_target_mm_7d": 25},
                "summer": {"water_need": "high", "rain_target_mm_7d": 35},
                "autumn": {"water_need": "low", "rain_target_mm_7d": 15},
                "winter": {"water_need": "low", "rain_target_mm_7d": 5},
            },
        },
    }


def write_config(tmp_path: Path, config: dict) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(config), encoding="utf-8")
    return path


def test_valid_load(tmp_path: Path) -> None:
    cfg = load_config(write_config(tmp_path, valid_config()))
    assert cfg.location.latitude == 52.52
    assert cfg.location.longitude == 13.41
    assert cfg.location.timezone == "Europe/Berlin"
    assert [plant.name for plant in cfg.plants] == ["peach", "tomato"]
    assert cfg.plants[1].count == 4
    assert cfg.requirement("peach", "summer").water_need == "high"
    assert cfg.requirement("tomato", "spring").rain_target_mm_7d == 25


def test_four_seasons_load(tmp_path: Path) -> None:  # test matrix #10
    cfg = load_config(write_config(tmp_path, valid_config()))
    assert sorted(set(cfg.month_to_season.values())) == [
        "autumn", "spring", "summer", "winter",
    ]


def test_month_to_season_mapping(tmp_path: Path) -> None:  # test matrix #9
    cfg = load_config(write_config(tmp_path, valid_config()))
    assert cfg.season_for_month(1) == "winter"
    assert cfg.season_for_month(3) == "spring"
    assert cfg.season_for_month(7) == "summer"
    assert cfg.season_for_month(10) == "autumn"
    assert cfg.season_for_month(12) == "winter"
    assert len(cfg.month_to_season) == 12


def test_example_yaml_matches_default_watering() -> None:
    """config.example.yaml must show exactly the built-in defaults (D20)."""
    example = yaml.safe_load((REPO_ROOT / "config.example.yaml").read_text(encoding="utf-8"))
    assert example["watering"] == DEFAULT_WATERING


def test_example_yaml_loads() -> None:
    cfg = load_config(REPO_ROOT / "config.example.yaml")
    assert cfg.thresholds.recent_days == DEFAULT_WATERING["recent_days"]
    assert cfg.thresholds.profile("high").min_rain_fraction == 0.65


def test_watering_section_omitted_uses_defaults(tmp_path: Path) -> None:
    cfg = load_config(write_config(tmp_path, valid_config()))
    assert cfg.thresholds.recent_days == 7
    assert cfg.thresholds.meaningful_rain_mm == 5
    assert cfg.thresholds.min_history_days == 5
    assert cfg.thresholds.heat.enabled is True
    assert cfg.thresholds.heat.max_factor == 1.5
    assert cfg.thresholds.severe.wait_multiplier == 2.0
    assert cfg.thresholds.profile("low").min_rain_fraction == 0.30


def test_partial_watering_override_keeps_other_defaults(tmp_path: Path) -> None:
    config = valid_config()
    config["watering"] = {"need_profiles": {"high": {"min_rain_fraction": 0.4}}}
    cfg = load_config(write_config(tmp_path, config))
    assert cfg.thresholds.profile("high").min_rain_fraction == 0.4
    assert cfg.thresholds.profile("high").skip_if_tomorrow_rain_mm == 12  # untouched
    assert cfg.thresholds.profile("medium").min_rain_fraction == 0.50      # untouched
    assert cfg.thresholds.recent_days == 7                                 # untouched
