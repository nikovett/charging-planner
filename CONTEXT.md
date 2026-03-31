# charging-planner — Project Context

This document captures the full development history, decisions, and current state of the charging-planner project across all development sessions.

---

## What it does

Fetches day-ahead electricity prices and schedules EV charging for the cheapest available hours, automatically, every day. Delivers the schedule to one or more chargers via the Charge Amps API. Publishes a GitHub Pages dashboard showing the current plan.

---

## Sessions

### Session 1 — 2026-03-19
Initial architecture. Core Python script, ENTSO-E price fetching, basic window selection, Charge Amps delivery handler, ntfy notifications, GitHub Actions workflow, OCPP delivery handler.

### Session 2 — 2026-03-24
Config restructuring, test suite, GitHub Pages dashboard (flip cards, price histogram, stats row), delivery handler improvements.

### Session 3 — 2026-03-26 (morning)
Bug fixes, algorithm improvements, histogram visualization, optimal slot comparison, fallback price source integration (nordpool-predict-fi).

### Session 4 — 2026-03-26 (afternoon)
DP slot selection algorithm, gap constraints, Sähkötin fallback, forecast display augmentation, histogram redesign, dashboard theme exploration.

### Session 5 — 2026-03-27 (morning)
Full dashboard redesign (hero price theme), light/dark theming, bar hover interaction, touch support, ntfy refactoring (failure-only channel), gap merge removal, algorithm correctness fixes.

### Session 6 — 2026-03-27 (afternoon)
Histogram fix: ENTSO-E now returns historical slots like Sähkötin, so histogram can center on charging slot midpoint. ENTSO-E fallback fix: raises PricesNotYetAvailable when slots don't reach tomorrow. Dashboard legend: all items now conditional on visible slots; "scheduled" renamed to "optimal"; added "suboptimal" entry for charging-but-not-optimal slots. Two-mode histogram window introduced. Forecast augmentation always runs after real-price fetch, capped at 12h beyond the last real slot.

### Session 7 — 2026-03-28
DST transition day. PlanParams refactor: renamed `all_prices` → `display_prices`, added `future_prices` (real slots from now onwards). Three clearly named price pools prevent historical/forecast data leaking into calculations. price_stats and optimal calculation fixed to use `future_prices` only. min_gap_minutes introduced as separate config parameter. Config pills redesigned. avg line z-order fixed.

### Session 8 — 2026-03-28 (afternoon)
**retained_minutes**: each run reads the previous plan JSON, counts future `charging: true` minutes, adds them to `required_hours` before DP. Dashboard hero shows carried-over time. End-to-end tests use `tempfile.TemporaryDirectory` to prevent leaking between runs.

**ntfy removed**: GHA is the primary run environment. Delivery failures exit non-zero → GHA emails. Forecast warning visible on dashboard. `_send_delivery_ntfy` removed from `deliver.py`. `NTFY_TOPIC` removed from `schedule.yml`. `ntfy:` block removed from `config.yaml`. `test_deliver.py` removed entirely — test count drops from 186 to 156.

**OCPP delivery handler removed**: `deliver_ocpp.py` removed. Requires direct WebSocket access — incompatible with GHA-first architecture. OCPP ChargingProfile remains in plan JSON output.

**min_gap_minutes**: separated from `min_slot_minutes` — block length and gap constraints now independent.

**Responsive histogram ticks**: 4h intervals on screens <520px, 2h on wider screens.

### Session 9 — 2026-03-29
**Per-day required_hours**: `required_hours` can be overridden per day within `schedule` entries. `_resolve_schedule_window` returns a third value — `required_minutes_override`. Weekly schedule grid on back card gains a "required" row showing per-day override or top-level fallback.

### Session 10 — 2026-03-29 (evening)
**retained_minutes compounding fix**: the invariant — `total_minutes` in the new plan should never exceed `total_minutes` from the previous plan when all slots are still future. Final formula: `min(future_minutes, prev_retained_minutes)`. Three-level fallback in `_load_retained_minutes`: `retained_minutes` → `retained_hours` (transitional) → `required_minutes` (pre-feature).

**optimal calculation fix**: `optimal_required = p.required_minutes + p.retained_minutes` so optimal covers the same total as scheduled.

### Session 11 — 2026-03-30
**retained_minutes rename**: `retained_hours` in JSON output renamed to `retained_minutes` — consistent with `required_minutes` and `total_minutes`. Integer minutes, no conversion. `index.html` updated to read `retained_minutes` directly.

**vs optimal in hero**: when a plan has any suboptimal charging slots, hero shows `vs optimal ↑N%` alongside `vs market`. Uses `avg_optimal_price_cents_kwh` from JSON. Condition mirrors the suboptimal legend — only shown when suboptimal slots actually exist. Optimal uses the same mode and constraints as scheduled (continuous_only respected) so the percentage purely reflects the cost of the window constraint.

**cron timing**: GHA consistently fires ~1h after the scheduled UTC time. After DST to EEST (UTC+3), cron set to `30 10 * * *`. Confirmed firing at ~14:30 EEST on both 2026-03-30 and 2026-03-31 — settled. Safe: even without GHA delay, 10:30 UTC lands before ENTSO-E publication at ~11:00 UTC and the fallback chain handles it.

### Session 13 — 2026-04-01

**Partial window coverage fix**: previously if real prices covered less than 90% of the charging window (e.g. ENTSO-E under maintenance, Sähkötin fallback but prices not yet published), the profile was skipped and no plan was delivered that day. Now the planner supplements the candidate prices with forecast slots to fill the window gap and builds a plan anyway. `price_source` is set to `"forecast"` when forecast slots were used for selection, triggering the dashboard warning. `fetch_forecast_display_slots` cap extended from 12h to 24h to ensure enough forecast coverage for a full overnight window. `_check_window_coverage` now returns `bool` instead of raising — `False` triggers supplementation rather than skipping.

### Session 12 — 2026-03-31

**Histogram Mode 3**: when now reaches or passes the charging midpoint, the window switches from charge-centered to now-centered (now-12h → now+12h). The window then tracks now in real time, revealing forecast bars to the right as time progresses. Also applied to the no-charging-slots fallback — now-centered ±12h makes more sense than the old now-1h → now+23h since there's nothing to anchor on.

**cron confirmed**: `30 10 * * *` fires consistently at ~14:30 EEST. Settled.

**forecast augmentation cap**: confirmed 12h at this point. Extended to 24h in Session 13 to cover full overnight windows when supplementing with forecast.

---

## Charge Amps integration

The official Charge Amps external API does not support scheduling. `deliver_chargeamps.py` uses the same API as the Charge Amps web portal, authenticating with the user's own credentials and delivering the charging schedule on their behalf.

---

## Architecture

```
charging_planner.py          # Core planner — price fetch, slot selection, plan building
delivery/
  deliver.py                 # Dispatcher — reads config, calls handlers
  deliver_chargeamps.py      # Charge Amps API handler
index.html                   # GitHub Pages dashboard
config.yaml                  # Configuration (committed with empty secrets)
.github/workflows/
  schedule.yml               # Daily GHA workflow
```

---

## Price sources (three-level fallback)

1. **ENTSO-E** — primary. Day-ahead 15-min prices. Retries 5×, backoff 5s. Raises `PricesNotYetAvailable` if slots don't reach tomorrow (catches partial/stale responses e.g. during maintenance).
2. **Sähkötin** (`sahkotin.fi/api`) — actual Nord Pool 15-min prices, FI only, no API key. Used transparently.
3. **nordpool-predict-fi** — ML forecast blended with realized prices. FI only. Tagged `price_source: "forecast"` in plan JSON; triggers dashboard warning.

Both ENTSO-E and Sähkötin return **all slots including historical** (from the previous evening). Past slots are used by the dashboard histogram; the scheduler ignores them as it filters by window start. After every successful real-price fetch, up to 24h of forecast slots are fetched beyond the last real slot. Primarily display-only (grey diagonal bars in the histogram), but also used for selection when real prices don't fully cover the charging window — in that case `price_source` is set to `"forecast"` and the dashboard warning is shown.

---

## Slot selection algorithm

### `continuous_only: true`
Slides a window of `required_hours` over all candidates, picks the cheapest contiguous block.

### `continuous_only: false`
Dynamic programming: finds the globally cheapest combination of blocks covering exactly `required_hours`.

**DP formulation:**
- State: `dp[i][r]` = minimum cost to schedule `r` more slots from position `i` onwards
- Transitions: skip slot `i`, or start a block of length `k ≥ min_slots_per_block`
- Gap constraint: next block can't start within `min_gap_slots` slots of previous block end
- Reconstruction: forward scan finding the **latest** valid block at each step (latest-preferred tiebreaker for equal-price ties)
- Globally optimal — no greedy approximation

**`min_slot_minutes`** controls the minimum individual block length.

**`min_gap_minutes`** controls the minimum gap between blocks (default 15, divisible by 15, can be 0). Kept separate from `min_slot_minutes` so e.g. `min_slot_minutes: 120` with `min_gap_minutes: 15` gives 2h blocks with 15-minute gaps rather than forcing 2h gaps.

**Spillover:** When the preferred window doesn't have enough slots, the planner fills the deficit from outside the window (never past `preferred_window_end`).

---

## Retained minutes

Each run reads `data/plan-{name}.json`, counts future `charging: true` minutes, and adds them to `required_hours` before running the DP. This ensures committed charging is never lost if a new plan is built before the previous one completes.

**Invariant:** `total_minutes` in the new plan should never exceed `total_minutes` from the previous plan when all slots are still future (script runs multiple times before charging starts).

**Formula:** `min(future_minutes, prev_retained_minutes)` — caps carry-over at the previously retained amount, never grows it.

**`_load_retained_minutes`:** `min(future_minutes, prev.get("retained_minutes", 0))` — simple and direct since only this script writes to `data/` and it always writes `retained_minutes`.

`retained_minutes` is always written to new JSONs (0 when nothing retained). Optimal calculation uses `required_minutes + retained_minutes` so the optimal set covers the same total as scheduled.

---

## Plan JSON structure

```json
{
  "version": 1,
  "date": "2026-03-30",
  "area": "FI",
  "price_source": "ENTSO-E",
  "timezone": "Europe/Helsinki",
  "utc_offset_hours": 3,
  "price_stats": { "min_cents_kwh": 0.45, "avg_cents_kwh": 1.92, "max_cents_kwh": 4.99 },
  "required_minutes": 270,
  "retained_minutes": 0,
  "total_minutes": 270,
  "avg_price_cents_kwh": 0.70,
  "avg_optimal_price_cents_kwh": 0.56,
  "preferred_window_start": "21:00",
  "preferred_window_end": "06:30",
  "windows": [
    { "start": "22:45", "end": "06:30", "duration_minutes": 270, "avg_price_cents_kwh": 0.70 }
  ],
  "window_starts_utc": ["2026-03-29T19:45:00+00:00"],
  "window_ends_utc":   ["2026-03-30T03:30:00+00:00"],
  "price_slots": [
    { "start_utc": "...", "price_cents_kwh": 0.45, "charging": true, "optimal": true },
    { "start_utc": "...", "price_cents_kwh": 2.05, "charging": false, "optimal": false, "forecasted": true }
  ],
  "ocpp_charging_profile": { ... }
}
```

Key fields:
- `required_minutes` + `retained_minutes` = `total_minutes`
- `price_slots[].charging`: true if scheduled
- `price_slots[].optimal`: true if in the globally cheapest solution (ignoring window constraint)
- `price_slots[].forecasted`: display-only, never charging/optimal
- `avg_optimal_price_cents_kwh`: average price of optimal slots — used by dashboard for `vs optimal` display

---

## Dashboard (index.html)

GitHub Pages dashboard. Fetches plan JSONs and config.yaml directly from the repository — no backend.

### Theme
Light/dark with OS preference default and manual toggle persisted in `localStorage`.

**Light:** warm cream background (`#F7F4EF`), white cards, teal accent.
**Dark:** near-black background (`#0D0D0D`), dark cards, same teal accent.

### Front card layout

**Card header:** profile name + charge period ("charges Mon, Mar 30 → Tue, Mar 31")

**Heroes (two side by side):**
- Left: `avg scheduled` — large teal price, `scheduled Xh (Ym carried over)` and `vs market ↓XX%` and `vs optimal ↑N%` (suboptimal only) underneath
- Right: `now` — large white/dark price, `min / avg / max` underneath in teal/white/amber

`vs optimal` only shown when there are suboptimal charging slots in the plan — mirrors the suboptimal legend item.

**Histogram (single row):**
- Solid teal bars = optimal scheduled slots (`charging && optimal`)
- Teal diagonal stripe bars = suboptimal scheduled slots (`charging && !optimal`)
- Teal outline bars = missed optimal slots (`!charging && optimal`)
- Grey diagonal stripe bars = forecast augmentation slots (display only)
- Playhead triangle above current bar = now position
- Dashed avg line behind bars

**Legend items** are all conditional — each only appears when that bar type is visible. Labels: optimal, suboptimal, missed, forecast.

**Histogram window:** Three-mode logic, snapped to 15-minute boundaries, telling a natural story as the day unfolds:
- **Mode 1** (charging midpoint > 11h away): rangeStart = now-1h, rangeEnd = max(now+23h, last charging slot+1h). Overall view — shows where in the future the charging slots are.
- **Mode 2** (charging midpoint ≤ 11h away, now before midpoint): rangeStart = mid-12h, rangeEnd = mid+12h. View compresses toward 24h with charging slots as the centerpiece.
- **Mode 3** (now ≥ charging midpoint, or no charging slots): rangeStart = now-12h, rangeEnd = now+12h. Once charging becomes "a thing in the past" (midpoint reached), now becomes the centerpiece — the histogram acts as a 12h price view into the future, with forecast bars sliding into view to the right as time progresses.

**Responsive ticks:** 4h intervals on screens <520px, 2h on wider screens.

**Hover/touch interaction:**
- Hovering any bar → right hero shows that slot's price and time
- Hovering a charging bar → left hero shows the window's avg price and time range
- Touch drag works via `touchmove` + `elementFromPoint`
- `stopPropagation` prevents bar touches from flipping the card

**Window pills:** time range, avg price, duration

### Back card layout
- Config grid (4 cols desktop, 2 mobile): required, mode, min slot, ceiling, delivery — teal pills for active values, grey for absent/off
- Weekly schedule grid: days × window start/end + required hours row (per-day override or top-level fallback); today highlighted in teal

### Warnings
- Staleness: plan data more than 1 day old
- Forecast: `price_source === "forecast"` — "prices sourced from forecast — real-time data was unavailable"

---

## Key design decisions

### DP replaces greedy slot selection
The greedy algorithm made locally optimal choices that prevented globally optimal combinations. The DP guarantees the cheapest valid solution. Reconstruction uses a latest-preferred tiebreaker for equal-price ties.

### Gap merge removed
`close_gap_merge` would bridge short gaps by including expensive gap slots, overriding price optimisation. The DP enforces gap constraints at selection time.

### `min_slot_minutes` and `min_gap_minutes` are separate constraints
Previously both used the same value, forcing unnecessarily long gaps when `min_slot_minutes` was set to e.g. 2h.

### Forecast augmentation is display-only (unless it's the only source)
When real prices are available, forecast slots are never passed to the selection algorithm or optimal calculation. When both real sources fail, forecast IS used for selection — but no display augmentation is fetched on top.

### PlanParams price pool separation
Three explicitly named pools prevent historical/forecast data leaking into calculations:
- `display_prices` — all real slots including historical. Used only for `price_slots` JSON output.
- `future_prices` — real slots from now onwards. Used for `price_stats`, optimal calculation, scheduler.
- `forecast_slots` — predicted slots. Appended to `price_slots`, never used in calculations.

### Retained minutes never compound
`min(future_minutes, prev_retained_minutes)` ensures the retained portion never grows when the script runs multiple times before charging starts.

### ntfy removed — GHA is the primary run environment
ntfy was originally used to notify on delivery failures, forecast prices, and skipped profiles. Removed because:
- Forecast warning is already visible on the dashboard
- Delivery failures exit non-zero → GHA emails the operator
- GHA is the primary run environment; local cron is secondary

### OCPP delivery handler removed
Requires direct WebSocket access to the charger — fundamentally incompatible with GHA-first architecture. The OCPP `ChargingProfile` object remains in the plan JSON output for any downstream system that wants it.

### Charge Amps uses web portal API
The official external API doesn't support scheduling. `deliver_chargeamps.py` authenticates with the user's own credentials and delivers schedules the same way the web portal does.

---

## Configuration reference

```yaml
entsoe:
  api_key: ""          # Injected via ENTSOE_API_KEY secret
  area: FI
  timezone: Europe/Helsinki

charging:
  - name: topup
    required_hours: 1.5
    continuous_only: false
    min_slot_minutes: 30
    min_gap_minutes: 15
    max_price_cents_kwh: null
    preferred_window_start: "21:00"
    preferred_window_end: "06:30"
    schedule:
      - days: [monday, tuesday, wednesday, thursday, friday]
        preferred_window_start: "21:00"
        preferred_window_end: "06:30"
        required_hours: 1.5
      - days: [saturday, sunday]
        preferred_window_start: any
        preferred_window_end: any
        required_hours: 4.5
    deliveries:
      - handler: chargeamps
        charge_point_id: CHARGER_ID_1
        connector_id: 1
        max_charging_rate: 16.0
        restore_mode: false
```

---

## Test suite

156 tests, 3 skipped:
- `test/test_charging_planner.py` — price parsing, window selection, DP algorithm, gap constraint, spillover, plan building, schedule resolution, retained minutes
- `test/test_deliver_chargeamps.py` — Charge Amps delivery handler (login/cache, connector mode, period fields, period timing)

---

## Files

| File | Description |
|---|---|
| `charging_planner.py` | Core planner |
| `delivery/deliver.py` | Delivery dispatcher |
| `delivery/deliver_chargeamps.py` | Charge Amps handler |
| `index.html` | GitHub Pages dashboard |
| `config.yaml` | Configuration template |
| `.github/workflows/schedule.yml` | GHA workflow |
| `test/test_charging_planner.py` | Planner tests |
| `test/test_deliver_chargeamps.py` | Charge Amps tests |
| `README.md` | Project documentation |
| `CONTEXT.md` | This file |

---

## Theme candidates

Seven color pairs considered as alternative themes for the dashboard. Each pair is (light mode accent / dark mode accent). The current theme uses warm cream + teal.

| # | Name | Light bg | Accent (light) | Dark bg | Accent (dark) |
|---|---|---|---|---|---|
| 1 | Cherry blossom / Deep twilight | `#F7F4EF` | `#1A1265` | `#1A1265` | `#F9A8BB` |
| 2 | Celadon / Chocolate plum | `#F0F7F0` | `#553832` | `#553832` | `#A8D3A8` |
| 3 | Shadow grey / Sandy clay | `#f5f0eb` | `#272727` | `#272727` | `#D4AA7D` |
| 4 | Electric rose / Chartreuse | `#f5f0eb` | `#FE00AE` | `#272727` | `#C1FE1A` |
| 5 | Icy blue / Gunmetal | `#EEF5FF` | `#35393C` | `#35393C` | `#A4D8FF` |
| 6 | Raspberry red / Deep space blue | `#FFF0F5` | `#EE005A` | `#012641` | `#EE005A` |
| 7 | Lime cream / Vintage grape | `#DDEA78` | `#433455` | `#433455` | `#DDEA78` |

---

## Future work

### Additional charger delivery handlers

The delivery architecture is designed for easy extension — a new `deliver_<n>.py` with a single `deliver()` function is all that's needed. Candidates researched:

**Easee** — best fit. Official public API (`api.easee.com`), well documented. `set_basic_charge_plan(chargeStartTime, chargeStopTime)` maps directly to our window format. Python library `pyeasee` available on PyPI, or can be implemented with raw `urllib` like the Charge Amps handler. Most popular charger in Norway/Sweden.

**Wallbox** — Python module (`wallbox` on PyPI). Scheduling uses `start`/`stop` as `"HHMM"` strings with a days bitmask — weekly recurring format, not per-night. Less natural fit. API also showing rate limit issues (429) in recent HA reports.

**Zaptec** — official API (`api.zaptec.com`). Scheduling supported on newer chargers. Mix of official and reverse-engineered endpoints. More complex than Easee.

**go-e** — has both a local HTTP API (direct to charger IP) and a cloud API (`{serial}.api.v3.go-e.io`) authenticated with a token from the app. Cloud API works from GHA — no local network needed. Scheduling API uses key-value pairs set via GET parameters. V2 API scheduler format needs investigation to confirm it can express arbitrary time windows cleanly. Potential good fit if scheduler keys map well.

Priority: Easee first, then go-e, then Zaptec, then Wallbox.

### SoC-derived required_hours

Currently `required_hours` is a static config value set conservatively for the worst case. The goal is to derive it dynamically at plan-build time from the car's current state of charge and target SoC, read from the charger via OCPP 2.0.1 or 2.1.

**Trigger:** Charge Amps LUNA firmware update to OCPP 2.0.1 or 2.1 — these versions provide reliable SoC measurand reporting. OCPP 1.6 has a SoC measurand but availability varies by charger/car combination.

`required_hours` will remain as a config fallback for when the car is not plugged in at plan-build time or SoC data is unavailable.
