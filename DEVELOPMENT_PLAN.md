# DEVELOPMENT_PLAN.md — garden-water

Plan for building the garden-watering reminder. Read `CLAUDE.md` first for the rules.
Tick the checkboxes as phases complete.

**Revision 2:** cron runs in the **evening**; today's rain counts; thresholds rewritten
in plain "percent of target" terms; readability rules added.

---

## 1. Decisions & assumptions

Choices made where the brief was open. Change them here first, then in code.

| # | Decision | Why |
|---|----------|-----|
| D1 | Package is `gardenwater/` (no underscore); entry script stays `garden_water.py`. | A `garden_water.py` file next to a `garden_water/` package is legal but confusing. CLI from the brief is preserved. |
| D2 | The run happens in the **evening** (suggested 19:00 local). The rain window is **7 days ending today, today included**. | By evening most of today's rain has fallen, so today counts. |
| D3 | "Today's rain" = the provider's daily total for today (rain so far + any remaining forecast for the day). Looking ahead uses **tomorrow's** daily forecast only. Hourly data and probability are out of scope for v1. | Simple; one number per day from every provider. |
| D4 | Dates `≤ today` are stored in `weather_daily` (one row per date, latest write wins). Today's row is provisional and is overwritten by the final value on the next run. Tomorrow's forecast is stored on the decision row. | Keeps the weather table "actuals", mixed providers across days work. |
| D5 | Each run fetches ~10 past days plus today and upserts them, so missed cron days self-heal. | Cheap, robust. |
| D6 | Missing days in the window are excluded and the total is scaled (`sum × 7/n`) if `n ≥ min_history_days` (default 5). Otherwise the run aborts as "insufficient data" (exit 1, one Telegram error message). | Never treat missing data as "dry". |
| D7 | Provider **failure** = timeout, network error, HTTP error, auth error, malformed JSON, or missing today's / tomorrow's precipitation. Missing API key = **skipped**, not failed. | Spec list + avoids alert noise for unused providers. |
| D8 | Provider-failure Telegram warning is sent once when a provider goes `ok → failed`, then suppressed until it recovers (`failed → ok` resets). No recovery message. | "Do not send every day." |
| D9 | "All providers failed" message is sent **every run** it happens (the system is blind), exit code 1. | Silent failure is worse than a repeated alert. One-line change to make it once per outage. |
| D10 | Watering message and provider-warning message are separate messages (max 2 per run). | Different purposes. Watering is still one combined message. |
| D11 | A notification counts as sent only if Telegram returned success. Failed sends are recorded as `failed` and do **not** create reminder state. Exit code 3. | State must reflect what the user actually received. |
| D12 | Re-running the same day never re-sends a watering message (checked via `watering_decisions`). | Idempotent cron/manual runs. |
| D13 | `--dry-run` may upsert weather data (harmless cache) but writes **no** decisions, reminder state, or provider-status changes, and sends nothing. | Dry runs must not alter reminder behavior. |
| D14 | `plant.type` and `plant.count` are validated but not used by the algorithm in v1. | Reserved for later. |
| D15 | Telegram messages are plain text (no `parse_mode`). | No escaping bugs. |
| D16 | HTTP via `requests` (timeouts 10 s, no retries beyond the provider chain). `.env` loaded by a ~10-line parser in `config.py` (no python-dotenv). | Few dependencies. |
| D17 | The brief's `severe_deficit_multiplier` is replaced by `severe.wait_multiplier` (see algorithm). | Clearer meaning. |
| D18 | Trigger thresholds are expressed as **"water when the plant got less than X% of its target rain"** (`min_rain_fraction`), not as a "deficit ratio". | A gardener can read and tune "less than 50% of target". |
| D19 | **Rain since the reminder** counts dates *after* the reminder date through today. | The reminder is sent in the evening, so rain on the reminder day mostly fell before it. |
| D20 | Defaults live in **one** place (`DEFAULT_WATERING` at the top of `config.py`). `config.example.yaml` shows the full `watering:` section with the same values and comments; a test keeps them in sync. | Humans edit the YAML; code has no hidden numbers. |
| D21 | **OpenWeather provider is disabled for now.** Live-tested 2026-10-07: One Call 4.0 `timeline/1day`, One Call 3.0 `onecall` and `day_summary` all answer HTTP 401 ("requires the separate One Call by Call subscription"); free `2.5/forecast` answered "Invalid API key" and has no daily history anyway. `gardenwater/weather/openweather.py` is a documented stub, **not registered** in the fallback chain (which is Open-Meteo → WeatherAPI). Re-enable path: subscribe the key → capture fixture → implement per `docs/api-notes.md` → register in `service.py`. | Do not build a provider that cannot run. "Missing API key = skipped" (D7) extends naturally to "provider unavailable = skipped". Two working providers keep v1 shippable. |

---

## 2. How the program decides (plain language)

### Inputs

| Input | Source |
|-------|--------|
| Current season | today's month → `season_calendar` |
| `water_need` (low/medium/high) | plant × season config |
| `rain_target_mm_7d` | plant × season config |
| Rain over the last 7 days (today included) | SQLite `weather_daily` |
| Recent heat / ET0 (same 7 days) | SQLite `weather_daily` |
| Tomorrow's forecast rain | this run's weather fetch |
| Reminder state | SQLite `reminder_state` |

### Steps (per plant)

1. **Season** from today's date; load that season's `water_need` and `rain_target_mm_7d`.
2. **Rain 7d** = sum of precipitation for the 7 days ending today (scaled if days are missing, D6).
3. **Heat factor** (≥ 1.0 — cool weather means normal demand, never less):
   - If ET0 exists for at least half the window days:
     `factor = clamp(mean_et0 / et0_baseline_mm_day, 1.0, max_factor)`
   - Else, using mean daily max temperature:
     `factor = clamp(1 + (mean_tmax − temp_threshold_c) × temp_step_per_c, 1.0, max_factor)`
   - No temperature data at all, or heat adjustment disabled: `factor = 1.0`.
4. **Effective target** = `rain_target_mm_7d × factor`.
5. **Rain fraction** = `rain_7d / effective_target` ("the plant got 40% of what it wants");
   if the target is 0, the fraction is 1.0 (nothing needed). `deficit_mm = max(0, effective_target − rain_7d)` is reported for humans.
6. **Is it dry enough to care?** If `rain_fraction ≥ min_rain_fraction[water_need]` → **NO_ACTION**
   (and any open reminder for the plant is cleared).
7. **Is rain coming that justifies waiting?**
   `wait_threshold = skip_if_tomorrow_rain_mm[water_need]`.
   If the deficit is **severe** (`rain_fraction ≤ severe.rain_fraction` and `reminders_count ≥ severe.min_reminders`),
   `wait_threshold *= severe.wait_multiplier` (it takes much more rain to justify waiting).
   If `tomorrow_mm ≥ wait_threshold` → **POSTPONE** (log/DB only, reminder stays open, no Telegram).
8. **Otherwise it's time to water.** Look at reminder state:
   - open reminder exists **and** rain since the reminder `< meaningful_rain_mm` → **FOLLOW_UP**
   - no open reminder, **or** meaningful rain fell since the reminder (streak resets) → **WATER**
9. `WATER` / `FOLLOW_UP` are collected into **one** Telegram message; each sent plant updates reminder state.

### Why `water_need` matters

It changes two numbers only: how little rain makes the plant a candidate (`min_rain_fraction`),
and how much tomorrow rain makes us wait (`skip_if_tomorrow_rain_mm`).
`low` tolerates a lot of dryness and waits for even a light shower.
`high` wants water sooner and only waits for a proper soaking.

### Default thresholds and why

Single place: `DEFAULT_WATERING` in `config.py`, mirrored with comments in `config.example.yaml`.
I reviewed the brief's numbers and kept their intent, restated as "percent of target".

```yaml
watering:
  recent_days: 7
  meaningful_rain_mm: 5          # rain at or above this ends a reminder streak
  min_history_days: 5            # minimum days of data inside the window

  heat_adjustment:
    enabled: true
    et0_baseline_mm_day: 3.0     # ET0 at/below this = normal demand
    temp_threshold_c: 28         # mean daily max above this = extra demand
    temp_step_per_c: 0.03        # +3% demand per °C above the threshold
    max_factor: 1.5              # demand is never raised by more than 50%

  need_profiles:                 # per water_need
    low:
      min_rain_fraction: 0.30            # water only if it got < 30% of target
      skip_if_tomorrow_rain_mm: 5        # a light shower is enough to wait
    medium:
      min_rain_fraction: 0.50            # < 50% of target
      skip_if_tomorrow_rain_mm: 8
    high:
      min_rain_fraction: 0.65            # < 65% of target
      skip_if_tomorrow_rain_mm: 12       # waits only for a real soaking

  severe:
    rain_fraction: 0.15          # got ≤ 15% of target
    min_reminders: 2             # at least this many reminders already sent in the streak
    wait_multiplier: 2.0         # then tomorrow's rain must be 2× larger to justify waiting
```

Reasoning: `meaningful_rain_mm: 5` is roughly what wets the soil beyond the surface, so smaller
showers don't cancel a reminder. Heat adds at most +50%, so the target stays "approximate home garden"
rather than a model. Forecasts tend to overstate light rain, hence `high` needs 12 mm before waiting.

### Quick tuning cheat-sheet (goes in README)

| You see | Change |
|---------|--------|
| Too many reminders for a plant type | lower that `min_rain_fraction` (e.g. medium 0.50 → 0.40) |
| Plants look dry before a reminder comes | raise `min_rain_fraction` |
| You watered, then it rained the next day (wasted water) | lower `skip_if_tomorrow_rain_mm` (wait for smaller forecasts) |
| Forecast rain often doesn't arrive and plants get dry | raise `skip_if_tomorrow_rain_mm` (wait only for bigger forecasts) |
| Reminders repeat after a decent rain | lower `meaningful_rain_mm` |
| Hot spells not noticed | lower `temp_threshold_c` or raise `temp_step_per_c` |
| One plant is always off | change its `rain_target_mm_7d` for that season |

### Worked checks against the brief's examples (heat factor = 1.0)

| Ex | Setup | Rain fraction | Trigger (<) | Tomorrow vs wait | Result |
|----|-------|---------------|-------------|------------------|--------|
| A | summer, medium, 45/40 mm, tomorrow 0 | 1.13 | 0.50 | — | NO_ACTION |
| B | summer, high, 5/40, tomorrow 0 | 0.13 | 0.65 | 0 < 12 | WATER |
| C | summer, low, 8/40, tomorrow 12 | 0.20 | 0.30 | 12 ≥ 5 | POSTPONE |
| D | open reminder, rain since < 5 mm, tomorrow 0 | low | — | 0 < wait | FOLLOW_UP |
| E | open reminder, low, tomorrow 12 | low | — | 12 ≥ 5 | POSTPONE |
| Severe | high, fraction 0.05, 2 reminders, tomorrow 14 | 0.05 | 0.65 | 14 < 12×2=24 | WATER (without severe it would POSTPONE: 14 ≥ 12) |
| Severe-low | low, fraction 0.05, 3 reminders, tomorrow 12 | 0.05 | 0.30 | 12 ≥ 5×2=10 | POSTPONE |

---

## 3. Data model

### Config (`config.yaml`)

Sections: `location`, `season_calendar`, `plants`, `water_requirements`, `watering` (optional; defaults above).
See `config.example.yaml` (peach example from the brief, coordinates `0.0`, timezone placeholder).

Validation (all errors collected and reported together, exit 2):
- latitude ∈ [−90, 90], longitude ∈ [−180, 180], timezone valid for `zoneinfo`.
- `season_calendar`: exactly the four seasons; months 1–12 each in exactly one season.
- plants: `name`, `type` present; `count` numeric > 0; names unique.
- every plant has a `water_requirements` entry and vice versa; all four seasons present;
  `water_need ∈ {low, medium, high}`; `rain_target_mm_7d` numeric ≥ 0.
- `watering` overrides: numeric, sensible ranges (fractions in 0..1, multiplier ≥ 1, etc.); all three `need_profiles` present.

### Weather model (`weather/models.py`)

```python
@dataclass
class DailyWeather:
    date: date
    precipitation_mm: float
    temperature_avg_c: float | None
    temperature_max_c: float | None
    et0_mm: float | None

@dataclass
class WeatherData:
    provider: str
    history: list[DailyWeather]    # dates before today
    today: DailyWeather            # required
    forecast: list[DailyWeather]   # tomorrow first; at least 1 entry required
```

Provider interface (`weather/base.py`): `name: str`, `is_configured() -> bool`,
`fetch(lat, lon, tz, today, past_days) -> WeatherData`; raises `ProviderError(short_message)`.

### SQLite schema (`database.py`, created idempotently on start)

```sql
CREATE TABLE IF NOT EXISTS weather_daily (
  date TEXT PRIMARY KEY,            -- YYYY-MM-DD, garden timezone; today's row is provisional
  provider TEXT NOT NULL,
  precipitation_mm REAL NOT NULL,
  temperature_avg_c REAL,
  temperature_max_c REAL,
  et0_mm REAL,
  fetched_at TEXT NOT NULL          -- ISO 8601 UTC
);

CREATE TABLE IF NOT EXISTS provider_status (
  provider TEXT PRIMARY KEY,
  state TEXT NOT NULL,              -- 'ok' | 'failed'
  failed_since TEXT,
  last_error TEXT,                  -- redacted, short
  last_notified_at TEXT,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS watering_decisions (
  decision_date TEXT NOT NULL,
  plant TEXT NOT NULL,
  season TEXT NOT NULL,
  water_need TEXT NOT NULL,
  decision TEXT NOT NULL,           -- NO_ACTION | POSTPONE | WATER | FOLLOW_UP
  reason TEXT NOT NULL,
  rain_7d_mm REAL NOT NULL,
  effective_target_mm REAL NOT NULL,
  rain_fraction REAL NOT NULL,
  deficit_mm REAL NOT NULL,
  heat_factor REAL NOT NULL,
  tomorrow_rain_mm REAL NOT NULL,
  rain_since_reminder_mm REAL,
  provider TEXT NOT NULL,
  notification_type TEXT,           -- NULL | 'WATER' | 'FOLLOW_UP'
  notification_status TEXT NOT NULL DEFAULT 'none',  -- none | sent | failed
  decided_at TEXT NOT NULL,
  PRIMARY KEY (decision_date, plant)
);

CREATE TABLE IF NOT EXISTS reminder_state (
  plant TEXT PRIMARY KEY,
  last_notified_on TEXT NOT NULL,
  reminders_count INTEGER NOT NULL  -- notifications in the current streak
);
```

Reminder state rules: row created/updated only after a **successful** send;
`reminders_count += 1` on each send; row **deleted** on `NO_ACTION`, or when a `WATER`
is produced because meaningful rain ended the streak (re-created with count 1 after sending).
`POSTPONE` leaves it untouched.

### Messages (`messages.py`, plain text)

```
🌱 Garden watering needed

Peach
• Season: summer
• Water need: high
• Rain last 7 days: 5 mm
• Tomorrow: 0 mm
• Status: watering recommended
```

Follow-up plants use `• Status: still appears to need watering` and
`• No meaningful rain (X mm) since the previous reminder (YYYY-MM-DD)`.
If every plant in the message is a follow-up, the header is `🌱 Garden watering reminder`.
Provider warning and all-failed texts exactly as in the brief (error text redacted and truncated to ~200 chars).
Insufficient data (D6): `⚠️ Garden watering check failed` + `Not enough recent weather data to decide.`
Never use wording that claims the user did or did not water.

---

## 4. Readability rules for the code (the code must be easy to read and tweak)

These apply to every phase:

1. `watering.py` has **one main function**, `decide_plant()`, written top to bottom with comments
   `# Step 1 … # Step 8` that match §2 exactly. Named intermediate variables (`rain_7d`, `effective_target`,
   `rain_fraction`, `wait_threshold`), no clever one-liners, no nested helpers more than one level.
2. Thresholds arrive as a small frozen dataclass (`Thresholds`) built from config. Logic code never contains
   a bare number except 0/1.
3. Every decision's `reason` is a plain sentence built from those named variables,
   e.g. `"got 13% of target (5.0/40.0 mm), below 65%; tomorrow 0.0 mm < 12 mm wait limit"`.
4. Type hints everywhere, docstrings that say *why*, files short (target < 200 lines each; `config.py` may exceed).
5. Each provider file reads top to bottom: build request → check response → parse → convert. A short comment
   at the top links the API doc page and lists the field names used.
6. `config.example.yaml` has a comment on every setting in plain language.
7. README has the tuning cheat-sheet from §2 and a "where do I change X?" table (thresholds → `watering:` in YAML;
   season months → `season_calendar`; add a plant → `plants` + `water_requirements`; message text → `messages.py`).

---

## 5. Phases

### Phase 0 — Scaffold and API verification
- [x] Create repo skeleton, `.gitignore` (`.env`, `config.yaml`, `*.db`, `*.log`, `.venv`), `requirements.txt`, empty modules, `tests/`.
- [x] **Verify current official docs** for each provider; write `docs/api-notes.md` with: endpoint URL, auth method, required params, units, how to get past days, **today's daily total**, and **tomorrow's daily forecast**, field names for precipitation / temp avg / temp max / ET0, free-tier limits, error format, one trimmed sample response.
  - Open-Meteo: forecast endpoint with `past_days` and `daily=` variables (expected: precipitation sum, temperature max/mean, `et0_fao_evapotranspiration`); confirm `timezone` handling and that today's daily sum mixes observed and forecast values.
  - WeatherAPI.com: forecast + history endpoints; confirm history range on the free plan, day structure (`totalprecip_mm`, `avgtemp_c`, `maxtemp_c`), whether any ET0 exists. History is likely one request per day — fetch only dates missing from SQLite.
  - OpenWeather: ~~unverified~~ **verified 2026-10-07 — see D21**: One Call 4.0 exists (`timeline/1day`, history + forecast in one, `appid`, "One Call by Call" subscription) but the key got 401 on 4.0/3.0/day_summary, and `2.5/forecast` said invalid key. **Provider disabled for now** (D21); recorded in `docs/api-notes.md`.
- [x] Save trimmed real responses as `tests/fixtures/<provider>_*.json`.
- **Done when:** `api-notes.md` answers, for each provider, "can it give history / today / tomorrow / temp / ET0?" with doc links, and fixtures exist. ✅ 2026-10-07: Open-Meteo + WeatherAPI fixtures recorded live; OpenWeather documented and disabled (D21).

### Phase 1 — Config and validation
- [x] `config.py`: dataclasses, `DEFAULT_WATERING` at the top, `Thresholds`, `.env` loader, validation (§3), `redact()` helper.
- [x] `config.example.yaml` (peach, full commented `watering:` section), `.env.example`.
- **Tests:** `test_config.py` — valid load; four seasons load (#10); missing season rejected (#11); month in two seasons / missing month rejected; bad lat/lon/tz; bad `water_need`; negative target; plant with no requirements; fraction outside 0..1 rejected; `redact()` strips keys/tokens; month→season mapping (#9); **`config.example.yaml` watering values equal `DEFAULT_WATERING`**.

### Phase 2 — Models and database
- [x] `weather/models.py`, `models.py` (Decision enum, `PlantDecision`, `ReminderState`).
- [x] `database.py`: schema init, upsert weather, read window, provider status get/set, decision upsert, reminder state get/set/clear.
- **Tests:** `test_database.py` — schema idempotent; upsert overwrites same date (today's provisional row replaced next day); window query; reminder lifecycle; provider status transitions.

### Phase 3 — Watering algorithm (core)
- [ ] `watering.py`: `season_for(date, calendar)`, `heat_factor(window, thresholds)`, `rain_in_window(...)`, `decide_plant(...) -> PlantDecision` implementing §2 exactly, following §4 readability rules. Pure functions.
- [ ] Decision carries all numbers needed for the DB row, dry-run output and messages.
- **Tests:** `test_watering.py` — #1–#9 plus examples A–E and Severe/Severe-low from the table; today's rain is counted in the window; scaled window with missing days; reminder streak reset by meaningful rain; zero target → NO_ACTION.

### Phase 4 — Weather providers
- [ ] `weather/open_meteo.py`, `weatherapi.py` per `api-notes.md`. Each: `is_configured`, request with timeouts, status-code handling, parse → `WeatherData`, wrap every failure in `ProviderError` with a redacted short message. (`openweather.py` stays a disabled stub per D21 — implement only if the subscription lands.)
- [ ] Missing today's or tomorrow's precipitation → `ProviderError`.
- **Tests:** `test_weather_parsers.py` — fixture → expected `DailyWeather` values per provider; malformed JSON; missing field; HTTP 401/429/500 via fake session; keys never present in error text.

### Phase 5 — Weather service (fallback + status)
- [ ] `weather/service.py`: try providers in priority order, skip unconfigured, persist `history` + `today` (not forecast) to `weather_daily`, update `provider_status`, return `(WeatherData, failures)`.
- [ ] Failure-notification decision: `ok → failed` ⇒ notify once; still failed ⇒ silent; recovered ⇒ reset.
- **Tests:** `test_weather_service.py` — fallback order (#12); mixed-provider days in DB; failure warning once not daily (#13); all fail raises `AllProvidersFailed`; unconfigured provider skipped silently.

### Phase 6 — Telegram and messages
- [ ] `telegram.py`: `send_message(token, chat_id, text)` via `sendMessage`, timeout, raises `TelegramError` (redacted).
- [ ] `messages.py`: watering message (WATER/FOLLOW_UP/mixed), provider warning, all-failed, insufficient data.
- **Tests:** `test_messages.py` — exact text for single plant, multi-plant, follow-up only, mixed; no forbidden phrasing ("you didn't water"); `test_telegram.py` — payload shape, error redaction.

### Phase 7 — Orchestration, CLI, logging
- [ ] `app.py`: parse args → load config → init DB → fetch weather → for each plant read window + reminder state → `decide_plant` → record decisions → build one message → send (unless dry-run or already sent today, D12) → update reminder state only after successful send → send provider warning if needed → exit code.
- [ ] Dry-run printout, human-readable, for example:
  ```
  Plant: peach          Season: summer     Water need: high
  Rain last 7 days: 5.2 mm
  Target: 40 mm (×1.05 for heat = 42 mm)
  Plant got 12% of its target; it gets watered below 65%
  Tomorrow rain: 0.5 mm (waits only at 12 mm or more)
  Decision: WATER
  Reason: got 12% of target, below 65%; tomorrow 0.5 mm is under the 12 mm wait limit
  ```
- [ ] Logging config (stdout, timestamps, `-v`); startup/season/provider/decision/telegram/final-status lines; redaction applied.
- **Tests:** `test_app.py` with fake providers and fake Telegram — dry-run sends nothing and writes no decisions (#15); all providers failing → exit 1 + all-failed message (#14); Telegram failure → exit 3, no reminder state; three-day scenario (day 1 WATER, day 2 FOLLOW_UP, day 3 meaningful rain → NO_ACTION); same-day re-run sends nothing; provider-failure message sent once across two runs.

### Phase 8 — Docs and smoke test
- [ ] `README.md`: setup, config reference, `rain_target_mm_7d` meaning (approximate natural rain the plant would ideally get over a rolling 7 days in that season; not scientific), `water_need`, thresholds table, tuning cheat-sheet, "where do I change X?" table, algorithm walkthrough (§2), provider fallback diagram, "add a provider" (create module, implement protocol, register in `service.py` list, add fixture + parser test), cron, troubleshooting.
- [ ] Cron example (evening): `0 19 * * * /path/to/venv/bin/python /path/to/garden-water/garden_water.py >> /path/to/garden-water/garden-water.log 2>&1`. Note: cron uses the **server's** timezone for the 19:00 trigger, while "today" is computed in the garden timezone from config. `.env` is resolved relative to the script path, not the working directory.
- [ ] Manual smoke test: real coordinates, `--dry-run` with each provider key set/unset; one real Telegram test message.
- **Done when:** all checkboxes ticked, `pytest -q` green, README matches behavior.

---

## 6. Test matrix (brief §31 → location)

| # | Requirement | Test file |
|---|-------------|-----------|
| 1 | enough rain → NO_ACTION | test_watering |
| 2 | little rain + dry tomorrow → WATER | test_watering |
| 3 | little rain + rain tomorrow + low need → POSTPONE | test_watering |
| 4 | high need + severe + modest rain tomorrow → WATER | test_watering |
| 5 | earlier reminder + dry + still dry → FOLLOW_UP | test_watering |
| 6 | reminder + rain tomorrow + low need → POSTPONE | test_watering |
| 7 | hot weather raises demand | test_watering |
| 8 | ET0 raises demand | test_watering |
| 9 | month → season | test_config |
| 10 | four seasons load | test_config |
| 11 | missing season rejected | test_config |
| 12 | provider fallback | test_weather_service |
| 13 | provider failure not re-notified daily | test_weather_service, test_app |
| 14 | all providers fail → non-zero exit | test_app |
| 15 | dry-run doesn't send Telegram | test_app |
| extra | today's rain counted in window | test_watering |
| extra | example YAML in sync with code defaults | test_config |

---

## 7. Out of scope for v1

Hourly rain and rain probability, soil moisture, acknowledgement ("I watered"),
per-plant watering amounts, multiple locations, automatic irrigation, recovery notifications.

## 8. Definition of done

- `pytest -q` passes with no network access.
- `--dry-run` against live APIs prints a clear explanation per plant.
- A normal run on a dry day sends exactly one Telegram message; on a fine day, none.
- No secret appears in logs, DB, or Telegram text.
- README lets a non-agronomist change thresholds and add a plant.
