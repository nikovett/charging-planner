# charging-planner

Fetches day-ahead electricity prices from the [ENTSO-E Transparency Platform](https://transparency.entsoe.eu) and schedules EV charging for the cheapest available hours — automatically, every day.

---

## Overview

The script makes a single API call to fetch all available day-ahead prices, then runs each configured charging profile against the price data independently. Each profile picks its own cheapest windows within its preferred time range and writes a plan to JSON.

```
  ══════════════════════════════════════════════════════════════════
  Charging Planner
  ══════════════════════════════════════════════════════════════════
  Date      2026-03-15   Area FI   Source ENTSO-E
  Timezone  Europe/Helsinki (UTC+2)

  Market prices   0.47 min  1.64 avg  4.27 max  c€/kWh

  Scheduled  4h of 4h required  ↓ 62% below market avg
  Avg price  0.62 c€/kWh

  Charging windows (1):
    03:00–07:00  ████████████████  0.62 c€/kWh  4h00m

  ══════════════════════════════════════════════════════════════════
```


---

## Requirements

- Python 3.11+
- [`pyyaml`](https://pypi.org/project/PyYAML/) — `pip install pyyaml`
- An ENTSO-E API key (free)

No other dependencies. The script uses only the standard library, including [`zoneinfo`](https://docs.python.org/3/library/zoneinfo.html) (stdlib since Python 3.9) for DST-correct timezone handling.

## Getting an API key

1. Register at [transparency.entsoe.eu](https://transparency.entsoe.eu)
2. Email [transparency@entsoe.eu](mailto:transparency@entsoe.eu) to request API access
3. Your key will appear under **My Account → Security Tokens**

Day-ahead prices are published at approximately 12:00 UTC each day. A run before publication will return a partial plan — whatever slots are already available — with a warning if the required hours could not be fully scheduled.

---

## Configuration

### Multiple profiles

The `charging` key accepts either a single profile or a list of named profiles. Prices are fetched once and each profile runs its own independent selection:

```yaml
entsoe:
  api_key: "your-api-key-here"
  area: "FI"

charging:
  - name: "topup"
    required_hours: 2
    continuous_only: false
    preferred_window_start: "00:00"
    preferred_window_end: "06:30"
    timezone: "Europe/Helsinki"

  - name: "overnight"
    required_hours: 6
    continuous_only: true
    preferred_window_start: "22:00"
    preferred_window_end: "06:30"
    timezone: "Europe/Helsinki"
```

Each profile produces its own `plan-{name}.json` output file.

### Reference

| Key | Default | Description |
|---|---|---|
| `entsoe.api_key` | — | **Required.** ENTSO-E security token |
| `entsoe.area` | — | **Required.** Bidding zone short code or full EIC (e.g. `FI`, `10YFI-1--------U`) |
| `charging.name` | `"default"` | Profile name — used in the output filename (`plan-{name}.json`) and phone notification |
| `charging.required_hours` | `4` | Hours of charging to schedule |
| `charging.continuous_only` | `false` | `true` = one unbroken block; `false` = cheapest individual slots (may be split) |
| `charging.min_slot_minutes` | `30` | Minimum continuous block length. Must be a multiple of 15 |
| `charging.max_price_cents_kwh` | `null` | Skip slots above this price (c€/kWh). `null` = no ceiling |
| `charging.preferred_window_start` | — | **Required.** Start of preferred charging window (`HH:MM`) |
| `charging.preferred_window_end` | — | **Required.** End of preferred charging window (`HH:MM`). If earlier in the day than `preferred_window_start` the window wraps midnight (overnight). Equal start and end is an error |
| `charging.timezone` | `null` | IANA timezone name (e.g. `"Europe/Helsinki"`). `null` = auto-detect from system. DST transitions are handled correctly via `zoneinfo` |

### Preferred window behaviour

The planner fills as many slots as possible from within the preferred window first, then spills leftward outside it only if needed to meet `required_hours`. Spillover never goes after `preferred_window_end`. To impose no restriction on timing, set `preferred_window_start: "00:00"` and `preferred_window_end: "23:59"`.

**Overnight windows** — if `preferred_window_end` is earlier in the day than `preferred_window_start`, the window wraps midnight. For example, `22:00`–`06:30` covers 22:00 tonight through 06:30 tomorrow morning.

**`continuous_only` and spill** — when `continuous_only: true` and `required_hours` exceeds the window length, the single block extends leftward past the window start. `preferred_window_end` is always the hard ceiling — no slot is ever scheduled after it.

**Guaranteed charge until departure time** — setting `required_hours` longer than the window with `continuous_only: true` ensures the block always ends exactly at `preferred_window_end`. For example, `preferred_window_start: "01:00"`, `preferred_window_end: "06:30"`, `required_hours: 8` always produces a block ending at 06:30 and starting as early as 22:30 the previous evening. The planner finds the cheapest available 8h slot that fits before the deadline. A practical benefit for EVs in cold climates is that charging ends close to departure, leaving the battery warm and improving range.

### Gap merging

When two selected blocks are separated by a gap shorter than `min_slot_minutes`, they are bridged into one continuous window automatically. Slots are then trimmed from the merged block to bring the total back to `required_hours` — most expensive slots are removed first, with earliest slots trimmed as a tiebreaker. Merged windows are flagged with ⚡ in all outputs. Gap merging is skipped when `continuous_only: true` since the result is already one unbroken block.

---

## Running

### Locally

```bash
python charging_planner.py
python charging_planner.py --config my-config.yaml --output-dir /tmp/plans
python charging_planner.py --debug
```

### GitHub Actions

Place `schedule.yml` in `.github/workflows/`. Add a single repository secret:

```
Settings → Secrets and variables → Actions → New repository secret
```

| Secret | Value |
|---|---|
| `ENTSOE_API_KEY` | Your ENTSO-E security token |

The workflow runs daily at 12:30 UTC — 14:30 Helsinki time in winter (EET, UTC+2) and 15:30 in summer (EEST, UTC+3). A single cron covers both DST states because 12:30 UTC always lands after ENTSO-E's ~12:00 UTC publication time.

To trigger a run manually: **Actions → Charging Planner → Run workflow**.

Each successful run writes a formatted markdown summary to the GitHub Actions job view and uploads a run artifact containing all `plan-{name}.json` files.

---

## Outputs

### Plan JSON

One `plan-{name}.json` file is written per profile:

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
  "required_minutes": 360,
  "total_minutes": 360,
  "avg_price_cents_kwh": 0.91,
  "preferred_window_start": "22:00",
  "preferred_window_end": "06:30",
  "windows": [
    {
      "start": "00:00",
      "end": "06:00",
      "duration_minutes": 360,
      "avg_price_cents_kwh": 0.91,
      "gap_merged": false
    }
  ],
  "window_starts_utc": ["2026-03-14T22:00:00+00:00"],
  "window_ends_utc":   ["2026-03-15T04:00:00+00:00"],
  "ocpp_charging_profile": { ... }
}
```

`window_starts_utc` and `window_ends_utc` are UTC ISO 8601 timestamps for each charging window — use these to start and stop charging in downstream systems. `utc_offset_hours` is for display only; all internal scheduling is UTC-native.

`price_stats` reflects the plan date's prices only and does not include any spill slots from the current evening.

### OCPP smart charging

Each plan includes an `ocpp_charging_profile` field containing a ready-to-use OCPP `ChargingProfile` object:

```json
"ocpp_charging_profile": {
  "chargingProfileId": 1,
  "stackLevel": 0,
  "chargingProfilePurpose": "TxDefaultProfile",
  "chargingProfileKind": "Absolute",
  "validFrom": "2026-03-14T22:00:00+00:00",
  "validTo":   "2026-03-15T04:00:00+00:00",
  "chargingSchedule": {
    "startSchedule":    "2026-03-14T22:00:00+00:00",
    "duration":         21600,
    "chargingRateUnit": "W",
    "chargingSchedulePeriod": [
      { "startPeriod": 0, "limit": 11000.0 }
    ]
  }
}
```

This is compatible with **OCPP 1.6**, **2.0.1**, and **2.1** — pass as `csChargingProfiles` in a `SetChargingProfile.req` message. The profile is `TxDefaultProfile` (`Absolute` kind), meaning it applies automatically to any transaction started on the EVSE without needing a transaction ID in advance.

Charging windows run at `max_charging_rate` (default 11 kW); gaps between windows are explicitly set to `limit: 0` so the charger does not charge outside the planned slots. `validFrom`/`validTo` bound the profile to the planned day.

**OCPP 2.0.1 and 2.1** use `id` instead of `chargingProfileId`. If you are integrating directly with the script, call `build_ocpp_charging_profile(plan, ocpp_version="2.0.1")` to generate the correct structure for your CSMS.

### Plan JSON

One `plan-{name}.json` file is written per profile:

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
  "required_minutes": 360,
  "total_minutes": 360,
  "avg_price_cents_kwh": 0.91,
  "preferred_window_start": "22:00",
  "preferred_window_end": "06:30",
  "windows": [
    {
      "start": "00:00",
      "end": "06:00",
      "duration_minutes": 360,
      "avg_price_cents_kwh": 0.91,
      "gap_merged": false
    }
  ],
  "window_starts_utc": ["2026-03-14T22:00:00+00:00"],
  "window_ends_utc":   ["2026-03-15T04:00:00+00:00"],
  "ocpp_charging_profile": { ... }
}
```

`window_starts_utc` and `window_ends_utc` are UTC ISO 8601 timestamps for each charging window — use these to start and stop charging in downstream systems. `utc_offset_hours` is for display only; all internal scheduling is UTC-native.

`price_stats` reflects the plan date's prices only and does not include any spill slots from the current evening.

### OCPP smart charging

Each plan includes an `ocpp_charging_profile` field containing a ready-to-use OCPP `ChargingProfile` object:

```json
"ocpp_charging_profile": {
  "chargingProfileId": 1,
  "stackLevel": 0,
  "chargingProfilePurpose": "TxDefaultProfile",
  "chargingProfileKind": "Absolute",
  "validFrom": "2026-03-14T22:00:00+00:00",
  "validTo":   "2026-03-15T04:00:00+00:00",
  "chargingSchedule": {
    "startSchedule":    "2026-03-14T22:00:00+00:00",
    "duration":         21600,
    "chargingRateUnit": "W",
    "chargingSchedulePeriod": [
      { "startPeriod": 0, "limit": 11000.0 }
    ]
  }
}
```

This is compatible with **OCPP 1.6**, **2.0.1**, and **2.1** — pass as `csChargingProfiles` in a `SetChargingProfile.req` message. The profile is `TxDefaultProfile` (`Absolute` kind), meaning it applies automatically to any transaction started on the EVSE without needing a transaction ID in advance.

Charging windows run at `max_charging_rate` (default 11 kW); gaps between windows are explicitly set to `limit: 0` so the charger does not charge outside the planned slots. `validFrom`/`validTo` bound the profile to the planned day.

**OCPP 2.0.1 and 2.1** use `id` instead of `chargingProfileId`. If you are integrating directly with the script, call `build_ocpp_charging_profile(plan, ocpp_version="2.0.1")` to generate the correct structure for your CSMS.

### Phone notification (ntfy)

The GitHub Actions workflow sends a push notification via [ntfy.sh](https://ntfy.sh) after each successful run. The topic name is set in `schedule.yml` — replace `entsoe-charging-f7x3k2` with your own. Install the ntfy app on iOS or Android, subscribe to that topic, and you will receive the plan each afternoon.

All profiles are included in a single message, ordered by required hours ascending:

```
⚡ Charging plan for 2026-03-15

topup  2h/2h
03:30–04:30  0.62 c€/kWh
00:00 ▒▒▒▒▒▒██▒▒▒▒▒▒▒▒░ 07:00

overnight  6h/6h
22:00–06:00  3.12 c€/kWh
21:00 ░████████████▒░░ 07:00
```

If a profile is incomplete (not enough price data yet), the hours are flagged: `topup  1h/2h ⚠️`.

The ruler spans the preferred charging window: `█` = scheduled, `▒` = unscheduled inside window, `░` = outside window. The ruler expands automatically if any slots fall outside the preferred window.

| Indicator | Meaning |
|---|---|
| `⚡ merged` | Gap between two blocks was bridged by gap merging |
| `⚠️ outside window` | Window falls outside the preferred charging window |

---

