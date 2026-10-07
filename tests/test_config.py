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


def expect_errors(tmp_path: Path, config: dict) -> str:
    """Load a broken config and return the collected error text."""
    try:
        load_config(write_config(tmp_path, config))
    except ConfigError as exc:
        return str(exc)
    raise AssertionError("expected ConfigError, but config loaded cleanly")


def test_missing_season_rejected(tmp_path: Path) -> None:  # test matrix #11
    config = valid_config()
    del config["season_calendar"]["summer"]
    message = expect_errors(tmp_path, config)
    assert "missing season(s): summer" in message


def test_month_in_two_seasons_rejected(tmp_path: Path) -> None:
    config = valid_config()
    config["season_calendar"]["spring"].append(7)  # 7 already in summer
    message = expect_errors(tmp_path, config)
    assert "month 7 appears in both" in message


def test_month_in_no_season_rejected(tmp_path: Path) -> None:
    config = valid_config()
    config["season_calendar"]["winter"].remove(1)
    message = expect_errors(tmp_path, config)
    assert "month 1 is not in any season" in message


def test_unknown_season_name_rejected(tmp_path: Path) -> None:
    config = valid_config()
    config["season_calendar"]["monsoon"] = [6]
    message = expect_errors(tmp_path, config)
    assert "unknown season(s): monsoon" in message


def test_bad_latitude_rejected(tmp_path: Path) -> None:
    config = valid_config()
    config["location"]["latitude"] = 91
    assert "location.latitude" in expect_errors(tmp_path, config)


def test_bad_longitude_rejected(tmp_path: Path) -> None:
    config = valid_config()
    config["location"]["longitude"] = -200
    assert "location.longitude" in expect_errors(tmp_path, config)


def test_bad_timezone_rejected(tmp_path: Path) -> None:
    config = valid_config()
    config["location"]["timezone"] = "Mars/Olympus_Mons"
    assert "location.timezone" in expect_errors(tmp_path, config)


def test_bad_water_need_rejected(tmp_path: Path) -> None:
    config = valid_config()
    config["water_requirements"]["peach"]["summer"]["water_need"] = "desperate"
    assert "water_need" in expect_errors(tmp_path, config)


def test_negative_target_rejected(tmp_path: Path) -> None:
    config = valid_config()
    config["water_requirements"]["peach"]["summer"]["rain_target_mm_7d"] = -1
    assert "rain_target_mm_7d" in expect_errors(tmp_path, config)


def test_plant_without_requirements_rejected(tmp_path: Path) -> None:
    config = valid_config()
    del config["water_requirements"]["tomato"]
    message = expect_errors(tmp_path, config)
    assert "missing entry for plant 'tomato'" in message


def test_requirement_without_plant_rejected(tmp_path: Path) -> None:
    config = valid_config()
    config["water_requirements"]["strawberry"] = config["water_requirements"]["peach"]
    message = expect_errors(tmp_path, config)
    assert "no matching plant" in message


def test_fraction_outside_unit_interval_rejected(tmp_path: Path) -> None:
    config = valid_config()
    config["watering"] = {"need_profiles": {"low": {"min_rain_fraction": 1.5}}}
    message = expect_errors(tmp_path, config)
    assert "min_rain_fraction" in message and "<=" in message


def test_unknown_watering_setting_rejected(tmp_path: Path) -> None:
    """A typo must not silently fall back to the default."""
    config = valid_config()
    config["watering"] = {"min_rain_fracton": 0.4}
    message = expect_errors(tmp_path, config)
    assert "unknown setting 'min_rain_fracton'" in message


def test_duplicate_plant_name_rejected(tmp_path: Path) -> None:
    config = valid_config()
    config["plants"].append({"name": "peach", "type": "fruit-tree", "count": 1})
    assert "duplicate plant name" in expect_errors(tmp_path, config)


def test_non_positive_count_rejected(tmp_path: Path) -> None:
    config = valid_config()
    config["plants"][0]["count"] = 0
    assert "count" in expect_errors(tmp_path, config)


def test_all_errors_reported_together(tmp_path: Path) -> None:
    config = valid_config()
    config["location"]["latitude"] = 999
    config["plants"][0]["count"] = -5
    config["water_requirements"]["peach"]["summer"]["water_need"] = "thirsty"
    message = expect_errors(tmp_path, config)
    assert "location.latitude" in message
    assert "count" in message
    assert "water_need" in message


def test_missing_config_file_rejected(tmp_path: Path) -> None:
    try:
        load_config(tmp_path / "nope.yaml")
    except ConfigError as exc:
        assert "not found" in str(exc)
    else:
        raise AssertionError("expected ConfigError for missing file")


# --- redact() ---------------------------------------------------------------

def test_redact_strips_query_param_keys() -> None:
    from gardenwater.config import redact

    text = redact("https://api.weatherapi.com/v1/forecast.json?key=ABC123&q=52.5")
    assert "ABC123" not in text
    assert "key=***" in text


def test_redact_strips_appid() -> None:
    from gardenwater.config import redact

    text = redact("https://api.openweathermap.org/data/4.0/x?appid=deadbeef&units=metric")
    assert "deadbeef" not in text
    assert "appid=***" in text


def test_redact_strips_telegram_token_path() -> None:
    from gardenwater.config import redact

    text = redact("POST https://api.telegram.org/bot98765:QQ-WW/sendMessage failed")
    assert "98765:QQ-WW" not in text


def test_redact_strips_literal_env_secret(monkeypatch) -> None:
    from gardenwater.config import redact

    monkeypatch.setenv("WEATHERAPI_KEY", "super-secret-key-42")
    assert "super-secret-key-42" not in redact("value=super-secret-key-42")


def test_redact_handles_non_string_input() -> None:
    from gardenwater.config import redact

    assert redact(42) == "42"
