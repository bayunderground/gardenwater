# For LLMs: editing `config.yaml`

You are editing the configuration of **garden-water**, a small gardening
reminder script. The human talking to you describes plants and watering
preferences in plain text; you turn that into a correct `config.yaml`.

## Inputs and outputs

**You will get:**

1. the current `config.yaml` (or asked to create one from `config.example.yaml`),
2. plain-text wishes, e.g. *"add two tomato pots — they're thirsty in summer,
   and I barely water them in winter"*, or *"make the peach less needy"*.

**You must produce:**

1. the **complete, updated `config.yaml`** as one code block (full file, valid
   YAML, comments preserved — never a diff or fragment),
2. a short list of what you changed and why,
3. open questions if the user's text left something ambiguous (ask only what
   actually matters; otherwise state your assumption).

**Never touch any other file.** No code, no `gardenwater/`, no `.env`.
**Secrets never belong in config.yaml** — API tokens live in `.env` only.
`config.yaml` is gitignored; do not run any git command for it.

## The schema (exactly what the validator enforces)

The loader is `gardenwater/config.py` (`load_config`). It collects **all**
problems and reports them together as `- <field>: <problem>`.

### `location` — required

```yaml
location:
  latitude: 44.5165591     # number, -90..90
  longitude: 33.5003639    # number, -180..180
  timezone: Europe/Moscow  # non-empty IANA name, must exist in zoneinfo
```

All three keys are required. `"UTC"` is always valid; e.g. `Europe/Moscow`,
`Asia/Tashkent`, `Europe/Berlin`.

### `season_calendar` — required, exactly four seasons

```yaml
season_calendar:
  spring: [3, 4, 5]
  summer: [6, 7, 8]
  autumn: [9, 10, 11]
  winter: [12, 1, 2]
```

- Season names must be exactly `spring`, `summer`, `autumn`, `winter` — no
  other keys allowed.
- Every month 1–12 must appear **exactly once** across the four lists.
- Only change this if the user's climate has different seasons (e.g.
  wet/dry). It decides which `water_requirements` block applies today.

### `plants` — required, non-empty list

```yaml
plants:
  - name: peach          # required, non-empty, unique
    type: fruit-tree     # required, non-empty string (free text, unused by the algorithm)
    count: 1             # required, number > 0 (unused by the algorithm)
```

`name` is the plant's identity: it must match a key under `water_requirements`
**character for character**, it is stored in SQLite (decisions, reminder
streaks), and it is **capitalized in Telegram messages** (`peach` → "Peach").
Prefer short lowercase names, one word or hyphenated (`peach`, `cherry-tomato`).

> **Renaming warning:** the reminder streak and past decisions are keyed by
> name. Renaming `peach` → `peach-tree` starts a fresh streak (the old row is
> simply no longer referenced). If the user wants a rename, confirm they accept
> losing the reminder history for that plant.

### `water_requirements` — required, same names as `plants`

Every plant in `plants` needs an entry here, and **all four seasons**:

```yaml
water_requirements:
  peach:
    spring: {water_need: medium, rain_target_mm_7d: 30}
    summer: {water_need: high,   rain_target_mm_7d: 40}
    autumn: {water_need: low,    rain_target_mm_7d: 20}
    winter: {water_need: low,    rain_target_mm_7d: 10}
```

- `water_need` must be exactly `low`, `medium` or `high`.
- `rain_target_mm_7d`: number ≥ 0 (millimetres over 7 days).
- Extra entries for plants **not** in `plants` are rejected, and vice versa —
  the two lists must describe the same set of names.

### `watering` — optional (thresholds)

Omit it unless the user explicitly asks to tune thresholds; the built-in
defaults then apply. If present, it is deep-merged over the defaults, so a
partial section is fine. Valid ranges (for reference):

| Key | Type / range | Default |
|---|---|---|
| `recent_days` | int 1–92 | 7 |
| `meaningful_rain_mm` | number ≥ 0 | 5 |
| `min_history_days` | int 1–`recent_days` | 5 |
| `heat_adjustment.enabled` | `true`/`false` | true |
| `heat_adjustment.et0_baseline_mm_day` | number ≥ 0.001 | 3.0 |
| `heat_adjustment.temp_threshold_c` | any number | 28 |
| `heat_adjustment.temp_step_per_c` | number ≥ 0 | 0.03 |
| `heat_adjustment.max_factor` | number ≥ 1 | 1.5 |
| `need_profiles.<low\|medium\|high>.min_rain_fraction` | 0–1 | 0.30 / 0.50 / 0.65 |
| `need_profiles.<low\|medium\|high>.skip_if_tomorrow_rain_mm` | number ≥ 0 | 5 / 8 / 12 |
| `severe.rain_fraction` | 0–1 | 0.15 |
| `severe.min_reminders` | int ≥ 1 | 2 |
| `severe.wait_multiplier` | number ≥ 1 | 2.0 |

Unknown keys (anywhere) are silently **ignored** — do not add them.

## Choosing values from plain text

### `water_need` — how dry before we remind

| Value | Remind when the plant got less than… | Wait if tomorrow brings ≥ | Use for |
|---|---|---|---|
| `high` | 65 % of its target | 12 mm | thirsty, fruiting crops in season, sun-baked pots |
| `medium` | 50 % | 8 mm | most vegetables and flower beds |
| `low` | 30 % | 5 mm | herbs, succulents, established plants that tolerate dry |

### `rain_target_mm_7d` — the gardener's ideal rain over 7 days

A rough "how much rain would this plant ideally see in a week right now" —
**not** a scientific irrigation figure. It is compared against the actual
7-day rainfall (heat can raise it by up to ×1.5).

Shipped example (a peach): spring `medium/30`, summer `high/40`,
autumn `low/20`, winter `low/10`. Typical starting points:

| Plant type | Seasons where it grows | Suggested target (mm/7d) |
|---|---|---|
| fruit trees, berries | spring–summer | 30–40 |
| fruiting vegetables (tomato, cucumber, pepper) | summer | 40–60 |
| leafy vegetables, flower beds | spring–autumn | 25–40 |
| herbs, Mediterranean plants | all year | 10–25 |
| succulents, cacti | all year | 5–15 |
| anything in winter dormancy | winter | 5–10 |

Treat these as **starting points**: when the user's text doesn't say, pick
from the table, mention the number you chose, and let them tune it.

### Mapping example

> *"add two cherry tomato pots, they're really thirsty in summer, nothing in
> winter"*

```yaml
plants:
  # ...existing plants...
  - name: cherry-tomato
    type: vegetable
    count: 2

water_requirements:
  # ...existing plants...
  cherry-tomato:
    spring: {water_need: medium, rain_target_mm_7d: 30}
    summer: {water_need: high,   rain_target_mm_7d: 50}
    autumn: {water_need: medium, rain_target_mm_7d: 25}
    winter: {water_need: low,    rain_target_mm_7d: 10}
```

(The user said "nothing in winter" — dormancy is expressed as `low` with a
tiny target, *not* by omitting the season: all four seasons are mandatory.)

## Before you output — checklist

1. Valid YAML; the file is complete from `location:` to the last line.
2. `plants` names and `water_requirements` keys are the **same set**, exact
   spelling, no duplicates.
3. Every plant has all four seasons; `water_need` ∈ {low, medium, high};
   all numbers are plain numbers, never quoted strings.
4. No secrets, no unknown keys, no changes outside the sections the user asked
   about (comments preserved).
5. Season names exactly `spring/summer/autumn/winter`.

## If you have shell access, validate

```bash
.venv/bin/python - <<'EOF'
from pathlib import Path
from gardenwater.config import ConfigError, load_config
try:
    load_config(Path("config.yaml"))
except ConfigError as exc:
    raise SystemExit(f"INVALID:\n{exc}")
print("config OK")
EOF
```

`INVALID:` lists **every** problem at once — fix them all, re-run, and only
then hand the file to the user. Optional live check (needs network, sends
nothing): `.venv/bin/python garden_water.py --dry-run`.
