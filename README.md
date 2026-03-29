# charging-planner

Fetches day-ahead electricity prices from the [ENTSO-E Transparency Platform](https://transparency.entsoe.eu) and schedules EV charging for the cheapest available hours — automatically, every day.

---

## What makes this different

**Build once, deliver once, charge unsupervised.** Day-ahead prices are published every afternoon for the following day — that's enough information to plan the entire next charging cycle in one go. The result is a charging profile delivered directly to the charger. No further monitoring, no on/off toggling, no always-on process needed.

**Zero hardware required.** Runs as a GitHub Actions cron job — no server, no hub, no Raspberry Pi. Day-ahead prices publish on a predictable schedule, making this a natural fit for a cloud cron. Local cron works too if preferred.

**Fits any setup, any schedule.** A 3.7 kW charger needs long overnight charging; a 22 kW charger benefits from hunting the cheapest short charging windows wherever they fall. Run multiple profiles simultaneously — weekday topup, weekend overnight, each with its own duration, window, mode, and charger — all from one config file. Both the preferred charging window and required hours can be configured per day of the week within each profile.

**Globally optimal scheduling.** Continuous mode finds the cheapest unbroken block ending at departure time. Split mode uses dynamic programming — not a greedy approximation. The result is often non-obvious: three 30-minute windows at 01:00, 03:30 and 05:15 can be significantly cheaper than one 90-minute block at the same total cost.

**Realistic charger behaviour built in.** Independent minimum charging length and minimum gap between charging blocks prevent short on/off cycling — by default the planner won't schedule 15 minutes on, 15 minutes off, on again. The minimum block length and gap are both configurable; the gap can be set to zero if no pause between blocks is needed.

**Previously committed charging is never lost.** Each run reads the previous plan and carries forward any future charging already committed to the charger.

**Three-level price source fallback for Finland.** ENTSO-E → Sähkötin → nordpool-predict-fi forecast. If ENTSO-E is under maintenance at 14:30, the plan still builds and delivers on time using real Nord Pool prices from Sähkötin. The dashboard warns when the plan is based on forecast rather than confirmed prices.

**Works with any car.** The planner schedules the charger, not the car. No car integration, no brand-specific API, no pairing required. Swap cars and nothing changes — the charger infrastructure stays the same and the planner keeps working exactly as before.

**Modular charger delivery.** Each charger type is a small handler script with a single deliver function. A Charge Amps handler is included out of the box — a home automation system, a custom API, or any other target can be added without touching the core planner.

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

No other dependencies. The script otherwise uses only the standard library, including [`zoneinfo`](https://docs.python.org/3/library/zoneinfo.html) (stdlib since Python 3.9) for DST-correct timezone handling.


---

## Getting an API key

1. Register at [transparency.entsoe.eu](https://transparency.entsoe.eu)
2. Email [transparency@entsoe.eu](mailto:transparency@entsoe.eu) to request API access
3. Your key will appear under **My Account → Security Tokens**

---

## Configuration

All configuration lives in a single `config.yaml` file. Deliveries are configured inside each charging profile, so the relationship between a plan and its chargers is explicit and co-located.

### Multiple profiles with deliveries

```yaml
entsoe:
  api_key: ""
  area: FI
  timezone: Europe/Helsinki

charging:
  - name: topup
    required_hours: 2
    continuous_only: false
    min_slot_minutes: 30
    min_gap_minutes: 15
    preferred_window_start: "22:00"
    preferred_window_end: "06:30"
    schedule:
      - days: [monday, tuesday, wednesday, thursday, friday]
        preferred_window_start: "22:00"
        preferred_window_end: "06:30"
        required_hours: 1.5
      - days: [saturday, sunday]
        preferred_window_start: any     # any start + any end = no window constraint
        preferred_window_end: any
        required_hours: 4.5
    deliveries:
      - handler: chargeamps
        charge_point_id: CHARGER_ID_1
        connector_id: 1
        max_charging_rate: 16.0
        restore_mode: false

  - name: overnight
    required_hours: 4.5
    continuous_only: true
    preferred_window_start: "21:00"
    preferred_window_end: "06:30"
    deliveries:
      - handler: chargeamps
        charge_point_id: CHARGER_ID_2
        connector_id: 1
        max_charging_rate: 16.0
        restore_mode: true

```

`charge_point_id` accepts either a single env var name or a list — when a list is given, the same plan is delivered to every charger independently. Timezone is set once in the `entsoe:` block and applies to all profiles and delivery handlers.

### Charging profile reference

| Key | Default | Description |
|---|---|---|
| `entsoe.api_key` | — | **Required.** ENTSO-E security token |
| `entsoe.area` | `FI` | **Required.** Bidding zone short code or full EIC (e.g. `FI`, `10YFI-1--------U`) |
| `entsoe.timezone` | `null` | IANA timezone name (e.g. `"Europe/Helsinki"`). `null` = auto-detect from system. Applies to all profiles |
| `charging.name` | `"default"` | Profile name — used in the output filename (`plan-{name}.json`) |
| `charging.required_hours` | `4` | Hours of charging to schedule |
| `charging.continuous_only` | `false` | `true` = one unbroken block; `false` = cheapest individual slots (may be split) |
| `charging.min_slot_minutes` | `30` | Minimum continuous block length. The charger should not run for less than this duration. Must be 15 minutes or more and a multiple of 15 (the price slot resolution) |
| `charging.min_gap_minutes` | `15` | Minimum gap between charging blocks. Prevents the charger toggling off and straight back on. Must be a multiple of 15. `0` = no gap constraint. Can be set independently of `min_slot_minutes` — e.g. `min_slot_minutes: 120` with `min_gap_minutes: 15` gives 2h blocks with 15-minute gaps |
| `charging.max_price_cents_kwh` | `null` | Skip slots above this price (c€/kWh). `null` = no ceiling |
| `charging.preferred_window_start` | `any` | Start of preferred charging window (`HH:MM`), or `any`. `any` start = use all slots from script run time. `any` + `HH:MM` end = charge anytime until departure time. Both `any` = no constraint. |
| `charging.preferred_window_end` | `any` | End of preferred charging window (`HH:MM`), or `any`. If earlier than `preferred_window_start` the window wraps midnight. Use `23:45` for end of day. `HH:MM` start + `any` end = charge from that time until last available price. Both `any` = no constraint. |
| `charging.schedule` | `[]` | Optional list of day-specific overrides. Each entry has a `days` list (`monday`–`sunday`) and optionally `preferred_window_start`, `preferred_window_end`, and `required_hours`. Any of these can be omitted to fall back to the top-level value. The first matching entry for the target day is used. |

### Preferred window behaviour

**The planner always plans for tomorrow.** The preferred window for tomorrow is taken from the matching `schedule` entry if one exists, otherwise from the top-level `preferred_window_start` / `preferred_window_end`.

A preferred window where start > end (e.g. `22:00–06:30`) wraps midnight — it starts the evening before the target day and ends the morning of the target day. A window where start < end (e.g. `00:00–23:45`) stays within the target day. Note that `00:00–23:45` excludes the last 15-minute slot of the day — use `any` if you want truly unconstrained selection. When both fields are set to `any` (or omitted), there is no window constraint — the planner picks the cheapest slots from all available prices from the script run onwards.

| Schedule entry | Used by | Plans |
|---|---|---|
| `saturday: any` | Friday's run | cheapest slots from all available prices |
| `sunday: any` | Saturday's run | cheapest slots from all available prices |
| `monday: 22:00–06:30` | Sunday's run | Monday (Sunday evening–Monday morning) |
| `friday: 22:00–06:30` | Thursday's run | Friday (Thursday evening–Friday morning) |

Days not listed in `schedule` use the top-level preferred window.

**Slot selection** — for `continuous_only: true`, the planner evaluates all possible contiguous blocks of `required_hours` length and picks the cheapest. For `continuous_only: false`, it uses dynamic programming to find the globally cheapest combination of blocks that together cover exactly `required_hours`, where every block is ≥ `min_slot_minutes` and every gap between blocks is ≥ `min_gap_minutes`. Adjacent selected blocks are merged into a single charging window automatically.

**Per-day required hours** — `required_hours` can be overridden per day within a `schedule` entry, independently of the window override. This allows a single profile to act as a true per-car profile: 1.5h on weekdays for a short commute, 4.5h on weekends for a longer charge. The top-level `required_hours` is the fallback when no schedule entry specifies it.

**Preferred window and spillover** — slots within the configured preferred window are the primary candidates. If the window doesn't contain enough slots to satisfy `required_hours` (too few slots, or all above `max_price_cents_kwh`), the planner adds the cheapest available slots from outside the window to cover the deficit — but never past `preferred_window_end`. When no window is configured (`any`), all available slots are candidates from the start.

**Guaranteed charge until departure time** — setting `required_hours` longer than the window with `continuous_only: true` ensures the block always ends exactly at `preferred_window_end`. Not applicable when using `any`.

---

## Charger delivery

Delivery is handled by `delivery/deliver.py`, which reads the `deliveries:` block inside each charging profile and dispatches each plan to the correct handler. Two handlers are included:

| Handler | Script | Description |
|---|---|---|
| `chargeamps` | `delivery/deliver_chargeamps.py` | Delivers via the `my.charge.space` API |

The `chargeamps` handler supports one additional option: `restore_mode` (default `false`) — when `true`, reads the connector mode before delivery and restores it afterwards if it was not already `Schedule`. Useful if the charger is normally kept in `On` or `Off` mode and should return to that state after the schedule is pushed.

See [`delivery/README.md`](delivery/README.md) for full handler configuration reference and instructions for adding a new handler. New handlers can be added by creating a `deliver_<name>.py` script with a single `deliver` function — the dispatcher handles the rest.

---

## Fallback price sources

When ENTSO-E is unavailable or returns incomplete data, the planner automatically tries two fallback sources before giving up:

1. **Sähkötin** (`sahkotin.fi/api`) — actual realized Nord Pool 15-min prices, same data range as ENTSO-E, Finland only. No API key needed. Used transparently — `price_source: "Sähkötin"` in the plan JSON, no dashboard warning.

2. **nordpool-predict-fi** (`raw.githubusercontent.com/vividfog/nordpool-predict-fi`) — ML forecast blended with realized Sähkötin prices. Hourly data expanded to 15-min slots. By ~14:00–16:00 Helsinki time the forecast transitions to actual market prices, making it nearly as reliable as ENTSO-E once Nord Pool has published. Plans from this source are tagged `price_source: "forecast"` and display a warning banner on the dashboard.

The ENTSO-E fallback triggers in two cases: network/HTTP errors, and when the returned prices don't extend into tomorrow (e.g. during scheduled maintenance where ENTSO-E returns a valid but stale response with only today's data). In either case the planner falls through to Sähkötin automatically.

Both fallbacks are only available for area `FI`. For other areas, the script exits with a non-zero code so the GHA run is marked as failed.

If all three sources fail, the script exits with a non-zero code so the GHA run is marked as failed.

## Histogram display augmentation

Both ENTSO-E and Sähkötin return price slots from the previous evening onwards, including historical prices. This gives the dashboard histogram enough data to the left of "now" for context.

After every successful real-price fetch, the planner always fetches up to 12 hours of forecast data from nordpool-predict-fi beyond the last real price slot. These slots are **display-only** — they are never used for slot selection or optimal calculation. They appear as grey diagonal-striped bars in the histogram with a "forecast" legend entry, making it clear they are estimates rather than confirmed prices. This ensures the right side of the histogram is always filled, even when charging slots fall late the following day.

## Dashboard

A GitHub Pages dashboard is included at `index.html`. It fetches the latest plan JSONs from `data/` and `config.yaml` directly from the repository — no token or backend needed.

Features:

- One flip card per profile — front shows the plan, back shows profile configuration and weekly schedule
- Price histogram with all available slots positioned by actual timestamp. The histogram window uses two modes depending on how far away the charging slots are:
  - **When charging is more than 11 hours away**: the window starts 1 hour before now and extends to cover all charging slots plus 1 hour, so "now" is always visible and the upcoming charging plan is visible to the right
  - **When charging is within 11 hours**: the window centers on the charging slot midpoint ±12h, so the charging slots are prominent and now is naturally visible nearby
- Four bar states: solid teal (scheduled + optimal), teal diagonal stripe (scheduled but not optimal), teal outline (optimal but not scheduled/missed), grey diagonal stripe (forecast display-only). Legend items are conditional — each only appears when that bar type is visible
- Hero shows `scheduled 3h (1h30m carried over)` when hours from the previous plan are carried forward
- "vs market" percentage shows how much cheaper the scheduled avg price is compared to the average across all slots available to the scheduler at run time
- Hover/touch any bar to see its price and time in the hero area; hovering a charging bar shows the window avg price
- Responsive tick intervals — 2h on wide screens, 4h on narrow (phone portrait)
- Forecast warning banner when `price_source` is `"forecast"`
- Staleness warning when plan is more than a day old
- Charging period derived from UTC window times (e.g. "charges Mon 23 → Tue 24 Mar")
- Weekly schedule grid per profile showing configured windows for each day

To enable: go to **Settings → Pages**, select **Deploy from a branch**, choose `main` and `/ (root)`. The site will be live at `https://<username>.github.io/<repo>/`.


---

## Running

### Locally

```bash
# Build plans
python charging_planner.py
python charging_planner.py --config my-config.yaml --output-dir /tmp/plans
python charging_planner.py --debug

# Deliver plans and send notification
python delivery/deliver.py plan-*.json --config config.yaml
python delivery/deliver.py plan-*.json --debug
```

### GitHub Actions

Place `schedule.yml` in `.github/workflows/`. Add the following repository secrets:

```
Settings → Secrets and variables → Actions → New repository secret
```

| Secret | Value |
|---|---|
| `ENTSOE_API_KEY` | Your ENTSO-E security token |
| `CHARGER_EMAIL` | Charge Amps login email |
| `CHARGER_PASSWORD` | Charge Amps login password |
| `CHARGER_ID_1` | First charger ID |
| `CHARGER_ID_2` | Second charger ID (if applicable) |

Never commit secrets to the repository. All sensitive values are injected at runtime as environment variables — `config.yaml` keeps only empty placeholders.

The workflow runs daily at 12:30 UTC — 14:30 Helsinki time in winter (EET, UTC+2) and 15:30 in summer (EEST, UTC+3). A single cron covers both DST states because 12:30 UTC always lands after ENTSO-E's ~12:00 UTC publication time.

Day-ahead prices are published at approximately 12:00 UTC each day. If ENTSO-E is unavailable or prices aren't published yet, the planner automatically tries Sähkötin, then the nordpool-predict-fi forecast. If all sources fail, a push notification is sent and the run exits with a non-zero code. Once prices are available the next scheduled run will succeed.

To trigger a run manually: **Actions → Charging Planner → Run workflow**.

Each successful run writes a formatted markdown summary to the GitHub Actions job view and commits the plan JSONs to `data/` in the repository so the GitHub Pages dashboard can fetch them without authentication.

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
  "retained_hours": 0.5,
  "total_minutes": 390,
  "avg_price_cents_kwh": 0.91,
  "avg_optimal_price_cents_kwh": 0.91,
  "preferred_window_start": "22:00",
  "preferred_window_end": "06:30",
  "windows": [
    {
      "start": "00:00",
      "end": "06:30",
      "duration_minutes": 390,
      "avg_price_cents_kwh": 0.91
    }
  ],
  "window_starts_utc": ["2026-03-14T22:00:00+00:00"],
  "window_ends_utc":   ["2026-03-15T04:30:00+00:00"],
  "price_slots": [
    { "start_utc": "2026-03-14T20:00:00+00:00", "price_cents_kwh": 0.82, "charging": false, "optimal": false },
    { "start_utc": "2026-03-14T22:00:00+00:00", "price_cents_kwh": 0.91, "charging": true, "optimal": true },
    ...
  ],
  "ocpp_charging_profile": { ... }
}
```

`price_source` is `"ENTSO-E"`, `"Sähkötin"`, or `"forecast"` depending on which source provided the prices for this plan.

`window_starts_utc` and `window_ends_utc` are UTC ISO 8601 timestamps for each charging window — use these to start and stop charging in downstream systems.

`price_slots` contains all available price slots from the previous evening onwards, each with `start_utc`, `price_cents_kwh`, `charging: true/false`, and `optimal: true/false`. The `optimal` flag marks the theoretically cheapest slots for the same required duration, respecting `continuous_only` and `min_slot_minutes` but ignoring any preferred window constraint. `avg_optimal_price_cents_kwh` is the average price across optimal slots. `price_stats` (min/avg/max) reflects the full range of slots available to the scheduler at run time — the basis for the "vs market" percentage shown on the dashboard.

`retained_hours` is the number of future charging hours carried forward from the previous plan. When non-zero, `total_minutes` will exceed `required_minutes` by the equivalent amount.

Slots with `"forecasted": true` are display-only — they extend the histogram beyond the last real price slot. They are never used for slot selection or optimal calculation.

### OCPP smart charging

Each plan includes an `ocpp_charging_profile` field containing a ready-to-use OCPP `ChargingProfile` object compatible with OCPP 1.6, 2.0.1, and 2.1. Charging windows run at `max_charging_rate`; gaps between windows are explicitly set to `limit: 0` so the charger does not charge outside the planned slots.

For a split plan with two windows separated by a gap, the schedule periods alternate between charging and zero:

```json
"chargingSchedulePeriod": [
  { "startPeriod":    0, "limit": 11000.0 },
  { "startPeriod": 5400, "limit": 0.0     },
  { "startPeriod": 7200, "limit": 11000.0 }
]
```

The profile is `TxDefaultProfile` (`Absolute` kind), meaning it applies automatically to any transaction started on the EVSE without needing a transaction ID in advance. OCPP 2.0.1 and 2.1 use `id` instead of `chargingProfileId` — adjust the field name when consuming the profile in your own delivery integration.

## Future work

### Plug & Schedule — SoC-derived required hours

Currently `required_hours` is a static value that must be set conservatively for the worst case (long drive, cold weather, nearly empty battery). This means the planner often schedules more charging than needed, which reduces its ability to find the cheapest windows.

**The goal** is to derive `required_hours` dynamically when the car is plugged in: read the current state of charge and the target SoC from the charger at plan-build time, then calculate the actual energy needed using the car's battery capacity and charging rate. A day with a short commute and 70% remaining SoC would produce a 1h plan; a day starting from 20% would produce a 5h plan. The planner finds the cheapest windows for the actual need — not a worst-case estimate.

The calculation would be approximately:

```
energy_needed_kwh = (target_soc - current_soc) * battery_capacity_kwh
charging_rate_kw  = min(max_charging_rate, car_max_charging_rate_kw)
required_hours    = (energy_needed_kwh / charging_rate_kw) * buffer_factor
```

The buffer covers charge rate tapering in the upper SoC range, cold-climate derating, and possible load balancing reductions.

`max_charging_rate` is already configured per delivery entry. The only additional static config values needed are `battery_capacity_kwh` and `car_max_charging_rate_kw` — both known per car and set once. The only truly dynamic runtime inputs are `current_soc` and `target_soc`, read from the charger at plan-build time.

**Trigger**: Charge Amps LUNA firmware update to OCPP 2.0.1 or 2.1. These versions provide reliable SoC measurand reporting in `MeterValues` and support the ISO 15118-20 SoC target negotiation between car and charger. OCPP 1.6 has a SoC measurand but its availability depends heavily on the charger and car combination.

`required_hours` will remain as a config fallback for when the car is not plugged in at plan-build time or SoC data is unavailable.

---
