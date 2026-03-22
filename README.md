# charging-planner

Fetches day-ahead electricity prices from the [ENTSO-E Transparency Platform](https://transparency.entsoe.eu) and schedules EV charging for the cheapest available hours — automatically, every day.

---

## Overview

The script makes a single API call to fetch all available day-ahead prices, then runs each configured charging profile against the price data independently. Each profile picks its own cheapest windows within its preferred time range and writes a plan to JSON. Once plans are built, `delivery/deliver.py` dispatches each plan to the configured chargers and sends a push notification with the plan summary and delivery status.

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
- [`websockets`](https://pypi.org/project/websockets/) — `pip install websockets` (only needed for the OCPP delivery handler)
- An ENTSO-E API key (free)
- [ntfy app](https://ntfy.sh) on iOS or Android (optional — for push notifications)

No other dependencies. The script uses only the standard library, including [`zoneinfo`](https://docs.python.org/3/library/zoneinfo.html) (stdlib since Python 3.9) for DST-correct timezone handling.

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
    preferred_window_start: "22:00"
    preferred_window_end: "06:30"
    schedule:
      - days: [sunday, monday, tuesday, wednesday, thursday]
        preferred_window_start: "22:00"
        preferred_window_end: "06:30"
      - days: [friday, saturday]
        preferred_window_start: "00:00"
        preferred_window_end: "23:45"
    deliveries:
      - handler: chargeamps
        charge_point_id: CHARGER_ID_1
        connector_id: 1
        max_charging_rate: 16.0
        restore_mode: false

  - name: overnight
    required_hours: 6
    continuous_only: true
    preferred_window_start: "21:00"
    preferred_window_end: "06:30"
    deliveries:
      - handler: chargeamps
        charge_point_id: CHARGER_ID_2
        connector_id: 1
        max_charging_rate: 16.0
        restore_mode: true

ntfy:
  enabled: true
  topic: ""
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
| `charging.min_slot_minutes` | `30` | Minimum continuous block length. Must be a multiple of 15 |
| `charging.max_price_cents_kwh` | `null` | Skip slots above this price (c€/kWh). `null` = no ceiling |
| `charging.preferred_window_start` | `00:00` | **Required.** Start of preferred charging window (`HH:MM`) |
| `charging.preferred_window_end` | `06:30` | **Required.** End of preferred charging window (`HH:MM`). If earlier in the day than `preferred_window_start` the window wraps midnight. Equal start and end is an error. Use `23:45` for end of day — `23:59` excludes the last 15-minute slot |
| `charging.schedule` | `[]` | Optional list of day-specific window overrides. Each entry has a `days` list (`monday`–`sunday`) and its own `preferred_window_start` / `preferred_window_end`. The first matching entry for the target day is used; falls back to top-level window if none match. Days not listed use the top-level window |

### Preferred window behaviour

**Which period is planned** — the planner always targets the next upcoming occurrence of the window:

| Time | Window | Plans |
|---|---|---|
| 14:30 | `20:30–23:45` | today (window not yet started) |
| 22:00 | `20:30–23:45` | tomorrow (window already started) |
| 14:30 | `00:00–23:45` | tomorrow (same-day window already passed) |
| 14:30 | `22:00–06:30` | tonight (overnight, window not yet started) |

For same-day windows (start before end, e.g. `00:00–23:45`) the planner targets tomorrow once today's window start has passed. This is the recommended pattern for weekend wide-open windows — run the script after ENTSO-E publishes prices (~12:00 UTC) and the cheapest slots from anywhere in tomorrow's day will be selected.

**Important — schedule entries shift by one day for same-day windows.** Because a same-day window that has already started targets tomorrow, the schedule entry must be assigned to the *previous* day. For example, to get a full-day window on Saturday and Sunday, assign `00:00–23:45` to `friday` and `saturday` — the Friday run plans Saturday, and the Saturday run plans Sunday. Overnight windows (`22:00–06:30`) do not shift — the entry day matches the night it starts on, so `sunday` covers Sunday evening into Monday morning.

For overnight windows (start after end, e.g. `22:00–06:30`) the target day is the day the window *starts* on. A schedule entry for `sunday` with `22:00–06:30` covers Sunday night into Monday morning.

**Slot selection** — the planner fills as many slots as possible from within the preferred window first, then spills leftward outside it only if needed to meet `required_hours`. Spillover never goes after `preferred_window_end`.

**Gap merging** — when two selected blocks are separated by a gap shorter than `min_slot_minutes`, they are bridged into one continuous window automatically. Slots are then trimmed from the merged block to bring the total back to `required_hours`.

**Guaranteed charge until departure time** — setting `required_hours` longer than the window with `continuous_only: true` ensures the block always ends exactly at `preferred_window_end`.

---

## Charger delivery

Delivery is handled by `delivery/deliver.py`, which reads the `deliveries:` block inside each charging profile and dispatches each plan to the correct handler. Two handlers are included:

| Handler | Script | Description |
|---|---|---|
| `chargeamps` | `delivery/deliver_chargeamps.py` | Delivers via the `my.charge.space` API |
| `ocpp` | `delivery/deliver_ocpp.py` | Delivers via OCPP WebSocket (`SetChargingProfile.req`), supports 1.6 / 2.0.1 / 2.1 |

The `chargeamps` handler supports one additional option: `restore_mode` (default `false`) — when `true`, reads the connector mode before delivery and restores it afterwards if it was not already `Schedule`. Useful if the charger is normally kept in `On` or `Off` mode and should return to that state after the schedule is pushed.

See [`delivery/README.md`](delivery/README.md) for full handler configuration reference and instructions for adding a new handler.

---

## Push notifications (ntfy)

After delivery completes, `delivery/deliver.py` sends a single push notification containing the plan summary and delivery status for each charger. The notification is sent to the configured ntfy topic.

Example notification:

```
Title: Charging plan 2026-03-21

topup 2h @ 0.62 c€/kWh ↓64%
03:30–05:30
  ✓ CHARGER-001

overnight 6h @ 1.93 c€/kWh ↑9%
22:00–06:00
  ✓ CHARGER-001
  ✗ CHARGER-002
```

The title contains the date. Each profile section shows the scheduled hours, average price, and how it compares to the market average (`↓` below, `↑` above). Chargers are listed indented under their profile. A delivery failure does not prevent the notification from being sent.

ntfy is configured in `config.yaml` under the `ntfy:` key. The topic is injected at runtime via the `NTFY_TOPIC` environment variable — never commit it to the repository.

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
| `NTFY_TOPIC` | Your ntfy topic name — only needed if `ntfy.enabled: true` |
| `CHARGER_EMAIL` | Charge Amps login email |
| `CHARGER_PASSWORD` | Charge Amps login password |
| `CHARGER_ID_1` | First charger ID |
| `CHARGER_ID_2` | Second charger ID (if applicable) |
| `OCPP_ENDPOINT_URL` | WebSocket base URL for OCPP charger (if applicable) |

Never commit secrets to the repository. All sensitive values are injected at runtime as environment variables — `config.yaml` keeps only empty placeholders.

The workflow runs daily at 12:30 UTC — 14:30 Helsinki time in winter (EET, UTC+2) and 15:30 in summer (EEST, UTC+3). A single cron covers both DST states because 12:30 UTC always lands after ENTSO-E's ~12:00 UTC publication time.

Day-ahead prices are published at approximately 12:00 UTC each day. If the script runs before publication, or ENTSO-E is delayed, the script detects this and exits cleanly — a push notification is sent to the configured ntfy topic so you know no plan was scheduled. Once prices are available the next scheduled run will succeed.

To trigger a run manually: **Actions → Charging Planner → Run workflow**.

Each successful run writes a formatted markdown summary to the GitHub Actions job view and uploads a run artifact containing all `plan-{name}.json` files.

---

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
      "avg_price_cents_kwh": 0.91
    }
  ],
  "window_starts_utc": ["2026-03-14T22:00:00+00:00"],
  "window_ends_utc":   ["2026-03-15T04:00:00+00:00"],
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
}
```

`window_starts_utc` and `window_ends_utc` are UTC ISO 8601 timestamps for each charging window — use these to start and stop charging in downstream systems.

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

The profile is `TxDefaultProfile` (`Absolute` kind), meaning it applies automatically to any transaction started on the EVSE without needing a transaction ID in advance. OCPP 2.0.1 and 2.1 use `id` instead of `chargingProfileId` — pass `ocpp_version` in the delivery config to get the correct field names.
