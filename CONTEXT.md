# charging-planner — Project Context

This document captures the full development history, decisions, and current state of the charging-planner project across all development sessions.

---

## What it does

Fetches day-ahead electricity prices and schedules EV charging for the cheapest available hours, automatically, every day. Delivers the schedule to one or more chargers via the Charge Amps API or OCPP WebSocket. Publishes a GitHub Pages dashboard showing the current plan.

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
Full dashboard redesign (hero price theme), light/dark theming, bar hover interaction, touch support, ntfy refactoring, gap merge removal, algorithm correctness fixes.


### Session 8 — 2026-03-28 (afternoon)

**min_gap_minutes**: new config parameter separating gap constraint from block length. `min_slot_minutes` now only controls minimum block length; `min_gap_minutes` (default 15, divisible by 15, can be 0) controls minimum gap between blocks independently.

**retained_hours**: each run reads the previous plan JSON, counts future `charging: true` minutes, and adds them to `required_hours` before running the DP. The scheduler covers the full combined need with proper gap enforcement — no special merging, no OCPP complications. `retained_hours` written to plan JSON (converted from minutes). Dashboard hero shows `scheduled 2h + 30m retained` in accent2 color when non-zero. End-to-end tests updated to use `tempfile.TemporaryDirectory` for output dir to prevent retained minutes leaking between test runs.

**ntfy removed**: GHA is the primary run environment. Delivery failures already exit non-zero. Forecast warning visible on dashboard. `_send_delivery_ntfy` and `skipped_profiles` logic removed from `deliver.py`. `NTFY_TOPIC` removed from `schedule.yml`. `ntfy:` block removed from `config.yaml`. `test_deliver.py` removed entirely (covered ntfy logic only) — test count drops from 186 to 156.

**Responsive histogram ticks**: 4h intervals on screens narrower than 520px (phone portrait), 2h on wider screens.

**Schedule cell centering**: profile name cell in the config back-card weekly schedule now uses flex centering to match the "any" cells.

**OCPP_ENDPOINT_URL removed from GHA**: OCPP delivery requires direct network access to the charger — not applicable to GHA runs. Removed from `schedule.yml` and README secrets table. `websockets` dependency moved to a separate "Optional — local OCPP delivery only" section in README requirements.

**README rewritten**: "What makes this different" section expanded and tightened — zero hardware, fits any setup (3.7 kW vs 22 kW example), globally optimal scheduling, realistic charger behaviour, retained hours, three-level fallback, modular delivery. Config parameter names removed from the feature pitch; concrete examples added to each bullet. ntfy section removed throughout.

### Session 7 — 2026-03-28
DST transition day. Several bugs found and fixed stemming from adding historical slots for histogram display without auditing all downstream uses of `all_prices`.

**PlanParams refactor**: renamed `all_prices` → `display_prices`, added `future_prices` (real slots from now onwards). Three clearly named pools now: `display_prices` (all real slots incl. historical — histogram output only), `future_prices` (future real slots — stats, optimal, scheduler), `forecast_slots` (predicted slots — display only, never used in calculations).

**price_stats fix**: min/avg/max and "vs market %" now use `future_prices` only, so historical price spikes don't distort the displayed market stats.

**optimal fix**: optimal slot calculation now uses `future_prices` — same pool as the scheduler, no window constraint, no historical or forecast slots.

**ENTSO-E fallback fix**: raises `PricesNotYetAvailable` when returned slots don't reach tomorrow (catches maintenance/stale responses). Triggers Sähkötin fallback correctly.

**Forecast augmentation**: always runs after any real-price fetch (removed the `tomorrow_noon` condition). Ensures histogram right edge is always filled.

**min_gap_minutes**: new config parameter separating gap constraint from block length constraint. `min_slot_minutes` now only controls minimum block length; `min_gap_minutes` (default 15, divisible by 15, can be 0) controls minimum gap between blocks. Allows e.g. 2h blocks with 30-minute gaps without forcing 2h gaps.

**Config pills**: all five back-card config fields now use pills consistently — teal for active values, grey for negative/absent (none, off, —).

**avg line z-order**: moved behind bars so playhead and price bars render in front.

**Tests**: 186 tests passing (3 skipped). `test_charging_planner.py` updated to use new `display_prices`/`future_prices` field names in `PlanParams`.

### Session 6 — 2026-03-27 (afternoon)
Histogram fix: ENTSO-E now returns historical slots like Sähkötin, so histogram can center on charging slot midpoint. ENTSO-E fallback fix: raises PricesNotYetAvailable when slots don't reach tomorrow (catches partial/stale responses during maintenance). Dashboard legend: all items now conditional on visible slots; "scheduled" renamed to "optimal"; added "suboptimal" entry for charging-but-not-optimal slots. Forecast display augmentation now always runs after a real-price fetch (not just when slots don't reach tomorrow noon) and cap extended from 12h to 24h — ensures histogram right edge is always filled. Histogram range snapped to 15-minute slot boundaries (floor rangeStart, ceil rangeEnd) to eliminate sub-slot gaps at histogram edges. Two-mode histogram window: Mode 1 (charging far away) anchors on now-1h and extends to cover all charging slots+1h; Mode 2 (charging midpoint within 11h of now) centers on charging midpoint ±12h. Ensures now is always visible and charging slots are always visible, with now drifting toward center as charging approaches.

---

## Architecture

```
charging_planner.py          # Core planner — price fetch, slot selection, plan building
delivery/
  deliver.py                 # Dispatcher — reads config, calls handlers, sends ntfy
  deliver_chargeamps.py      # Charge Amps API handler
  deliver_ocpp.py            # OCPP WebSocket handler
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

Both ENTSO-E and Sähkötin return **all slots including historical** (from the previous evening). Past slots are used by the dashboard histogram; the scheduler ignores them as it filters by window start. After every successful real-price fetch (ENTSO-E or Sähkötin), up to 24h of forecast slots are always fetched beyond the last real slot for display only — grey diagonal bars in the histogram, never used for selection. This ensures the histogram right edge is always filled even when charging slots fall late the next day.

---

## Slot selection algorithm

### `continuous_only: true`
Slides a window of `required_hours` over all candidates, picks the cheapest contiguous block.

### `continuous_only: false`
Dynamic programming: finds the globally cheapest combination of blocks covering exactly `required_hours`.

**DP formulation:**
- State: `dp[i][r]` = minimum cost to schedule `r` more slots from position `i` onwards
- Transitions: skip slot `i`, or start a block of length `k ≥ min_slots_per_block`
- Gap constraint: next block can't start within `min_slots_per_block` slots of previous block end
- Reconstruction: forward scan finding the **latest** valid block at each step (latest-preferred tiebreaker for equal-price ties)
- Globally optimal — no greedy approximation

**`min_slot_minutes`** controls the minimum individual block length — the charger should not run for less than this duration.

**`min_gap_minutes`** controls the minimum gap between blocks (default 15 min, must be divisible by 15, can be 0). Prevents the charger toggling off and straight back on. Kept separate from `min_slot_minutes` so e.g. `min_slot_minutes: 120` with `min_gap_minutes: 15` gives 2h blocks with 30-minute gaps rather than forcing 2h gaps too.

**Gap merge removed:** The old `close_gap_merge` post-selection patch was removed. Short gaps between blocks are now impossible by construction — the DP enforces gap constraints at selection time.

**Spillover:** When the preferred window doesn't have enough slots, the planner fills the deficit from outside the window (never past `preferred_window_end`). Spillover uses `min_slot_minutes=slot_dur` (15 min) since spillover slots extend existing blocks.

---

## Plan JSON structure

```json
{
  "version": 1,
  "date": "2026-03-27",
  "area": "FI",
  "price_source": "ENTSO-E",
  "timezone": "Europe/Helsinki",
  "utc_offset_hours": 2,
  "price_stats": { "min_cents_kwh": 1.81, "avg_cents_kwh": 3.23, "max_cents_kwh": 6.90 },
  "required_minutes": 120,
  "total_minutes": 120,
  "avg_price_cents_kwh": 1.99,
  "avg_optimal_price_cents_kwh": 1.99,
  "preferred_window_start": "21:00",
  "preferred_window_end": "06:30",
  "windows": [
    { "start": "14:45", "end": "16:15", "duration_minutes": 90, "avg_price_cents_kwh": 1.96 }
  ],
  "window_starts_utc": ["2026-03-27T12:45:00+00:00"],
  "window_ends_utc":   ["2026-03-27T14:15:00+00:00"],
  "price_slots": [
    { "start_utc": "...", "price_cents_kwh": 2.17, "charging": true, "optimal": true },
    { "start_utc": "...", "price_cents_kwh": 2.08, "charging": false, "optimal": false, "forecasted": true }
  ],
  "ocpp_charging_profile": { ... }
}
```

Key fields:
- `price_source`: `"ENTSO-E"` | `"Sähkötin"` | `"forecast"`
- `price_slots[].charging`: true if this slot is in the scheduled plan
- `price_slots[].optimal`: true if this slot is in the globally cheapest solution (ignoring window constraints)
- `price_slots[].forecasted`: true for display-only forecast augmentation slots (never charging/optimal)
- `avg_optimal_price_cents_kwh`: average price of the globally optimal slots

---

## Dashboard (index.html)

GitHub Pages dashboard. Fetches plan JSONs and config.yaml directly from the repository — no backend.

### Theme
Light/dark with OS preference default (`@media (prefers-color-scheme: dark)`) and manual toggle persisted in `localStorage`. Toggle button in the header.

**Light:** warm cream background (`#F7F4EF`), white cards, teal accent.
**Dark:** near-black background (`#0D0D0D`), dark cards, same teal accent.

### Front card layout

**Card header:** profile name + charge period ("charges Fri, Mar 27 → Sat, Mar 28")

**Heroes (two side by side):**
- Left: `avg scheduled` — large teal price, `scheduled Xh` and `vs market ↓XX%` underneath
- Right: `now` — large white/dark price, `min / avg / max` underneath in teal/white/amber

**Histogram (single row):**
- Solid teal bars = optimal scheduled slots (`charging && optimal`)
- Teal diagonal stripe bars = suboptimal scheduled slots (`charging && !optimal`)
- Teal outline bars = missed optimal slots (`!charging && optimal`)
- Grey diagonal stripe bars = forecast augmentation slots (display only)
- Playhead triangle above current bar = now position
- Dashed avg line
- No bar labels — all numbers live in the hero area

**Legend items** are all conditional — each only appears when that bar type is visible in the histogram. Labels: optimal, suboptimal, missed, forecast.

**Histogram window:** Two-mode logic, always snapped to 15-minute slot boundaries (floor/ceil):
- **Mode 1** (charging midpoint > 11h away): rangeStart = now-1h, rangeEnd = max(now+23h, last_charging_slot_end+1h). Now is always 1h from the left edge; charging slots visible to the right. Window may exceed 24h if charging is far away.
- **Mode 2** (charging midpoint ≤ 11h away): rangeStart = mid-12h, rangeEnd = mid+12h. Charging centered; now floats naturally within the window since charging is nearby.
- Fallback (no charging slots): now-1h → now+23h.

**Hover/touch interaction:**
- Hovering any bar → right hero shows that slot's price and time
- Hovering a charging bar → left hero shows the window's avg price and time range (e.g. "14:45–16:15")
- Touch drag across bars works via `touchmove` + `elementFromPoint`
- `stopPropagation` prevents bar touches from flipping the card

**Window pills:** time range, avg price, duration

### Back card layout
- Config grid: required, mode, min slot, ceiling, delivery — all values shown as pills. Teal pill for active/positive values (hours, mode, min slot, ceiling, ✓ on). Grey pill for negative/absent values (none, off, —)
- Weekly schedule grid: days × start/end times, today highlighted in teal, "any" slots centered

### Warnings (banners)
- Staleness: plan data is more than 1 day old
- Forecast: `price_source === "forecast"` — "prices sourced from forecast — real-time data was unavailable"

---

## ntfy notifications

Notifications fire only when action is needed — successful deliveries with real prices are silent.

**Fires when:**
- Any delivery to a charger fails → title: "⚠ Delivery failed — {date}"
- `price_source === "forecast"` (both ENTSO-E and Sähkötin failed) → title: "⚠ Forecast prices used — {date}"
- A profile was skipped

**Also fires from the planner** (before delivery) when no plan could be built at all:
- All three price sources failed
- Price data doesn't cover any configured window
- Message: "No price data — charging could not be scheduled."

---

## Key design decisions

### DP replaces greedy slot selection
The greedy algorithm made locally optimal choices that prevented globally optimal combinations. The DP guarantees the cheapest valid solution. Reconstruction uses a latest-preferred tiebreaker for equal-price ties.

### Gap merge removed
`close_gap_merge` would bridge short gaps by including expensive gap slots, overriding price optimisation. The DP enforces gap constraints at selection time — gaps between blocks are a direct consequence of price optimisation, not something to patch.

### `min_slot_minutes` and `min_gap_minutes` are separate constraints
`min_slot_minutes` controls minimum block length; `min_gap_minutes` controls minimum gap between blocks (default 15). They were previously the same value which forced unnecessarily long gaps when `min_slot_minutes` was set to e.g. 2h.

### Forecast augmentation is display-only (unless it's the only source)
When real prices are available (ENTSO-E or Sähkötin), forecast slots in `price_slots` are never passed to the selection algorithm or optimal calculation — they exist solely to fill the histogram's right side. However, when both real sources fail, `fetch_forecast_prices` is used as a last resort and those slots ARE used for selection. In that case no display augmentation is fetched on top (the `price_source != "forecast"` guard prevents it).

### Historical slots included in price_slots
Both ENTSO-E and Sähkötin return slots from the previous evening onwards (not just future slots). This gives the histogram enough data to center properly on the charging slot midpoint without being clamped to "now".

### PlanParams price pool separation
`PlanParams` has three explicitly named price pools to prevent historical/forecast data leaking into calculations:
- `display_prices` — all real slots including historical. Used only for building `price_slots` in the JSON output.
- `future_prices` — real slots from now onwards. Used for `price_stats` (min/avg/max), optimal calculation, and print summary.
- `forecast_slots` — predicted slots. Appended to `price_slots` after real slots, never used in any calculation.

### Hero price replaces stat grid
The old 4-column stats row (scheduled / avg price / vs market / vs optimal) was replaced with two large hero numbers. The "vs optimal" percentage is replaced by visual outline bars in the histogram — if scheduled and optimal slots overlap completely, no outlines appear.

### ntfy is a failure channel only
The old behaviour sent a success notification after every delivery. Now ntfy only fires on failures, forecast fallback, or skipped profiles — eliminating noise on the happy path.

---

## Configuration reference

```yaml
entsoe:
  api_key: ""          # Injected via ENTSOE_API_KEY secret
  area: FI
  timezone: Europe/Helsinki

charging:
  - name: topup
    required_hours: 2
    continuous_only: false
    min_slot_minutes: 30       # Min block length
    min_gap_minutes: 15        # Min gap between blocks (independent of min_slot_minutes)
    max_price_cents_kwh: null
    preferred_window_start: "21:00"
    preferred_window_end: "06:30"
    schedule:
      - days: [saturday, sunday]
        preferred_window_start: any
        preferred_window_end: any
    deliveries:
      - handler: chargeamps
        charge_point_id: CHARGER_ID_1
        connector_id: 1
        max_charging_rate: 16.0
        restore_mode: false

ntfy:
  enabled: true
  topic: ""              # Injected via NTFY_TOPIC secret
```

---

## Test suite

186 tests across three files:
- `test_charging_planner.py` — price parsing, window selection, DP algorithm, gap constraint, spillover, plan building, schedule resolution
- `test_deliver.py` — ntfy message content, silence-on-success, fire-on-failure/forecast/skipped
- `test_deliver_chargeamps.py` — Charge Amps delivery handler

3 skipped (OCPP schema validation requiring external files).

---

## Files

| File | Description |
|---|---|
| `charging_planner.py` | Core planner |
| `deliver.py` | Delivery dispatcher + ntfy |
| `deliver_chargeamps.py` | Charge Amps handler |
| `deliver_ocpp.py` | OCPP handler |
| `index.html` | GitHub Pages dashboard |
| `config.yaml` | Configuration template |
| `schedule.yml` | GHA workflow |
| `test_charging_planner.py` | Planner tests |
| `test_deliver.py` | Delivery + ntfy tests |
| `test_deliver_chargeamps.py` | Charge Amps tests |
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

## Decisions

### OCPP delivery handler removed

`delivery/deliver_ocpp.py` removed. OCPP requires direct WebSocket access to the charger — fundamentally incompatible with GHA-first architecture. The `websockets` dependency was the only reason for the optional local dependency section in README.

The OCPP `ChargingProfile` object remains in the plan JSON output — ready-to-use for any downstream system that wants it. The delivery handler on top of it is gone.

`config.yaml` OCPP example block removed. README delivery table now shows only the Charge Amps handler.

### ntfy removed — GHA is the primary run environment

ntfy was originally used to notify on delivery failures, forecast prices, and skipped profiles. Removed because:
- Forecast warning is already visible on the dashboard
- Delivery failures now exit non-zero → GHA emails the operator and flags the workflow as failed
- GHA is the primary run environment; local cron is a secondary use case

`deliver.py` already exited non-zero on delivery failure — no change needed there. `schedule.yml` and `config.yaml` updated to remove all ntfy references. `_send_delivery_ntfy` and `skipped_profiles` logic removed from `deliver.py`.

---

## Future work

### Retain already-committed future charging slots across plan runs — IMPLEMENTED

Each plan run reads `data/plan-{name}.json` (or the local output dir), counts future `charging: true` minutes (`retained_minutes`), and adds them to `required_hours` before running the DP scheduler. This means the new plan always schedules `required_hours + retained_hours` total, with full gap enforcement and optimal slot selection. If the DP picks the same slots as last time, fine. If it finds cheaper ones, even better.

`retained_hours` is written to the plan JSON (converted from minutes). The dashboard hero shows `scheduled 2h + 30m retained` in accent2 color when `retained_hours > 0`. No special bar styling or `retained: true` flags needed — the DP handles everything cleanly.

The charger API is an alternative source for retained slot data but reading from `data/` is preferred — simpler, no external dependency, guaranteed to match what was delivered.

Test note: end-to-end tests use `tempfile.TemporaryDirectory` for `output_dir` to prevent retained minutes from previous test runs leaking into subsequent runs.
