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

## WeatherAPI.com — ✅ docs verified (live capture needs `WEATHERAPI_KEY`)

- **Docs:** https://www.weatherapi.com/docs/ (Authentication, Request URL,
  Forecast API, History API, API Error Codes sections)
- **Endpoint:** `GET https://api.weatherapi.com/v1/<method>.json`
- **Auth:** API key as query param `key=<KEY>` (⚠️ key appears in the URL —
  `requests` exceptions include it, so **always `redact()`**).
- **Units:** response is metric when requested with metric fields —
  `totalprecip_mm` (mm), `avgtemp_c` / `maxtemp_c` (°C).

### Request plan

| Data | Method | Params | Notes |
|------|--------|--------|-------|
| today + tomorrow (+ up to 14 days) | `/forecast.json` | `q=<lat>,<lon>`, `days=2`, `key=` | `forecast.forecastday[]`, index 0 = today, index 1 = tomorrow. Dates are **location-local** (`location.localtime`, `tz_id`). |
| past days (only dates missing from SQLite) | `/history.json` | `q=<lat>,<lon>`, `dt=YYYY-MM-DD`, `key=` | History from **2010-01-01**. `end_dt` (range fetch) is **Pro plan and above** ⇒ free plan is **one date per request**, so we fetch only missing dates (plan §Phase 4 ✅). |

### Fields used (`day` element, same shape in forecast and history)

| Field | Unit | Notes |
|-------|------|-------|
| `date` | `YYYY-MM-DD` | location-local date |
| `day.totalprecip_mm` | mm | daily total precipitation |
| `day.avgtemp_c` | °C | daily mean |
| `day.maxtemp_c` | °C | daily max |

`forecastday[].date` + `day.*` verified in docs' Forecast/History `day` tables.

### ET₀ — **not available on our plan**

Docs: `et0=yes` param returns Evapotranspiration "available for **Business and
Enterprise** clients only"; History also mentions ET₀ only for Enterprise.
⇒ provider returns `et0_mm=None`; the algorithm falls back to the temperature
heat factor (§2 step 3 handles this). **No plan change needed** (plan already
allows `et0` missing).

### Errors

JSON body with `error.code` + `error.message`, HTTP 4xx:

| HTTP | code | meaning |
|------|------|---------|
| 401 | 1002 | API key not provided |
| 401 | 2006 | API key provided is invalid |
| 403 | 2007 | monthly call quota exceeded |
| 403 | 2008 | API key disabled |
| 403 | 2009 | key has no access to resource (plan limit) |
| 400 | 1006 | no location matching `q` |

Free-tier limits: see pricing page (quota enforced via code 2007). Defensive
handling: any 4xx/5xx or malformed body ⇒ `ProviderError` with redacted message.

### Fixture (pending key)

Live capture → `tests/fixtures/weatherapi_forecast.json` and
`tests/fixtures/weatherapi_history.json` once `WEATHERAPI_KEY` is in `.env`.
Until then parser tests will use a hand-written fixture built exactly from the
docs' response shape, and be replaced by the recorded one when the key lands.

---

## OpenWeather — ✅ docs verified (live capture needs `OPENWEATHER_API_KEY`)

- **Docs:** https://openweathermap.org/api/one-call-4 (One Call API **4.0**,
  launched Jun 2026, "recommended for all new integrations"); migration notes:
  https://openweathermap.org/api/one-call-3-migration
- **PLAN CHANGE (recorded as D21 in `DEVELOPMENT_PLAN.md` §1):** the plan's
  unverified "One Call 4.0" **does exist** and is the current product, so we use
  it — not 3.0.
- **Endpoint:** `GET https://api.openweathermap.org/data/4.0/onecall/timeline/1day`
- **Auth:** `appid=<key>` query param (⚠️ key in URL → always `redact()`).
- **Subscription:** "One Call by Call" **only** (separate free subscription;
  docs: "1,000 calls/day for free"; account default cap is set to 2,000/day on
  subscribe; changeable in Personal account). No other plan needed.
- **Units:** pass `units=metric` ⇒ °C; precipitation fields in mm.

### Request plan

One endpoint serves history + today + tomorrow ("47 years of history and up to
1.5 years ahead") — simpler than 3.0's split history/forecast endpoints:

| Data | How |
|------|-----|
| past 10 days + today + tomorrow | `timeline/1day?lat=&lon=&units=metric&start=<unix of ~10 days ago>&appid=` |

**Pagination caveat:** the 1-day timeline returns **max 10 records** per
response; more pages via `next`/`prev` URLs (`start`, `cnt`). 12 days ⇒ 2
requests (each paginated request counts toward the quota). No `cnt` param is
documented on the main call table, but `next` URLs include `cnt=10` — follow the
provided `next` URL verbatim rather than building it.

### Fields used (daily record in `data[]`)

| Field | Unit | Notes |
|-------|------|-------|
| `data.dt` | unix UTC | convert to garden-local date using `timezone`/`timezone_offset` from the response |
| `data.temp.max` | °C | daily max → `temperature_max_c` |
| `data.temp.day` | °C | "Day temperature" — **not documented as a daily mean** ⇒ we store `temperature_avg_c=None` (heat factor uses tmax anyway, §2 step 3) |
| `data.rain` / `data.snow` | mm | docs list `data.rain.1h` even for the daily endpoint (likely copy-paste from hourly). **Must confirm the actual shape live** (number vs object) before finishing the parser. |
| ET₀ | — | **not provided by One Call 4.0** ⇒ `et0_mm=None`, heat falls back to temperature |

Today's record: docs don't state explicitly whether today's value mixes observed
+ forecast — **confirm live**; we accept whatever single daily total it gives
(plan D3).

### Errors

JSON `{"cod": <code>, "message": "...", "parameters": [...]}`:
400 bad params · 401 missing/no-access key · 404 data unavailable · 429 quota ·
5xx server. Treat 4xx/5xx/malformed as `ProviderError` (redacted; message may
echo `lat`/`lon`, never the key — but `requests` exceptions include the URL, so
redact anyway).

### Fixture (pending key)

Live capture → `tests/fixtures/openweather_timeline_1day.json` once
`OPENWEATHER_API_KEY` (One Call by Call subscribed) is in `.env`.
