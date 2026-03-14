# entsoe-prices

Fetches day-ahead electricity prices from the [ENTSO-E Transparency Platform](https://transparency.entsoe.eu), selects the cheapest charging windows for the target day, and saves a plan to JSON for review or further use.

```
  ══════════════════════════════════════════════════════════════════
  ENTSO-E Charging Plan
  ══════════════════════════════════════════════════════════════════
  Date      2026-03-15   Area FI   Source ENTSO-E
  Timezone  Europe/Helsinki (UTC+2)

  Market prices   0.47 min  1.64 avg  4.27 max  c€/kWh

  Scheduled  240 min of 240 min required  ↓ 62% below market avg
  Avg price  0.62 c€/kWh

  Charging windows (1):
    03:00–07:00  ████████████████  0.62 c€/kWh  4h00m

  ══════════════════════════════════════════════════════════════════
```

Terminal output is colour-coded: market price stats are shown in green (min) / yellow (avg) / red (max), and each charging window bar is coloured green→yellow→red by its price relative to the day's range.

---

## Requirements

- Python 3.11+
- [`pyyaml`](https://pypi.org/project/PyYAML/) — `pip install pyyaml`
- An ENTSO-E API key (free — see [Getting an API key](#getting-an-api-key))

No other dependencies. The script uses only the standard library, including [`zoneinfo`](https://docs.python.org/3/library/zoneinfo.html) (stdlib since Python 3.9) for DST-correct timezone handling.

---

## Getting an API key

1. Register at [transparency.entsoe.eu](https://transparency.entsoe.eu)
2. Email [transparency@entsoe.eu](mailto:transparency@entsoe.eu) to request API access
3. Your key will appear under **My Account → Security Tokens**

Day-ahead prices are published at approximately 13:00 CET each day for the following day. If the script is run before prices are available it exits cleanly with a warning rather than an error.

---

## Usage

```bash
# Run with defaults (reads config.yaml, writes plan-*.json to current directory)
python entsoe_prices.py

# Specify config and output directory
python entsoe_prices.py --config my-config.yaml --output-dir /tmp/plans

# Enable debug logging
python entsoe_prices.py --debug
```

The script prints a formatted summary to stdout for each profile and writes one `plan-{name}.json` file per profile. These files can be reviewed, hand-edited, and consumed by downstream systems.

---

## Configuration

Create a `config.yaml` next to the script. The `charging` key accepts either a single profile or a list of named profiles:

```yaml
entsoe:
  api_key: "your-api-key-here"
  area: "FI"                    # see Supported areas below

# Single profile (simple form):
charging:
  name: "default"               # optional; used in output filename and notifications
  required_hours: 4
  contiguous_only: false
  merge_gaps: true
  min_slot_minutes: 30
  max_price_cents_kwh: null
  preferred_window_start: "00:00"
  preferred_window_end: "06:30"
  timezone: "Europe/Helsinki"
```

Or multiple named profiles — prices are fetched once and each profile runs its own selection:

```yaml
entsoe:
  api_key: "your-api-key-here"
  area: "FI"

charging:
  - name: "topup"
    required_hours: 2
    contiguous_only: false
    preferred_window_start: "00:00"
    preferred_window_end: "06:30"
    timezone: "Europe/Helsinki"

  - name: "overnight"
    required_hours: 6
    contiguous_only: true
    preferred_window_start: "00:00"
    preferred_window_end: "06:30"
    timezone: "Europe/Helsinki"
```

`entsoe.api_key`, `entsoe.area`, `preferred_window_start`, and `preferred_window_end` are required — all other fields have defaults. Each profile produces its own `plan-{name}.json` output file.

### Configuration reference

| Key | Default | Description |
|---|---|---|
| `entsoe.api_key` | — | **Required.** ENTSO-E security token |
| `entsoe.area` | — | **Required.** Bidding zone short code or full EIC (e.g. `FI`, `10YFI-1--------U`) |
| `charging.name` | `"default"` | Profile name. Used in the output filename (`plan-{name}.json`) and phone notification |
| `charging.required_hours` | `4` | Hours of charging to schedule |
| `charging.contiguous_only` | `false` | `true` = one unbroken block; `false` = cheapest slots (may be split across the day) |
| `charging.merge_gaps` | `true` | Bridge gaps shorter than `min_slot_minutes` between selected blocks. Ignored when `contiguous_only` is `true` |
| `charging.min_slot_minutes` | `30` | Minimum contiguous block length. Must be a multiple of 15 |
| `charging.max_price_cents_kwh` | `null` | Skip slots above this price (c€/kWh). `null` = no ceiling |
| `charging.preferred_window_start` | — | **Required.** Start of preferred charging window (`HH:MM`, `00:00`–`23:59`) |
| `charging.preferred_window_end` | — | **Required.** End of preferred charging window (`HH:MM`). If end is before start the window is treated as overnight and wraps midnight (e.g. `22:00`–`06:30`). Equal start and end is an error. Treat the end time as your departure — charging is scheduled as late as possible within the window |
| `charging.timezone` | `null` | IANA timezone name (e.g. `"Europe/Helsinki"`). `null` = auto-detect from `/etc/timezone`. Used with `zoneinfo` for DST-correct window resolution — the correct UTC offset is applied for each specific date, including DST transition days. An explicit but unrecognised name is a hard config error (exits with a clear message); auto-detection falling back to UTC is allowed silently |

### Preferred window

The planner fills as many slots as possible from within the preferred window first, then spills over outside it only if needed to meet `required_hours`. To impose no restriction on timing, set `preferred_window_start: "00:00"` and `preferred_window_end: "23:59"`.

**Overnight windows** — if `preferred_window_end` is earlier in the day than `preferred_window_start`, the window is treated as crossing midnight. For example, `22:00`–`06:30` covers 22:00 on the plan date through 06:30 the following morning. Equal start and end times are an error (zero-length window).

**Spill direction** — spillover never goes *after* `preferred_window_end`. If extra slots are needed they are always taken from before the window start. The planner fetches today's remaining prices alongside tomorrow's so that spill can reach back into the current evening if necessary.

**`contiguous_only` and spill** — when `contiguous_only: true` and `required_hours` exceeds the preferred window length, the single contiguous block is extended leftward: charging simply starts earlier than the window start rather than jumping to a separate block later in the day. `preferred_window_end` is always respected as a hard ceiling — no slot will be scheduled after it even in this path. If there are not enough hours available before the window the plan will be short by the deficit (a warning is logged).

### Gap merging

After slot selection, if two chosen blocks are separated by a gap **shorter than `min_slot_minutes`**, the gap is bridged automatically. The intervening slots are included to form one continuous block, then slots are trimmed from the **beginning** of the merged block until the total charging time returns to `required_hours`. Trimming from the start pushes the charging window as late as possible — closer to your departure time. Merged windows are flagged with ⚡ in the terminal output, GitHub Actions summary, and phone notification.

This behaviour can be disabled by setting `merge_gaps: false`. It is automatically skipped when `contiguous_only: true` since the result is already one unbroken block.

---

## Supported areas

| Code | Region |
|---|---|
| `FI` | Finland |
| `SE1`–`SE4` | Sweden (bidding zones) |
| `NO1`–`NO5` | Norway (bidding zones) |
| `DK1`, `DK2` | Denmark |
| `EE` | Estonia |
| `LV` | Latvia |
| `LT` | Lithuania |
| `DE` | Germany |
| `FR` | France |
| `NL` | Netherlands |
| `BE` | Belgium |
| `AT` | Austria |
| `PL` | Poland |

You can also pass a full EIC code directly (e.g. `10YFI-1--------U`).

---

## Plan JSON format

One `plan-{name}.json` file is written per profile. The format is straightforward and easy to hand-edit:

```json
{
  "version": 1,
  "date": "2026-03-15",
  "area": "FI",
  "price_source": "ENTSO-E",
  "timezone": "Europe/Helsinki",
  "utc_offset_hours": 2,
  "profile": "overnight",
  "price_stats": {
    "min_cents_kwh": 0.82,
    "max_cents_kwh": 7.21,
    "avg_cents_kwh": 3.14
  },
  "required_minutes": 240,
  "total_minutes": 240,
  "avg_price_cents_kwh": 0.91,
  "preferred_window_start": "00:00",
  "preferred_window_end": "07:00",
  "windows": [
    {
      "start": "03:00",
      "end": "07:00",
      "duration_minutes": 240,
      "avg_price_cents_kwh": 0.91,
      "gap_merged": false
    }
  ],
  "window_starts_utc": [
    "2026-03-15T01:00:00+00:00"
  ],
  "window_ends_utc": [
    "2026-03-15T05:00:00+00:00"
  ]
}
```

`profile` is the name of the charging profile that produced this plan, matching the `name` field in config.

`gap_merged` is `true` when the window was formed by bridging a sub-`min_slot_minutes` gap between two originally separate blocks.

`utc_offset_hours` is derived from `zoneinfo` at noon on the target date — it represents the offset in effect for that day (e.g. `2` for EET, `3` for EEST) and is used for display purposes only. All internal scheduling is done in UTC.

`window_starts_utc` lists the UTC ISO 8601 start time of each charging window, in order. `window_ends_utc` lists the corresponding stop times. Useful for downstream systems that need to start and stop charging at the right UTC times.

`price_stats` reflects **tomorrow's prices only** — it does not include any spill slots pulled from today's evening. This means `avg_price_cents_kwh` (your scheduled average) can occasionally be lower than `price_stats.min_cents_kwh` when spillover selected unusually cheap slots from earlier today.

---

## GitHub Actions

Place `schedule.yml` in `.github/workflows/`. The workflow runs once daily at 12:00 UTC, builds the plan, and uploads it as a workflow artifact for review.

### Required secret

Add a single repository secret:

| Secret | Value |
|---|---|
| `ENTSOE_API_KEY` | Your ENTSO-E security token |

### Workflow

```
Settings → Secrets and variables → Actions → New repository secret
```

The workflow runs once daily at 12:00 UTC — 14:00 Helsinki time in winter (EET, UTC+2) and 15:00 in summer (EEST, UTC+3). A single cron covers both DST states because 12:00 UTC lands after ENTSO-E's ~13:00 CET publication window in all cases.

To trigger a run manually: **Actions → ENTSO-E Charging Plan → Run workflow**.

### Job summary

Each successful run writes a formatted summary into the GitHub Actions job view, and saves a `chart.svg` price chart to the workspace which is uploaded as part of the run artifact alongside all `plan-{name}.json` files.

The SVG chart shows:

- **Pink filled area** — day-ahead prices at full 15-minute slot resolution (c€/kWh)
- **Blue shading** — preferred charging window
- **Purple bar** — scheduled charging windows along the x-axis
- **Y-axis** — price in c€/kWh with gridlines; x-axis spans 00:00–24:00 local time

See [`example_chart.svg`](example_chart.svg) for a rendered example.

To view the chart: open any workflow run → click the run artifact → download the zip → open `chart.svg` in any browser.

---

## Phone notification (ntfy)

The workflow sends a push notification via [ntfy.sh](https://ntfy.sh) after each successful run. Install the ntfy app on iOS or Android, subscribe to your topic, and you'll receive the plan each afternoon.

The notification shows all profiles in a single message, ordered by required hours ascending (shortest first). Each profile has its own windows and ruler:

```
⚡ Charging plan for 2026-03-15

topup
03:30–04:30  0.62 c€/kWh
00:00 ▒▒▒▒▒▒██▒▒▒▒▒▒▒▒ 07:00

overnight
00:00–04:00  0.50 c€/kWh
00:00 ████████▒▒▒▒▒▒▒▒ 07:00
```

The ruler spans the preferred charging window (`▒` = preferred window unscheduled, `░` = outside preferred window unscheduled, `█` = scheduled). If any slots fall outside the preferred window the ruler automatically expands to show them, and each affected slot is flagged.

Additional per-slot indicators:

| Indicator | Meaning |
|---|---|
| `⚡ merged` | Two blocks separated by a short gap were merged into one continuous window |
| `⚠️ outside window` | Window falls outside the preferred charging window (computed from `preferred_window_start`/`end` at notification time, not stored in plan JSON) |

---

## How it works

1. **Fetch** — makes a single query to the ENTSO-E Transparency Platform API (`documentType=A44`, endpoint `web-api.tp.entsoe.eu`) spanning `(today-1)T20:00Z` – `(today+1)T23:00Z`, covering all slots needed for both same-day and overnight windows. Returns all non-past slots; each profile's window filter picks what it needs from the result. No hardcoded target date — the window anchor is derived per profile from the clock
2. **Parse** — handles 15-, 30-, and 60-minute resolution data; deduplicates overlapping periods; trims today's slots to those within reach of the preferred window
3. **Select** — picks the cheapest slots totalling `required_hours`, respecting `min_slot_minutes` block length and the optional price ceiling (`max_price_cents_kwh`); among equally priced options the latest slots are always preferred (closest to `preferred_window_end`); spills leftward into today's evening if needed
4. **Plan** — bridges sub-`min_slot_minutes` gaps between blocks (trimming from the start of the merged block to preserve total charging time and push the window as late as possible), merges adjacent slots into contiguous windows, computes stats, writes JSON
5. **Display** — prints a colour-coded terminal summary (header, market price stats in green/yellow/red, charging windows coloured by relative price); writes a markdown summary and saves `chart.svg` (pink area chart, preferred window shading, purple charging bar) for the GitHub Actions run artifact
