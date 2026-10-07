# docs/api-notes.md — verified weather API notes

Every entry was verified against the **current official docs** (linked) plus a
**live request** captured on 2026-10-07. Anything the plan assumed that the doc
contradicts is flagged with **PLAN CHANGE** and recorded in
`DEVELOPMENT_PLAN.md` §1.

Providers must return, for one fixed location:

- past days (for the 7-day rain window),
- **today's daily total** (rain so far + rest-of-day forecast in one number),
- **tomorrow's daily forecast**,
- daily mean temp, daily max temp, ET₀ if available.

---

## Summary

| Provider | History | Today total | Tomorrow | Temp mean/max | ET₀ | Key needed | Fixture |
|----------|---------|-------------|----------|---------------|-----|-----------|---------|
| Open-Meteo | yes (`past_days` 0–92) | yes (daily agg.) | yes | yes / yes | yes | no | `tests/fixtures/open_meteo_forecast.json` |
| WeatherAPI.com | yes (history, free-plan range TBD below) | yes | yes | yes / yes | no | `WEATHERAPI_KEY` | `tests/fixtures/weatherapi_*.json` |
| OpenWeather | see One Call section | see One Call section | see One Call section | yes / yes | see section | `OPENWEATHER_API_KEY` | `tests/fixtures/openweather_*.json` |

---

## Open-Meteo — Weather Forecast API ✅ verified

- **Docs:** https://open-meteo.com/en/docs
- **Endpoint:** `GET https://api.open-meteo.com/v1/forecast`
- **Auth:** none for non-commercial use. Docs: `apikey` is "only required to
  commercial use to access reserved API resources" (customer- prefix, see
  pricing page). We use no key.
- **Units:** defaults are what we want — precipitation **mm**, temperature **°C**
  (`precipitation_unit=mm` is the default, `temperature_unit=celsius` default).

### Request parameters we use

| Param | Value | Why |
|-------|-------|-----|
| `latitude`, `longitude` | from config | required, WGS84 |
| `daily` | `precipitation_sum,temperature_2m_mean,temperature_2m_max,et0_fao_evapotranspiration` | the four fields we persist |
| `timezone` | garden timezone from config (e.g. `Europe/Berlin`) | **required when `daily=` is set**; makes `daily.time` dates local to the garden, so "today" matches our timezone. `auto` also works but explicit TZ is clearer. |
| `past_days` | `10` (plan: ~10 past days + today) | 0–92 allowed; archived forecast days for the rain window / self-healing (D5) |
| `forecast_days` | `2` | today + tomorrow is all we need (max 16) |

### Fields used (Daily Parameter Definition)

| Field | Unit | Notes |
|-------|------|-------|
| `time` | `YYYY-MM-DD` | local date in requested timezone |
| `precipitation_sum` | mm | "Sum of daily precipitation (including rain, showers and snowfall)" — today's value is the daily aggregation of hourly values, i.e. **observed so far + remaining forecast** in one number (plan D3 ✅) |
| `temperature_2m_mean` | °C | mean temp |
| `temperature_2m_max` | °C | max temp |
| `et0_fao_evapotranspiration` | mm/day | "Daily sum of ET₀ Reference Evapotranspiration of a well watered grass field" — exactly our heat input |

Aggregation is "a simple 24 hour aggregation from hourly values". Values can be
`null` when data is missing — treat `null` as missing, never as 0.

### Response shape

```json
{
  "latitude": 52.5126, "longitude": 13.4195, "elevation": 38.0,
  "utc_offset_seconds": 7200, "timezone": "Europe/Berlin",
  "daily_units": {"time": "iso8601", "precipitation_sum": "mm", "...": "..."},
  "daily": {
    "time": ["2026-09-27", "...", "2026-10-08"],
    "precipitation_sum": [0.0, 0.0, 3.6],
    "temperature_2m_mean": [17.6, 15.7],
    "temperature_2m_max": [23.8, 20.1],
    "et0_fao_evapotranspiration": [1.2, 0.9]
  }
}
```

Live sample captured 2026-10-07 → `tests/fixtures/open_meteo_forecast.json`
(12 days: 10 past + today `2026-10-07` + tomorrow `2026-10-08`, no nulls).

### Errors

- Malformed parameter → **HTTP 400** with
  `{"error": true, "reason": "Cannot initialize WeatherVariable ..."}` (verified live).
- Network/timeout → `requests` exception (**contains the URL — redact before logging**).
- Free-tier limits: not published as a hard number in the docs page; non-commercial
  use is free, commercial needs a key. Handle 429 defensively like any provider.

---

## WeatherAPI.com — pending verification (Phase 0 task 5)

## OpenWeather — pending verification (Phase 0 task 6)
