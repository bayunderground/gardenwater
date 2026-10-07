# CLAUDE.md — garden-water

Personal garden-watering reminder. A **pet project**: keep it small, readable,
dependency-light, easy to modify. Read `DEVELOPMENT_PLAN.md` before writing code
and follow its phases in order.

## What it does

Runs **once per day from cron, in the evening** (suggested 19:00), exits. Fetches weather for one fixed location,
estimates whether each plant needs watering, and sends **one combined Telegram
message only when action is needed**. State lives in SQLite.

Architecture, exactly: `Python script + YAML config + env vars + SQLite + cron + Telegram`.
Nothing else.

## Hard constraints (do not violate)

- No daemon, web server, worker, scheduler loop. One run, then exit.
- No Docker, FastAPI, Celery, Redis, PostgreSQL, ORM, frontend, ML, or extra frameworks.
- Use stdlib `sqlite3`, `logging`, `dataclasses`, `zoneinfo`, `argparse`.
- Runtime dependencies: `requests`, `PyYAML` only. Dev: `pytest` only. Ask before adding anything.
- No IP geolocation. Location comes from config.
- No soil-moisture simulation. No acknowledgement system. The program has no sensor
  and cannot know whether watering happened.

## Wording rule (important)

Never claim the user did or did not water. Allowed: "Peach **still appears to need
watering**. No meaningful rain occurred since the previous reminder." Forbidden:
"You didn't water the peach." Applies to messages, logs, README, and code comments.

## Layout

```
garden_water.py          # thin entry point: calls gardenwater.app.main()
config.example.yaml      # committed; config.yaml is gitignored
.env.example
gardenwater/             # package (named without underscore to avoid clashing with garden_water.py)
    app.py               # orchestration: one run, exit code
    config.py            # load + validate YAML, load .env, dataclasses
    database.py          # sqlite3 schema + small query functions
    models.py            # Decision enum, PlantDecision, etc.
    watering.py          # PURE decision logic (no I/O)
    messages.py          # PURE message formatting
    telegram.py          # send_message() only
    weather/
        models.py        # DailyWeather, WeatherData
        base.py          # WeatherProvider protocol + ProviderError
        open_meteo.py  weatherapi.py  openweather.py
        service.py       # fallback chain + persistence of normalized data
tests/                   # pytest, fixtures in tests/fixtures/
```

## Commands

```bash
python -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python -m pytest -q
.venv/bin/python garden_water.py --dry-run     # live weather, no Telegram, no reminder state written
.venv/bin/python garden_water.py               # normal run (cron)
```

Flags: `--config PATH`, `--db PATH`, `--dry-run`, `--today YYYY-MM-DD` (testing only), `-v`.
Exit codes: `0` ok · `1` all weather providers failed / insufficient data · `2` config error · `3` Telegram send failed.

## Design rules

1. **`watering.py` is pure.** It takes normalized `DailyWeather` data, plant config,
   thresholds, today's date and reminder state; returns a decision. No HTTP, no
   SQLite, no `datetime.now()`, no provider names.
2. **Providers hide their JSON.** Each provider module does the HTTP request, auth,
   parsing and conversion to `WeatherData`. Raising `ProviderError` (short message)
   is the only failure signal. Nothing outside `weather/<provider>.py` touches
   provider-specific fields.
3. **All thresholds live in config** (`watering:` section). Defaults are in one
   place: `DEFAULT_WATERING` at the top of `config.py`, mirrored with comments in
   `config.example.yaml` (a test keeps them in sync). No magic numbers in logic code.
4. **Persist normalized daily weather**, never raw API responses.
5. **Decisions** are exactly: `NO_ACTION`, `POSTPONE`, `WATER`, `FOLLOW_UP`.
   Only `WATER` and `FOLLOW_UP` produce Telegram text. `POSTPONE` is log/DB only.
6. "Today" and "tomorrow" are computed in the configured garden timezone.
   The rain window is the 7 days ending **today (included)**; look-ahead is **tomorrow's** forecast.
7. Explainability: every decision carries a short human-readable `reason`;
   `--dry-run` prints the inputs and the reason.
8. Prefer a function over a class. Add a dataclass only when it removes confusion.

## Readability (the code must be easy to read and tweak by a human)

- `decide_plant()` in `watering.py` reads top to bottom with `# Step 1 … # Step 8`
  comments matching `DEVELOPMENT_PLAN.md` §2. Use named intermediate variables
  (`rain_7d`, `effective_target`, `rain_fraction`, `wait_threshold`), no clever one-liners.
- Thresholds are expressed in plain terms: "water when the plant got less than X% of
  its target" (`min_rain_fraction`), "wait if tomorrow's rain is at least N mm"
  (`skip_if_tomorrow_rain_mm`). Pass them as a small frozen `Thresholds` dataclass.
- Every decision `reason` is a plain sentence built from those named variables.
- Short files (aim < 200 lines), type hints, docstrings that explain *why*.
- Each provider file: build request → check response → parse → convert, with a
  top comment linking the API doc and listing the fields used.
- `config.example.yaml` has a plain-language comment on every setting.

## Security / logging

- Secrets only from env (`TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `WEATHERAPI_KEY`,
  `OPENWEATHER_API_KEY`). `.env`, `config.yaml`, `*.db`, logs are gitignored.
- API keys appear in URLs (Telegram token path, `key=`, `appid=` params), and
  `requests` exceptions include the URL. **Always pass errors through
  `redact()`** before logging or sending them to Telegram.
- A provider with no API key configured is **skipped** (logged), not treated as a failure.
- Log: startup, date/season, provider chosen, provider failures, history used,
  tomorrow's forecast, per-plant decision + reason, Telegram sent/not, final status.

## Testing rules

- Never call live APIs in tests. Use recorded/hand-written JSON fixtures and a fake
  HTTP session via monkeypatch.
- `watering.py` tests use plain dataclasses, no DB. Database tests use `:memory:` or `tmp_path`.
- Every item in the plan's test matrix must exist before the project is "done".
- `config.example.yaml`'s `watering:` values must equal `DEFAULT_WATERING` (tested).
- Run `pytest -q` before reporting any phase complete.

## Working agreements

- **Verify current official API docs before writing each provider** (Phase 0).
  Record findings in `docs/api-notes.md`. Do not rely on memory or old tutorials.
  If a doc contradicts the plan, follow the doc and note the change in the plan.
- Work phase by phase; keep each phase small and tested. Update `DEVELOPMENT_PLAN.md`
  checkboxes as you go.
- README must explain: algorithm, `rain_target_mm_7d`, `water_need`, thresholds,
  provider fallback, adding a provider, cron setup.
- When a requirement is ambiguous, prefer the simpler behavior and write the
  assumption in the plan's "Decisions & assumptions" section.
