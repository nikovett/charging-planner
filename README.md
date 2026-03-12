# entsoe-prices

Fetches day-ahead electricity prices from the [ENTSO-E Transparency Platform](https://transparency.entsoe.eu), selects the cheapest charging windows for the next day, and saves a plan to JSON for review or further use.

```
  ══════════════════════════════════════════════════════════════════
  ENTSO-E Charging Plan
  ══════════════════════════════════════════════════════════════════
  Date      2026-03-15   Area FI   Source ENTSO-E
  Timezone  Europe/Helsinki (UTC+2)

  Market prices   0.82 min  3.14 avg  7.21 max  c€/kWh

  Scheduled  240 min of 240 min required  ↓ 71% below market avg
  Avg price  0.91 c€/kWh

  Charging windows (1):
    03:00–07:00  ████████████████  0.91 c€/kWh  4h00m

  Hour  c€/kWh                  Price profile
  ──────────────────────────────────────────────────────────
  00:00   2.10  ▒▒▒▒▒▒▒▒▒▒▒▒░░░░░░░░░░░░░░░░░░░░░░░░░░
  01:00   1.80  ▒▒▒▒▒▒▒▒▒░░░░░░░░░░░░░░░░░░░░░░░░░░░░░
  02:00   1.50  ▒▒▒▒▒▒░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░
  03:00   1.20  ███░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░ ◀
  04:00   1.00  █░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░ ◀
  05:00   0.90  █░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░ ◀
  06:00   1.10  ██░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░ ◀
  ...
```

Terminal output is colour-coded green→yellow→red by relative price. Selected hours show a filled bar (`█`) with a `◀` marker.

---

## Requirements

- Python 3.11+
- [`pyyaml`](https://pypi.org/project/PyYAML/) — `pip install pyyaml`
- An ENTSO-E API key (free — see [Getting an API key](#getting-an-api-key))

No other dependencies. The script uses only the standard library.

---

## Getting an API key

1. Register at [transparency.entsoe.eu](https://transparency.entsoe.eu)
2. Email [transparency@entsoe.eu](mailto:transparency@entsoe.eu) to request API access
3. Your key will appear under **My Account → Security Tokens**

Day-ahead prices are published at approximately 13:00 CET each day for the following day.

---

## Usage

```bash
# Run with defaults (reads config.yaml, writes plan.json)
python entsoe_prices.py

# Specify paths explicitly
python entsoe_prices.py --config my-config.yaml --plan output/plan.json

# Enable debug logging
python entsoe_prices.py --debug
```

The script prints a formatted plan to stdout and writes the full plan to JSON. The JSON file can be reviewed, hand-edited, and consumed by downstream systems.

---

## Configuration

Create a `config.yaml` next to the script:

```yaml
entsoe:
  api_key: "your-api-key-here"
  area: "FI"                    # see Supported areas below

charging:
  required_hours: 4             # total hours of charging needed
  contiguous_only: false        # true = one unbroken block; false = cheapest individual slots
  min_slot_minutes: 30          # minimum block length (must be divisible by 15)
  schedule_next_day: true       # plan for tomorrow (false = today)
  max_price_cents_kwh: null     # optional price ceiling, e.g. 5.0
  preferred_window_start: "00:00"  # optional: prefer slots within this window
  preferred_window_end: "07:00"
  timezone: "Europe/Helsinki"   # null = auto-detect from system
```

All fields have defaults — a minimal config only needs `entsoe.api_key` and `entsoe.area`.

### Configuration reference

| Key | Default | Description |
|---|---|---|
| `entsoe.api_key` | — | **Required.** ENTSO-E security token |
| `entsoe.area` | `10YFI-1--------U` | Bidding zone — short code or full EIC (see below) |
| `charging.required_hours` | `4` | Hours of charging to schedule |
| `charging.contiguous_only` | `false` | `true` = one unbroken block; `false` = cheapest slots (may be split across the day) |
| `charging.min_slot_minutes` | `30` | Minimum contiguous block length. Must be a multiple of 15 |
| `charging.schedule_next_day` | `true` | `true` = plan tomorrow; `false` = plan today |
| `charging.max_price_cents_kwh` | `null` | Skip slots above this price (c€/kWh). `null` = no ceiling |
| `charging.preferred_window_start` | `null` | Prefer slots starting at or after this local time (`HH:MM`) |
| `charging.preferred_window_end` | `null` | Prefer slots ending at or before this local time (`HH:MM`) |
| `charging.timezone` | `null` | IANA timezone name. `null` = auto-detect from `/etc/timezone` |

### Preferred window

When `preferred_window_start` / `preferred_window_end` are set, the planner fills as many slots as possible from within that window first, then spills over to hours outside it only if needed to meet `required_hours`. This is useful for preferring overnight off-peak hours while still guaranteeing a full charge.

### Gap merging

After slot selection, if two chosen blocks are separated by a gap **shorter than `min_slot_minutes`**, the gap is bridged automatically. The intervening slots are included to form one continuous block, then the most expensive 15-minute slot from either end of the merged block is dropped to keep the total charging time at `required_hours`. This avoids impractically short gaps where stopping and restarting a charger would make no sense.

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

The saved plan is straightforward and easy to hand-edit:

```json
{
  "version": 1,
  "date": "2026-03-15",
  "area": "FI",
  "price_source": "ENTSO-E",
  "timezone": "Europe/Helsinki",
  "utc_offset_hours": 2,
  "price_stats": {
    "min_cents_kwh": 0.82,
    "max_cents_kwh": 7.21,
    "avg_cents_kwh": 3.14
  },
  "required_minutes": 240,
  "total_minutes": 240,
  "avg_price_cents_kwh": 0.91,
  "windows": [
    {
      "start": "03:00",
      "end": "07:00",
      "duration_minutes": 240,
      "avg_price_cents_kwh": 0.91
    }
  ]
}
```

Window times are in **local time** (as specified by `timezone`), making them easy to read and edit by hand.

---

## GitHub Actions

Place `schedule.yml` in `.github/workflows/`. The workflow runs daily at 14:30 Helsinki time (after ENTSO-E publishes next-day prices at ~13:00 CET), builds the plan, and uploads it as a workflow artifact for review.

### Required secret

Add a single repository secret:

| Secret | Value |
|---|---|
| `ENTSOE_API_KEY` | Your ENTSO-E security token |

### Workflow

```
Settings → Secrets and variables → Actions → New repository secret
```

The cron schedule runs twice daily to handle both summer (EEST, UTC+3) and winter (EET, UTC+2) time. Only one run will find prices on any given day — the other exits cleanly.

To trigger a run manually: **Actions → ENTSO-E Charging Plan → Run workflow**.

---

## How it works

1. **Fetch** — queries the ENTSO-E Transparency API (`documentType=A44`) for the target date
2. **Parse** — handles 15-, 30-, and 60-minute resolution data; deduplicates overlapping periods
3. **Select** — picks the cheapest slots totalling `required_hours`, respecting `min_slot_minutes` block length and the optional preferred window
4. **Plan** — bridges sub-`min_slot_minutes` gaps between blocks (trimming the costliest endpoint slot to compensate), merges adjacent slots into contiguous windows, computes stats, writes JSON
5. **Display** — prints a colour-coded terminal summary with a 24-hour price bar chart
