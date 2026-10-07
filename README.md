# garden-water

A personal garden-watering reminder. Once an evening it checks the weather for
one fixed location, estimates whether each plant needs watering, and sends
**one combined Telegram message only when action is needed**. State lives in
SQLite. Then it exits — no daemon, no server, no scheduler loop.

Architecture, exactly: **Python script + YAML config + env vars + SQLite + cron +
Telegram**. Runtime dependencies: `requests`, `PyYAML`. Dev: `pytest`.

## Quick start

```bash
python -m venv .venv && .venv/bin/pip install -r requirements.txt
cp config.example.yaml config.yaml     # then edit coordinates and plants
cp .env.example .env                   # fill in Telegram (and optional weather) keys

.venv/bin/python -m pytest -q                      # 115 tests, no network
.venv/bin/python garden_water.py --dry-run         # live weather, prints, sends nothing
.venv/bin/python garden_water.py                   # normal run (what cron runs)
```

Flags: `--config PATH` · `--db PATH` · `--dry-run` · `--today YYYY-MM-DD`
(testing only) · `-v`.

Exit codes: `0` ok · `1` all weather providers failed / insufficient data ·
`2` config error · `3` Telegram send failed.

`--dry-run` fetches live weather and caches it like a normal run, but writes no
decisions, never touches reminder state and sends nothing — it only prints the
inputs and the reason for every plant.

## How the decision is made

For each plant, on each run (plan §2, `gardenwater/watering.py`):

1. **Season** — `month_to_season` from config maps the current month to a
   season; the season picks the plant's `water_need` and `rain_target_mm_7d`.
2. **Rain over the last 7 days** — today included, from SQLite. Missing days
   are *not* treated as dry: the total is scaled (`sum × 7 / days_found`), and
   with fewer than `min_history_days` rows the run aborts (exit 1) instead of
   guessing.
3. **Heat raises demand** — factor = `1 + (mean daily max − 28 °C) × 0.03`,
   clamped to `[1.0, 1.5]`; with ET₀ data, `mean ET₀ / 3.0 mm` is used instead.
   Cool weather never *lowers* demand.
4. **Effective target** = `rain_target_mm_7d × heat factor`.
5. **Fraction** = rain received ÷ effective target (0 target ⇒ nothing needed).
6. **Wet enough?** — fraction ≥ the profile's `min_rain_fraction` ⇒
   `NO_ACTION` (any open reminder is cleared).
7. **Wait?** — tomorrow's forecast ≥ `skip_if_tomorrow_rain_mm` ⇒ `POSTPONE`
   (log/DB only, no message). For *severe* plants (≤ 15% of target **and**
   already reminded `min_reminders` times) the wait limit is doubled.
8. **Water** — otherwise `WATER` (first time) or `FOLLOW_UP` (an open reminder
   and less than `meaningful_rain_mm` of rain since it). Both produce the
   Telegram message; `NO_ACTION` and `POSTPONE` never do.

Every decision carries a plain-sentence `reason` (shown by `--dry-run`, logged,
and stored in SQLite).

### Example

Peach in summer: target 40 mm over 7 days, `water_need: high` (water below 65%
of target, wait for 12 mm tomorrow). It got 5 mm → 12.5% of target, tomorrow
brings 0 mm → **WATER**.

## Key config values

### `rain_target_mm_7d`

The approximate amount of natural rain this plant would ideally get over a
rolling 7 days **in that season** — a gardener's number, *not* a scientific
irrigation requirement. It is the denominator of "how much of what the plant
wants actually arrived". Raise it in a dry climate or for a thirstier plant;
lower it for rain-loving plants.

### `water_need` — low / medium / high

Chooses a **need profile** (thresholds table below): how dry is dry enough, and
how much forecast rain makes us wait one more day.

### Thresholds (`watering:` section)

All numbers live in config (`DEFAULT_WATERING` in `gardenwater/config.py`,
mirrored in `config.example.yaml`):

| Key | Default | Meaning |
|---|---|---|
| `recent_days` | 7 | window size; today is included |
| `meaningful_rain_mm` | 5 | rain ≥ this since the reminder ends its streak |
| `min_history_days` | 5 | fewer stored days in the window ⇒ abort (exit 1) |
| `heat_adjustment.enabled` | true | hot weather raises demand |
| `heat_adjustment.temp_threshold_c` | 28 | mean daily max above this = extra demand |
| `heat_adjustment.temp_step_per_c` | 0.03 | +3 % demand per °C above the threshold |
| `heat_adjustment.max_factor` | 1.5 | demand is never raised by more than 50 % |
| `heat_adjustment.et0_baseline_mm_day` | 3.0 | ET₀ at/below this = normal demand |
| `need_profiles.low.min_rain_fraction` | 0.30 | water when it got **less than 30 %** of target |
| `need_profiles.medium.min_rain_fraction` | 0.50 | less than 50 % |
| `need_profiles.high.min_rain_fraction` | 0.65 | less than 65 % |
| `need_profiles.low/medium/high.skip_if_tomorrow_rain_mm` | 5 / 8 / 12 | wait if tomorrow brings at least this |
| `severe.rain_fraction` | 0.15 | "bad shape" = got ≤ 15 % of target |
| `severe.min_reminders` | 2 | …after at least this many reminders |
| `severe.wait_multiplier` | 2.0 | …then tomorrow's rain must be 2× larger |

### Tuning cheat-sheet

- Plant waters too often → **lower** its `min_rain_fraction` (it must look
  drier before we act) or **lower** its `rain_target_mm_7d` (it wants less
  rain, so the same rainfall counts for more).
- Plant not watered when it looks dry → **raise** `min_rain_fraction` or
  **raise** `rain_target_mm_7d`.
- Messages arrive when rain is forecast anyway → raise
  `skip_if_tomorrow_rain_mm`.
- Reminders repeat too aggressively → raise `skip_if_tomorrow_rain_mm`, raise
  `severe.wait_multiplier`, or lower `severe.min_reminders` — all three make
  it easier to wait for forecast rain instead of messaging.
- Too little history kept → lower `min_history_days` (accepts thinner data) or
  let it run a few days to fill `weather_daily`.

## Where do I change X?

| I want to change… | Edit |
|---|---|
| thresholds (fractions, mm, heat) | `watering:` in `config.yaml` |
| which months are which season | `season_calendar` |
| add/remove a plant | `plants` + `water_requirements` |
| message wording | `gardenwater/messages.py` |
| decision logic | `gardenwater/watering.py` (keep it pure!) |
| provider order / adding a provider | `default_providers()` in `gardenwater/app.py` |
| database schema | `gardenwater/database.py` (`SCHEMA`) |
| cron schedule | your crontab (see below) |

## Weather providers

Fallback chain (first success wins):

```
Open-Meteo (keyless)  ──fails──►  WeatherAPI (WEATHERAPI_KEY)  ──fails──►  exit 1
```

- Unconfigured providers are **skipped**, not counted as failures.
- Normalized daily weather is stored in SQLite (`weather_daily`); raw API
  responses are never persisted.
- Days already stored are not re-fetched where the API charges per date
  (WeatherAPI history, one request per missing day).
- A provider going `ok → failed` triggers **one** warning message per outage;
  while it keeps failing, no repeats; recovery resets it. If *all* providers
  fail you get the all-failed message **every** run (the system is blind) and
  exit code 1.
- OpenWeather is disabled for now (needs a separate One Call subscription —
  see `docs/api-notes.md`, decision D21).

### Adding a provider

1. Create `gardenwater/weather/<name>.py` with a class implementing the
   `WeatherProvider` protocol (`weather/base.py`): `name`, `is_configured()`,
   `fetch(lat, lon, tz, today, past_days, missing_dates) -> WeatherData`.
2. Raise `ProviderError` (short, **redacted** message) on any problem.
3. Register it in `default_providers()` in `gardenwater/app.py`.
4. Record API facts in `docs/api-notes.md`, capture a fixture under
   `tests/fixtures/`, and add a parser test (see `tests/test_open_meteo.py`).

## Telegram setup

1. Talk to **@BotFather** → `/newbot` → copy the token.
2. Get your chat id (e.g. from **@userinfobot**), message your bot once.
3. Put both in `.env` (never commit it):

```
TELEGRAM_BOT_TOKEN=123456:ABC...
TELEGRAM_CHAT_ID=424242
```

Messages are plain text (no `parse_mode`), and every error that might contain
a key is passed through `redact()` before logging or sending.

## Cron

```cron
0 19 * * * /path/to/garden-water/.venv/bin/python /path/to/garden-water/garden_water.py >> /path/to/garden-water/garden-water.log 2>&1
```

- Cron triggers at **19:00 in the server's timezone**; "today"/"tomorrow" are
  computed in the **garden** timezone from `config.yaml` (`location.timezone`).
- `.env`, `config.yaml` and `garden.db` default to the directory of
  `garden_water.py`, so the working directory cron uses does not matter.
- A copyable line lives in `cron.example`.

## Troubleshooting

| Symptom | What to check |
|---|---|
| exit 2 | the printed config errors — config.yaml lists *all* problems at once |
| exit 1, "all weather providers failed" | network; `WEATHERAPI_KEY`; provider logs (`-v`) |
| exit 1, "not enough recent weather data" | the window needs ≥ `min_history_days` stored days; run daily for a few days |
| exit 3, Telegram failed | `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID`, bot not blocked, chat started |
| no message although plants look dry | that is normal: `NO_ACTION`/`POSTPONE` never message; `--dry-run` shows the reason |
| wrong "today" | `location.timezone` must be the **garden's** IANA timezone |
| secrets in logs | impossible by design — everything goes through `redact()`; but never paste raw exception URLs from third-party tools |

## Tests

```bash
.venv/bin/python -m pytest -q
```

No test ever calls a live API: providers are exercised through recorded JSON
fixtures and a fake HTTP session; the algorithm tests are pure dataclasses;
database tests use temp files. `config.example.yaml`'s `watering:` values are
asserted to equal the built-in defaults.
