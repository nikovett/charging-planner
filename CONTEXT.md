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

### Session 6 — 2026-03-27 (afternoon)
Histogram fix: ENTSO-E now returns historical slots like Sähkötin, so histogram can center on charging slot midpoint. ENTSO-E fallback fix: raises PricesNotYetAvailable when slots don't reach tomorrow (catches partial/stale responses during maintenance). Dashboard legend: all items now conditional on visible slots; "scheduled" renamed to "optimal"; added "suboptimal" entry for charging-but-not-optimal slots.

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

Both ENTSO-E and Sähkötin return **all slots including historical** (from the previous evening). Past slots are used by the dashboard histogram; the scheduler ignores them as it filters by window start. After a successful real-price fetch, the planner checks if the last slot is before `(today+1) 12:00 UTC`. If so, up to 12h of forecast slots are fetched for display only — grey diagonal bars in the histogram, never used for selection.

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

**`min_slot_minutes`** applies to both:
- Minimum individual block length
- Minimum gap between blocks

This prevents the charger from being toggled on/off too rapidly in either direction.

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

**Histogram window:** centered on the midpoint of charging slots ±12h, starting no earlier than the first available slot.

**Hover/touch interaction:**
- Hovering any bar → right hero shows that slot's price and time
- Hovering a charging bar → left hero shows the window's avg price and time range (e.g. "14:45–16:15")
- Touch drag across bars works via `touchmove` + `elementFromPoint`
- `stopPropagation` prevents bar touches from flipping the card

**Window pills:** time range, avg price, duration

### Back card layout
- Config grid: required, mode, min slot, ceiling, delivery
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

### `min_slot_minutes` applies to gaps too
Both block length and inter-block gaps must be ≥ `min_slot_minutes`. Same physical constraint: the charger shouldn't toggle on or off for less than this duration.

### Forecast augmentation is display-only
Forecast slots in `price_slots` are never passed to the selection algorithm or optimal calculation. They exist solely to fill the histogram's right side before Nord Pool publishes tomorrow's prices.

### Historical slots included in price_slots
Both ENTSO-E and Sähkötin return slots from the previous evening onwards (not just future slots). This gives the histogram enough data to center properly on the charging slot midpoint without being clamped to "now".

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
    min_slot_minutes: 30       # Min block length AND min gap between blocks
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
