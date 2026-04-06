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

### Session 12 — 2026-03-31

**Histogram Mode 3**: when now reaches or passes the charging midpoint, the window switches from charge-centered to now-centered (now-12h → now+12h). The window then tracks now in real time, revealing forecast bars to the right as time progresses. Also applied to the no-charging-slots fallback — now-centered ±12h makes more sense than the old now-1h → now+23h since there's nothing to anchor on.

**cron confirmed**: `30 10 * * *` fires consistently at ~14:30 EEST. Settled.

**forecast augmentation cap**: confirmed 12h at this point. Extended to 24h in Session 13 to cover full overnight windows when supplementing with forecast.

### Session 13 — 2026-04-01

**v1.0.0 released** — first public release tagged on 2026-04-01. Charge Amps handler tested and supported. See RELEASE_NOTES.md.

**v1.1.0 released** — forecast supplementation for partial window coverage. See RELEASE_NOTES_v1.1.0.md.

**Forecast supplementation for partial window coverage** (major feature): previously if real prices covered less than 90% of the charging window the profile was skipped entirely — no plan, no delivery that day. Root cause: ENTSO-E or Sähkötin only has today's prices when the script runs before Nord Pool publishes (~11:00–12:00 UTC), which can happen on exception days.

Fix: instead of skipping, the planner now supplements candidate prices with forecast slots to fill the window gap and builds a plan on the best available data. `price_source` is set to `"forecast"` when forecast slots were used for selection, triggering the dashboard warning banner. This is the same source used for the full forecast fallback — just applied at the window level rather than the global price fetch level.

`_check_window_coverage` changed from raising `PricesNotYetAvailable` to returning `bool` — `False` triggers supplementation, still raises if no forecast slots are available either. `fetch_forecast_display_slots` cap extended from 12h to 24h to ensure full overnight window coverage (~16h from 14:30 to 06:30).

**`plan_warning` field**: when `total_minutes < required_minutes` the JSON includes a human-readable reason. Two root causes: (1) price ceiling — detected by running a shadow plan without the ceiling; if shadow succeeds, ceiling was the cause → `"partial plan — price limit X c€/kWh"`. (2) data shortage — shadow also fails, or no ceiling set → `"partial plan — required hours exceed boundaries"`. "Boundaries" covers both `preferred_window_end` (the hard spillover limit) and the forecast horizon for `any:any`. Dashboard shows the reason in amber in brackets next to the scheduled hours — no warning emoji since the amber colour already signals the issue.

**Partial plan reasoning**: `preferred_window_end` is the final frontier — spillover fills from outside the window inward but never past the end. Comparison plan uses the same window + slot constraints, just removes `max_price_eur`. Symmetric with optimal: optimal removes the window constraint but keeps the ceiling; comparison removes the ceiling but keeps the window.

**Forecast horizon**: nordpool-predict-fi covers ~35h ahead. Display fetch capped at 24h beyond last real slot — enough to cover any overnight window. With `any:any` window the horizon is the only limit.

**GHA failure notifications**: verified — plan cannot be built exits non-zero (`sys.exit(1)`), delivery failure exits non-zero (`sys.exit(0 if ok else 1)`). Both trigger GHA email to operator.

**Bar height scaling**: min price at 10% height, max at 90% (`h = 10 + x*80`). Reverted price ticks experiment — added visual noise without meaningful new information, avg line is sufficient reference.

**Console output updated**: `print_plan_summary` now shows all plan details in an indented layout. Detail lines align under the value at a fixed indent column. Scheduled block shows: duration (with "of Xh required" when partial), carried over minutes, plan warning reason. Avg price block shows: price, savings vs market avg, vs optimal when suboptimal. Example full output:

```
  Scheduled  1h30m of 2h required
             30min carried over
             price limit 2.50 c€/kWh
  Avg price  1.20 c€/kWh
             ↓ 40% below market avg
             vs optimal ↑8%
```

### Session 17 — 2026-04-06

**`max_price_cents_kwh: "avg"` dynamic ceiling** (v1.4.0):

- `"avg"` (case-insensitive) accepted as a valid value alongside positive numbers and `null`
- `Config` gets `max_price_is_avg: bool = False` field (after `schedule` to avoid dataclass ordering issues)
- Resolved at plan time in `_plan_one_profile` from `display_prices` (horizon-capped real prices) → `resolved_max_price_eur`
- `resolved_max_price_eur` passed as `max_price_override` to `_select_slots` and used throughout slot selection and spillover — `cfg.max_price_eur` stays `None`
- Plan warning shows `"partial plan — price limit avg (X c€/kWh)"` with resolved value when partial
- 7 new tests in `TestAvgPriceCeiling` covering validation, parsing, and end-to-end behaviour
- `config.yaml` and README updated

**172 tests** passing.

**Dashboard histogram height doubled** (`index.html`): histogram height increased from 56px to 112px for better readability of price differences between slots. Scaling (10–90%) unchanged.

---

### Session 15 — 2026-04-05

**Charge Amps active session protection** (v1.3.0): handler always reads connector state before delivery. If `isCharging: true`, schedule override is activated after delivery via `PUT /api/chargepoints/{id}/{connector_id}/schedule/override`. Override expires when cable disconnected.

Key findings from live testing:
- `isActive: true` is correct — confirmed by testing `isActive: false` which causes schedule to be completely ignored (charger behaves as always-on)
- Schedule PUT always switches charger to Schedule mode regardless of `isActive` value — this is Charge Amps by design, confirmed by web portal network capture
- Override error codes treated as success: `OverridingScheduleExists` (already active), `NoScheduleForConnector` (no active schedule)
- Connector state fields confirmed from live API: `isCharging` (boolean), `onBySchedule`/`offBySchedule`, `mode`, `ocppStatus`

**Connector dump script** (`connector_dump.py`): diagnostic tool to read raw connector state from Charge Amps API.

**schedule.yml fix**: publish step now uses `always()` so plan JSONs are saved to `data/` even when delivery fails.

**v1.3.0 released.**

---

### Session 16 — 2026-04-05

**Bug fixes** (v1.3.1):

**`any:any` window 7-day delivery failure**: on Sundays with extended forecast prices, the `any:any` window selected slots on next Monday which exceeded Charge Amps' 604800-second schedule limit. Fix: planning horizon (tomorrow 23:00 UTC) now caps `any:any` window end in `_resolve_any_window` and `preferred_window_any` blocks. The planner never selects slots beyond the next day regardless of how far the forecast extends.

**Forecast display slots not showing in histogram**: two bugs fixed:
1. Display fetch was skipped when `price_source == "forecast"` — removed the guard so display slots are always fetched regardless of price source.
2. When real prices are supplemented with forecast (rule 2), the display fetch anchor was computed from `all_prices` (which includes supplement slots) instead of from the horizon-capped real prices — fixed to use horizon-capped boundary.

**Rule 2 supplement fix**: when real prices don't reach tomorrow noon, forecast prices now **supplement** real prices (appended after last real slot) rather than replacing them entirely. `price_source` is still marked as `forecast`. Previously discarded real prices for the day.

**Price source rules — four rules now tested and enforced:**
1. Real prices reach tomorrow → display forecast appended as `forecasted:true`, `price_source` = real source, display slots never selected for charging
2. Real prices partial → forecast supplements real prices for planning, `price_source = forecast`
3. No real prices → full forecast fallback, `price_source = forecast`
4. Planning horizon caps slot selection; display forecast extends 24h beyond it freely (stored in JSON as `forecasted:true`)

**Supplement slots tagged `forecasted:true` in JSON**: supplement forecast slots (rule 2) previously appeared as plain grey bars in the histogram, indistinguishable from real price slots. Now written to `price_slots` with `forecasted: true` so the dashboard renders them with the hatched grey forecast pattern. Charging slots selected from supplement data remain green/suboptimal as normal. `supplement_starts` set tracked at `cmd_plan` level, passed through `PlanParams`.

**Dashboard fixes** (`index.html`):
- Forecast legend now appears naturally via `visible.some(s => s.forecasted)` — works correctly now that supplement slots carry `forecasted: true`. No plan-level override needed.
- Negative price slots rendered at zero bar height (real slots only). Forecast/display slots keep minimum height since they serve as visual context. Histogram scale baseline shifts to 0 when negative prices exist so positive bars remain proportional. Avg line uses same corrected scale.

**165 tests** (8 new `TestPriceSourceRules` + 1 new supplement-tagging test).

**Validated live**: manual run at 09:24 local with tomorrow's prices unavailable confirmed all four rules working — 204 real slots, 96 supplement forecast slots (tagged `forecasted:true`), 96 display forecast slots, all charging slots falling within supplement range and correctly tagged.

---

### Session 14 — 2026-04-01

**Plan comparisons feature** (v1.2.0): two background comparisons now run on every plan build, documented together as a unified feature.

- **Optimal comparison** (existing but newly documented): finds cheapest slots ignoring preferred window, same price ceiling and slot constraints. Result is `avg_optimal_price_cents_kwh` and `optimal` flag per slot. Dashboard shows `vs optimal ↑N%` when window forced suboptimal choices.
- **Price ceiling comparison** (new): when plan is partial and price ceiling is set, reruns selection without ceiling. If it succeeds → `plan_warning: "partial plan — price limit X c€/kWh"`. If it also fails or no ceiling → `plan_warning: "partial plan — required hours exceed boundaries"`. Covers both short window and `any:any` hitting forecast horizon. Dashboard shows reason in amber next to scheduled hours.
- Both comparisons documented in README under "Plan comparisons" section.
- Terminology: "comparison plan" not "shadow plan" in all external-facing text.

**Console output** (`print_plan_summary`): redesigned to indented layout. Scheduled block: duration, carried over, plan warning. Avg price block: price, vs market, vs optimal. Each detail on its own indented line aligned under the value.

**Histogram bar scaling**: min 10%, max 90% (`h = 10 + x*80`). Reverted price ticks — noise without value.

**SoC-derived required_hours** removed from roadmap — not feasible with this project's cloud API architecture. OCPP SoC data stays inside the charger firmware loop; becoming an OCPP server is out of scope. Documented in future work for completeness.

**v1.2.0 drafted** — covers plan comparisons, console output improvements, histogram scaling.

**Easee delivery handler** (`deliver_easee.py`): first version written based on official Easee API documentation. No extra dependencies — raw `urllib` like Charge Amps. Auth: POST `/api/accounts/login` with `CHARGER_USERNAME`/`CHARGER_PASSWORD`, returns bearer token cached for process lifetime. Single window → Basic Charge Plan (`/api/chargers/{id}/basic_charge_plan`, `repeat: false`, specific datetime). Multiple windows → Weekly Charge Plan (`/api/chargers/{id}/weekly_charge_plan`, full 7-day replacement, same contract as Charge Amps). Marked untested in docstring. Uses same env vars as Charge Amps (`CHARGER_USERNAME`, `CHARGER_PASSWORD`, `CHARGER_ID_<N>`).

**`CHARGER_EMAIL` → `CHARGER_USERNAME`**: renamed across all files (handlers, tests, schedule.yml, README, delivery README) — more generic, works for both email and phone number login.

**delivery/README.md updated**: OCPP handler removed, Easee handler added with untested warning, `CHARGER_USERNAME` throughout.

**Zaptec assessed as not viable**: official API is well documented but scheduling is not a native concept — dynamic current control only, no discrete on/off windows. Would require an always-on process. Removed from priority list.


---

## Charge Amps integration

The official Charge Amps external API (`eapi.charge.space`) does not support scheduling, override control, or connector state reading — it only exposes basic charger control. Everything we use — schedule delivery, mode detection and restore, active charging detection, override activation — is only available via the web portal API (`my.charge.space`), which is the same API the Charge Amps web app uses. `deliver_chargeamps.py` authenticates with the user's own credentials and uses this portal API directly.

Without portal API access none of the sophisticated session protection logic would be possible — the external API simply doesn't expose these capabilities.

**Schedule delivery always switches to Schedule mode.** The schedule PUT switches the charger to Schedule mode regardless of `isActive` value and regardless of what mode it was in before. `isActive: true` → mode switches AND windows are immediately enforced (charging stops if now is outside a window). `isActive: false` → mode switches but windows are NOT enforced — charging continues but tonight's schedule won't fire either. Tested both variants. `isActive: true` is correct. This behaviour is not a bug in our delivery; the web portal is identical (confirmed by network capture).

**Override error codes**: `OverridingScheduleExists` (400) — override already active, treat as success. `NoScheduleForConnector` (400) — schedule was delivered with `isActive: false` or no schedule exists, override not applicable, treat as success.

**Override mechanism.** `deliver_chargeamps.py` always reads the connector state (`isCharging`) before delivery. If the car is actively charging, schedule override is activated after delivery via `PUT /api/chargepoints/{id}/{connector_id}/schedule/override`. This tells the charger to ignore the schedule for the current session. If an override is already active, the API returns `{"error":"OverridingScheduleExists"}` which is treated as success — the session was already protected. Override is automatically cleared when the cable is disconnected, so the next session will follow the schedule normally.

**Connector state fields** (confirmed from live API response):
- `isCharging` — boolean, true when car is actively charging. Used to decide whether to activate override.
- `onBySchedule` / `offBySchedule` — whether schedule is currently enabling or blocking charging
- `mode` — `"On"`, `"Off"`, or `"Schedule"`
- `ocppStatus` — `"Charging"`, `"Available"`, etc.

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

**Zaptec** — official well-documented API (`api.zaptec.com`), OAuth2, no reverse engineering needed. However scheduling is not a native API concept — the integration model is dynamic current control (`AvailableCurrent` via `/api/installation/{id}/update`) rather than delivering discrete on/off windows. Time-based charging would require sending start/stop commands at the right times from an always-on process, which is fundamentally incompatible with the GHA cron architecture. **Not a viable handler for this project.**

**go-e** — has both a local HTTP API (direct to charger IP) and a cloud API (`{serial}.api.v3.go-e.io`) authenticated with a token from the app. Cloud API works from GHA — no local network needed. Set parameters via `GET /api/set?token={token}&{key}={value}`. Scheduler keys exist in v2 API: `sch_week`, `sch_satur`, `sch_sund` (R/W, object, `control` enum: Disabled=0, Inside=1, Outside=2). However the actual time range object format is undocumented — not in the official API docs and not found in any community reverse-engineering. Without knowing the payload structure for the time window, the handler cannot be written. Blocked until the scheduler object format is discovered (e.g. by reading `sch_week` from a real charger that has a schedule set via the app).

Priority: Easee first, then go-e. Zaptec not viable (no native schedule delivery). Wallbox low priority (weekly recurring schedule model, poor fit).

### SoC-derived required_hours

Currently `required_hours` is a static config value set conservatively for the worst case. Deriving it dynamically from the car's actual state of charge would make the planner genuinely autonomous — a short commute day with 70% remaining SoC would produce a 1h plan; starting from 20% would produce a 5h plan.

**Why this is unlikely for this project:** This tool sits outside the charger's firmware loop, using cloud APIs. SoC data from OCPP 2.0.1/2.1 is communicated internally between the car and charger — charger manufacturers are unlikely to expose it via their external API. The alternative would be to pivot to an OCPP server that communicates directly with the charger, but that is not the intention of this project.

`required_hours` will remain a static config value. The feature is documented here for completeness but is not on the roadmap.
