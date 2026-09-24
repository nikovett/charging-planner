# charging-planner

Fetches day-ahead electricity prices from the [ENTSO-E Transparency Platform](https://transparency.entsoe.eu) and schedules EV charging for the cheapest available hours — automatically, every day.

---

## What makes this different

**Build once, deliver once, charge unsupervised.** Day-ahead prices are published every afternoon for the following day — only information needed to plan the entire next charging cycle in one go. The result is a plan delivered once to whatever target is configured. No further monitoring, no on/off toggling, no always-on process needed.

**Zero extra hardware required.** Runs as a GitHub Actions cron job — no server, no hub, no Raspberry Pi. Day-ahead prices publish on a predictable schedule, making this a natural fit for a cloud cron. Local cron works too if preferred.

**Globally optimal scheduling.** `max_windows: 1` finds the cheapest unbroken block ending at departure time. `max_windows: null` (or a higher bound) uses dynamic programming to evaluate every valid combination of blocks across 96 price slots per day. The cheapest combination is rarely obvious to a human eye, and even harder to find manually as prices fluctuate every 15 minutes.

**The planner and the delivery target are fully separate.** The planner only ever produces a plan — a list of cheapest charging windows — with no knowledge of chargers or cars. Delivery is a separate, pluggable step: a handler can be as simple as pushing a schedule, or as sophisticated as understanding its target's own state — reading connector status, detecting an active charging session and working around it, restoring a prior mode afterwards. New targets mean a new handler, never a change to the planner.

**Fits any setup, any schedule.** A 3.7 kW charger needs long overnight charging window; a 22 kW charger benefits from hunting the cheapest short charging windows wherever they fall. Run multiple profiles simultaneously — weekday topup, weekend overnight, each with its own duration, window, mode, and delivery target — all from one config file. Both the preferred charging window and required hours can be configured per day of the week within each profile.

**Realistic charging behaviour built in.** Independent minimum charging length and minimum gap between charging blocks prevent short on/off cycling — by default the planner won't schedule 15 minutes on, 15 minutes off, on again. The minimum block length and gap are both configurable; the gap can be set to zero if no pause between blocks is needed.

**Previously committed charging is never lost.** If a new plan is built before all charging slots from the previous plan have taken place, the remaining uncommitted charging time is added on top of the new plan's requirement. The scheduler then finds the cheapest slots for the full combined need — so nothing already committed to a delivery target is lost.

**Area-aware price source fallback.** The fallback chain is built from the configured area: FI uses ENTSO-E → Elering → Sähkötin → forecast; EE/LV/LT use ENTSO-E → Elering; SE1–SE4 use ENTSO-E → elprisetjustnu.se; NO1–NO5 use ENTSO-E → hvakosterstrommen.no. Sources outside the area's chain are never tried. The dashboard warns when the plan is based on forecast rather than confirmed prices.


```
  ══════════════════════════════════════════════════════════════════
  Charging Planner
  ══════════════════════════════════════════════════════════════════
  Date      2026-03-15   Area FI   Source ENTSO-E
  Timezone  Europe/Helsinki (UTC+2)

  Market prices   0.47 min  1.64 avg  4.27 max  c€/kWh

  Scheduled  4h
  Avg price  0.62 c€/kWh
             vs market -1.02 c€/kWh

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

All configuration lives in a single `config.yaml` file. Deliveries are configured inside each charging profile, so the relationship between a plan and its delivery targets is explicit and co-located.

### Multiple profiles with deliveries

```yaml
area: FI
timezone: Europe/Helsinki       # ENTSOE_API_KEY supplies the secret at runtime — never commit a key

profiles:
  - name: topup
    schedule:
      mon-fri: { window: 22:00-06:30, required: 1.5 }
      sat-sun: { window: any-any,     required: 4.5 }   # any-any = no window constraint, cheapest slots from run time onward
    delivery:
      - chargeamps: { charger: CHARGER_ID_1, connector: 1, max_amps: 16.0 }

  - name: overnight
    schedule:
      mon-sun: { window: 21:00-06:30, required: 4.5 }
    max_windows: 1
    delivery:
      - chargeamps: { charger: CHARGER_ID_2, restore_mode: true }

```

`delivery:` entries use each handler's own field names — `charger`/`vin` for the charge point ID, plus whatever else that handler accepts (see "Charging profile reference" for the full per-key list, and `delivery/README.md` for every handler's options and defaults). `charger`/`vin` accepts either a single env var name or a list — when a list is given, the same plan is delivered to every charger independently. Every key other than `schedule:` is optional; a value only needs stating when it differs from its default. `mon-sun` covers all seven days in one entry — day keys accept a single day (`fri`), a forward range (`mon-fri`), a comma list (`sat,sun`), or a mix (`mon-wed,fri`); every day of the week must be covered by exactly one entry. Timezone is set once at the top level and applies to every profile and delivery handler.

### Charging profile reference

| Key | Default | Description |
|---|---|---|
| `area` | — | **Required.** Bidding zone short code or full EIC (e.g. `FI`, `10YFI-1--------U`) |
| `timezone` | — | **Required.** IANA timezone name (e.g. `Europe/Helsinki`). Applies to every profile and delivery handler |
| `profiles[].name` | — | **Required.** Profile name — used in the output filename (`plan-{name}.json`) |
| `profiles[].schedule` | — | **Required.** A mapping of day-group keys to `{ window, required }`. Day keys: a single day (`fri`), a forward range (`mon-fri`), a comma list (`sat,sun`), or a mix (`mon-wed,fri`) — every day of the week must be covered by exactly one entry. `window` is `HH:MM-HH:MM`, `any-any` for no constraint at all, or a mix (`any-06:30`, `21:00-any`) for an open start or open end — see "Preferred window behaviour" below. `required` is hours of charging to schedule that day |
| `profiles[].max_windows` | `null` (unlimited) | `null` = unlimited blocks (cheapest individual slots, DP-optimal, may be split across the window); `1` = one unbroken block; `N` (≥2) = at most N separate blocks, DP-optimal within that budget. A handler may reject a plan with more blocks than it can accept — see the handler's own reference in `delivery/README.md` |
| `profiles[].min_slot_minutes` | `30` | Minimum continuous block length. The charger should not run for less than this duration. Must be 15 minutes or more and a multiple of 15 (the price slot resolution) |
| `profiles[].min_gap_minutes` | `15` | Minimum gap between charging blocks. Prevents the charger toggling off and straight back on. Must be a multiple of 15. `0` = no gap constraint. Can be set independently of `min_slot_minutes` — e.g. `min_slot_minutes: 120` with `min_gap_minutes: 15` gives 2h blocks with 15-minute gaps |
| `profiles[].price_limit` | `none` | Skip slots above this price. `none` = no ceiling. `avg` = dynamic daily market average — ceiling is set to today's average price at plan time. A number = a fixed ceiling in c€/kWh |
| `profiles[].delivery` | — | Optional list of delivery targets. Omit entirely to plan for the dashboard only, without sending anywhere. Each entry is `{ handler_name: { ...handler-specific keys... } }` — see `delivery/README.md` for every handler's keys and defaults |

### Preferred window behaviour

**A window's end is the departure deadline; its start is when the car is expected to be home and plugged in.** The gap between them is usually looser than `required` needs — that slack is what `min_slot_minutes`/`min_gap_minutes`/`max_windows` optimize within, not time the car is expected to be actively charging throughout. A schedule entry is indexed by the day it gets the car ready *for*, not by the date its window starts on — the `mon-fri` entry's window for, say, Wednesday actually starts Tuesday evening.

A window where start > end (e.g. `21:00-06:30`) wraps midnight — it starts the evening before the target day and ends the morning of the target day. A window where start < end (e.g. `00:00-23:45`) stays within the target day. `any-any` means no constraint at all — the planner picks the cheapest slots from all available prices from the run time onward. One side alone can be `any` too: `any-06:30` charges anytime as long as it's done by a fixed deadline (useful when you don't care when charging starts, only that it finishes in time), and `21:00-any` starts at a fixed time with no fixed end (useful when you want charging to begin promptly but are fine with it running as long as needed within the available prices).

**Catching a delayed run.** A normal run plans tomorrow's window (day-ahead prices are published for the next day). But if a run fires late enough that tonight's overnight window has already opened, it still targets that live window rather than skipping ahead to the next occurrence — using whatever time remains from the run onward, never discarding hours of a still-usable window. This only applies to overnight (midnight-wrapping) windows; a same-day window that's already passed by the time a delayed run fires is simply gone for that day, same as it always was.

**Slot selection** — for `max_windows: 1`, the planner evaluates all possible contiguous blocks of `required` length and picks the cheapest. For `max_windows: null` (unlimited) or `max_windows: N` (N ≥ 2), it uses dynamic programming to find the globally cheapest combination of blocks that together cover exactly `required` — unlimited or capped at N separate blocks respectively — where every block is ≥ `min_slot_minutes` and every gap between blocks is ≥ `min_gap_minutes`. Adjacent selected blocks are merged into a single charging window automatically. If the full `required` amount can't be reached at all (too little time remains, or the window is too fragmented), the planner uses everything it can rather than producing nothing, and reports the shortfall.

**Per-day required hours** — each schedule entry states its own `required`, so a single profile can act as a true per-car profile: 1.5h on weekdays for a short commute, 4.5h on weekends for a longer charge.

**Preferred window and spillover** — slots within the configured window are the primary candidates. If the window doesn't contain enough slots to satisfy `required` (too few slots, or all above `price_limit`), the planner adds the cheapest available slots from *before* the window to cover the deficit — never past the window's end, and never before the actual run time, so a delayed run's spillover can't reach into time that's already elapsed. When the window is `any`, all available slots from the run time onward are candidates from the start.

**Dynamic price ceiling** — `price_limit: avg` uses today's market average as the ceiling, resolved at plan time from the available price data. This avoids hardcoding a number that may become stale as market conditions change. A partial plan is still possible if all slots in the window happen to be above the average, but this is uncommon in practice.

**Guaranteed charge until departure time** — setting `required` longer than the window with `max_windows: 1` ensures the block always ends exactly at the window's end. Not applicable when using `any`.

---

## Fallback price sources

The fallback chain is assembled at startup from the configured area — sources outside an area's chain are never called:

| Area | Chain |
|---|---|
| `FI` | ENTSO-E → Elering → Sähkötin → forecast |
| `EE`, `LV`, `LT` | ENTSO-E → Elering |
| `SE1`–`SE4` | ENTSO-E → elprisetjustnu.se |
| `NO1`–`NO5` | ENTSO-E → hvakosterstrommen.no |
| other | ENTSO-E only |

The fallback triggers in two cases: network/HTTP errors, and when the returned prices don't extend into tomorrow (e.g. during scheduled maintenance where ENTSO-E returns a valid but stale response with only today's data).

All sources use EUR/kWh ex-VAT. ENTSO-E returns EUR/MWh for all areas including SE and NO; the regional sources also provide EUR directly (SEK and NOK fields in the response are unused).

1. **Elering** (`dashboard.elering.ee/api`) — actual Nord Pool 15-min prices for Finland, Estonia, Latvia and Lithuania. No API key needed. Used transparently — `price_source: "Elering"` in the plan JSON, no dashboard warning.

2. **Sähkötin** (`sahkotin.fi/api`) — actual realized Nord Pool 15-min prices, Finland only. No API key needed. Used transparently — `price_source: "Sähkötin"` in the plan JSON, no dashboard warning.

3. **nordpool-predict-fi** (`raw.githubusercontent.com/vividfog/nordpool-predict-fi`) — ML forecast blended with realized prices. Finland only. By ~14:00–16:00 Helsinki time the forecast transitions to actual market prices, making it nearly as reliable as ENTSO-E once Nord Pool has published. Plans from this source are tagged `price_source: "forecast"` and display a warning banner on the dashboard.

4. **elprisetjustnu.se** — Sweden (SE1–SE4). Native 15-min resolution. No API key needed. Used transparently — `price_source: "nordpool-regional"` in the plan JSON, no dashboard warning.

5. **hvakosterstrommen.no** — Norway (NO1–NO5). Hourly resolution, expanded to 15-min. No API key needed. Used transparently — `price_source: "nordpool-regional"` in the plan JSON, no dashboard warning.

If all sources in the area's chain fail, the script exits with a non-zero code so the GHA run is marked as failed and the operator receives an email.

## Histogram display augmentation

Both ENTSO-E and Sähkötin return price slots from the previous evening onwards, including historical prices. This gives the dashboard histogram enough data to the left of "now" for context.

After every successful real-price fetch, the planner always fetches up to 24 hours of forecast data from nordpool-predict-fi beyond the last real price slot (Finland only — other areas skip this step). These slots are primarily **display-only** — they appear as grey diagonal-striped bars in the histogram. They are also used for slot selection when real prices don't fully cover the charging window (see fallback price sources), in which case `price_source` is set to `"forecast"`.

## Dashboard

A GitHub Pages dashboard is included at `index.html`. It fetches the latest plan JSONs and `config.json` from `data/` directly from the repository — no token or backend needed.

Features:

- One flip card per profile — front shows the plan, back shows profile configuration and weekly schedule
- Price histogram with all available slots positioned by actual timestamp. The histogram window uses three modes as the day progresses:
  - **Charging far away (>11h)**: the window starts 1 hour before now and extends to cover all charging slots — an overview of where in the future the charging slots are
  - **Charging approaching (midpoint within 11h, not yet reached)**: the window centers on the charging slot midpoint ±12h, so the charging slots become the centerpiece
  - **During and after charging (now past midpoint)**: the window centers on now ±12h and tracks forward in real time, revealing forecast bars to the right as time progresses
- Four bar states: solid teal (scheduled + optimal), teal diagonal stripe (scheduled but not optimal), teal outline (optimal but not scheduled/missed), grey diagonal stripe (forecast display-only). Legend items are conditional — each only appears when that bar type is visible. Negative-price slots render as downward-growing bars from the zero baseline using the same colour coding; a faint zero baseline line appears when negative prices are present
- Hero shows `scheduled 3h (1h30m carried over)` when hours from the previous plan are carried forward, and `vs optimal +0.18 c€/kWh` when the window constraint forces suboptimal slots
- "vs market" shows the absolute difference between scheduled avg price and market avg (e.g. `-1.02 c€/kWh` = cheaper than market, `+0.50 c€/kWh` = more expensive). Same sign convention as "vs optimal"
- Hover/touch any bar to see its price and time in the hero area; hovering a charging bar shows the window avg price
- Price axis on the right side of the histogram with 3–5 evenly-spaced tick marks. The scale is based on the maximum price visible in the current histogram window with a floor of 6 c€/kWh — off-screen spikes don't compress visible bars, and cheap days stay visually flat relative to a normal day rather than zooming in. The zero tick tracks the zero baseline and moves up when negative prices are present
- Responsive time tick intervals — 2h on wide screens, 4h on narrow (phone portrait)
- Forecast warning banner when `price_source` is `"forecast"`
- Staleness warning when plan is more than a day old
- Charging period derived from UTC window times (e.g. "charges Mon 23 → Tue 24 Mar")
- Weekly schedule grid per profile showing configured windows and required hours for each day

To enable: go to **Settings → Pages**, select **Deploy from a branch**, choose `main` and `/ (root)`. The site will be live at `https://<username>.github.io/<repo>/`.


---

## Delivery

`delivery/deliver.py` reads the `delivery:` block inside each charging profile and dispatches the plan to the right handler. Three are included out of the box — `chargeamps` (tested), `easee` (untested), `myskoda` (tested, delivers to the vehicle instead of a charger) — and adding a new one requires no changes to the planner or the dispatcher.

See [`delivery/README.md`](delivery/README.md) for handler config keys, environment variables, and per-handler behaviour.

---

## Running

### Locally

```bash
# Build plans
python charging_planner.py
python charging_planner.py --config my-config.yaml --output-dir /tmp/plans
python charging_planner.py --debug

# Deliver plans
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
| `CHARGER_USERNAME` | Charge Amps or Easee login username (email or phone number) |
| `CHARGER_PASSWORD` | Charge Amps or Easee login password |
| `CHARGER_ID_1` | First charger ID |
| `CHARGER_ID_2` | Second charger ID (if applicable) |
| `SKODA_VIN` | Vehicle VIN — only needed for the `myskoda` handler |
| `SKODA_API_KEY` | MyŠkoda app API key — only needed for the `myskoda` handler |

Which secrets you actually need depends on which handlers are configured — see Delivery above.

Never commit secrets to the repository. All sensitive values are injected at runtime as environment variables — `config.yaml` keeps only empty placeholders.

The workflow runs daily at 12:27 UTC (15:27 EEST / 14:27 EET). GHA typically delays 30–60 minutes, landing around 16:00 local time in summer. Prices publish at ~11:00 UTC so the run lands well after publication even without any GHA delay.

If ENTSO-E is unavailable or prices aren't published yet, the planner automatically tries Elering, then Sähkötin, then the nordpool-predict-fi forecast. If all sources fail, the run exits with a non-zero code — the GHA job is marked as failed and the operator receives an email. Once prices are available the next scheduled run will succeed.

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
  "retained_minutes": 30,
  "max_windows": 1,
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
  "plan_warning": null,
  "ocpp_charging_profile": { ... }
}
```

`price_source` is `"ENTSO-E"`, `"Sähkötin"`, or `"forecast"` depending on which source provided the prices for this plan.

`window_starts_utc` and `window_ends_utc` are UTC ISO 8601 timestamps for each charging window — use these to start and stop charging in downstream systems.

`price_slots` contains all available price slots from the previous evening onwards, each with `start_utc`, `price_cents_kwh`, `charging: true/false`, and `optimal: true/false`. The `optimal` flag marks the theoretically cheapest slots for the same required duration, respecting `max_windows` and `min_slot_minutes` but ignoring any preferred window constraint. `avg_optimal_price_cents_kwh` is the average price across optimal slots. `price_stats` (min/avg/max) reflects the full range of slots available to the scheduler at run time — the basis for the "vs market" difference shown on the dashboard.

`retained_minutes` is the number of future charging minutes carried forward from the previous plan. When non-zero, `total_minutes` will exceed `required_minutes` by the same amount.

`plan_warning` is `null` when the plan is complete. When `total_minutes < required_minutes` it contains a human-readable reason: `"partial plan — price limit X c€/kWh"` or `"partial plan — required hours exceed boundaries"`. See Plan comparisons for how the cause is determined. The dashboard shows the reason in amber next to the scheduled hours.

Slots with `"forecasted": true` are display-only — they extend the histogram beyond the last real price slot. They are never used for slot selection or optimal calculation.

### Plan comparisons

Every time a plan is built, the planner runs two background comparisons.

**Optimal comparison** — finds the cheapest possible slots ignoring the preferred window constraint, with all other settings (price ceiling, `max_windows`, `min_slot_minutes`, `min_gap_minutes`) kept intact. The result is `avg_optimal_price_cents_kwh` and the `optimal` flag on each slot in `price_slots`. The dashboard shows `vs optimal +X.XX c€/kWh` when the window constraint forced suboptimal choices — if scheduled and optimal are the same, the window contained the cheapest slots anyway and no comparison is shown.

**Price ceiling comparison** — only runs when the plan is partial (`total_minutes < required_minutes`) and `max_price_cents_kwh` is set. Runs the same plan without the price ceiling. If it succeeds, the ceiling was the limiting factor → `plan_warning: "partial plan — price limit X c€/kWh"` (or `"partial plan — price limit avg (X c€/kWh)"` when using the dynamic ceiling). If it also fails, the shortage is due to insufficient slots in the window → `plan_warning: "partial plan — required hours exceed boundaries"`.

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

---
