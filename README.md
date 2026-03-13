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

Day-ahead prices are published at approximately 13:00 CET each day for the following day. If the script is run before prices are available it exits cleanly with a warning rather than an error.

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
  merge_gaps: true              # bridge gaps shorter than min_slot_minutes between selected blocks
  min_slot_minutes: 30          # minimum block length (must be divisible by 15)
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
| `charging.merge_gaps` | `true` | Bridge gaps shorter than `min_slot_minutes` between selected blocks. Ignored when `contiguous_only` is `true` |
| `charging.min_slot_minutes` | `30` | Minimum contiguous block length. Must be a multiple of 15 |
| `charging.max_price_cents_kwh` | `null` | Skip slots above this price (c€/kWh). `null` = no ceiling |
| `charging.preferred_window_start` | `null` | Prefer slots starting at or after this local time (`HH:MM`) |
| `charging.preferred_window_end` | `null` | Prefer slots ending at or before this local time (`HH:MM`) |
| `charging.timezone` | `null` | IANA timezone name. `null` = auto-detect from `/etc/timezone` |

### Preferred window

When `preferred_window_start` / `preferred_window_end` are set, the planner fills as many slots as possible from within that window first, then spills over outside it only if needed to meet `required_hours`. This is useful for preferring overnight off-peak hours while still guaranteeing a full charge.

**Spill direction** — spillover never goes *after* `preferred_window_end`. If extra slots are needed they are always taken from before the window start (i.e. earlier in the evening or the current day). The planner fetches today's remaining prices alongside tomorrow's so that spill can reach back into the current evening if necessary.

**`contiguous_only` and spill** — when `contiguous_only: true` and `required_hours` exceeds the preferred window length, the single contiguous block is extended leftward: charging simply starts earlier than the window start rather than jumping to a separate block later in the day. If there are not enough hours available before the window the plan will be short by the deficit (a warning is logged).

### Gap merging

After slot selection, if two chosen blocks are separated by a gap **shorter than `min_slot_minutes`**, the gap is bridged automatically. The intervening slots are included to form one continuous block, then the most expensive 15-minute slot from either end of the merged block is dropped to keep the total charging time at `required_hours`. On a price tie, the earliest slot is always dropped. Merged windows are flagged with ⚡ in the terminal output, GitHub Actions summary, and phone notification.

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
  ]
}
```

`preferred_window_start` and `preferred_window_end` are `null` if no preferred window is configured. `gap_merged` is `true` when the window was formed by bridging a sub-`min_slot_minutes` gap between two originally separate blocks.

`price_stats` reflects **tomorrow's prices only** — it does not include any spill slots pulled from today's evening. This means `avg_price_cents_kwh` (your scheduled average) can occasionally be lower than `price_stats.min_cents_kwh` when spillover selected unusually cheap slots from earlier today.

---

## GitHub Actions

Place `schedule.yml` in `.github/workflows/`. The workflow runs twice daily (landing at 14:30 Helsinki time in both summer and winter), builds the plan, and uploads it as a workflow artifact for review.

### Required secret

Add a single repository secret:

| Secret | Value |
|---|---|
| `ENTSOE_API_KEY` | Your ENTSO-E security token |

### Workflow

```
Settings → Secrets and variables → Actions → New repository secret
```

The workflow is scheduled twice daily — at 11:30 UTC and 12:30 UTC — to land at 14:30 Helsinki time regardless of whether DST is in effect (EEST, UTC+3 in summer; EET, UTC+2 in winter). On any given day only one run will find prices; the other exits cleanly with no side effects.

To trigger a run manually: **Actions → ENTSO-E Charging Plan → Run workflow**.

### Job summary chart

Each successful run writes an SVG price chart directly into the GitHub Actions job summary. The chart shows:

- **Pink filled area** — 15-minute resolution day-ahead prices (c€/kWh)
- **Blue shading** — preferred charging window
- **Purple bar** — scheduled charging windows

Open any workflow run and click the job name to see the summary with the chart inline.

---

## Phone notification (ntfy)

The workflow sends a push notification via [ntfy.sh](https://ntfy.sh) after each successful run. Install the ntfy app on iOS or Android, subscribe to your topic, and you'll receive the plan each afternoon.

The notification looks like this:

```
⚡ Charging plan 2026-03-15
2026-03-15  ·  120 min  ·  avg 0.18 c€/kWh

00:00      03:30       07:00
▒▒▒▒▒▒▒▒████████▒▒▒▒▒▒▒▒▒▒▒▒

02:00–04:00  0.18 c€/kWh
```

The ruler spans the preferred charging window (`▒` = preferred window unscheduled, `░` = outside preferred window unscheduled, `█` = scheduled). If any slots fall outside the preferred window the ruler automatically expands to show them, and each affected slot is flagged:

```
00:00            05:00             10:00
▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒░░░░████████

08:00–10:00  0.54 c€/kWh  ⚠️ outside window
```

Additional per-slot indicators:

| Indicator | Meaning |
|---|---|
| `⚡ merged` | Two blocks separated by a short gap were merged into one continuous window |
| `⚠️ outside window` | Slot falls outside the preferred charging window |

---

## How it works

1. **Fetch** — queries the ENTSO-E Transparency API (`documentType=A44`) for tomorrow's prices and today's remaining prices; exits cleanly if tomorrow's prices are not yet available
2. **Parse** — handles 15-, 30-, and 60-minute resolution data; deduplicates overlapping periods; trims today's slots to those within reach of the preferred window
3. **Select** — picks the cheapest slots totalling `required_hours`, respecting `min_slot_minutes` block length and the optional preferred window; spills leftward into today's evening if needed
4. **Plan** — bridges sub-`min_slot_minutes` gaps between blocks (trimming the costliest endpoint slot to compensate), merges adjacent slots into contiguous windows, computes stats, writes JSON
5. **Display** — prints a colour-coded terminal summary with a 24-hour price bar chart; writes an SVG chart into the GitHub Actions job summary
