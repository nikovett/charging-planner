# charging-planner — Project Context

This document is the authoritative reference for the charging-planner project. It is structured in three parts:

- **Reference** — current state only, updated in place when things change
- **Changelog** — one entry per release, append-only
- **Session log** — development history, append-only

---

## Guiding principles

Three principles, in priority order — any change that would violate a higher one is wrong regardless of what it does for a lower one:

1. **A charging plan may never fail to have the vehicle ready by its expected departure, nor interrupt a charging session already in progress.**
2. **A charging plan must respect its configured window and constraints, except where doing so would conflict with the First principle.**
3. **A charging plan must minimize cost, except where doing so would conflict with the First or Second principle.**

Cost minimization is the entire point of this project, but it is always subordinate to respecting the configured window, which is itself always subordinate to never leaving the driver stranded or interrupting a charge already underway. When evaluating a design change or reviewing a bug, check it against these in order — a fix or feature that improves cost optimization at the expense of window correctness, or improves window correctness at the expense of departure readiness or an active session, is a regression even if it looks like an improvement locally.

---

# Reference

## What it does

Fetches day-ahead electricity prices and schedules EV charging for the cheapest available hours, automatically, every day. Delivers the schedule to one or more chargers or vehicles via cloud APIs. Publishes a GitHub Pages dashboard showing the current plan.

---

## Files

| File | Description |
|---|---|
| `charging_planner.py` | Core planner — price fetch, window resolution, slot selection, plan building |
| `delivery/deliver.py` | Delivery dispatcher — loads handlers, redundant-delivery protection |
| `delivery/deliver_chargeamps.py` | Charge Amps handler (tested against real hardware) |
| `delivery/deliver_easee.py` | Easee handler (untested against real hardware) |
| `delivery/deliver_myskoda.py` | MyŠkoda handler — delivers to the vehicle directly, not a charger (tested against real hardware) |
| `index.html` | GitHub Pages dashboard |
| `config.yaml` | Configuration template (committed with empty secrets) |
| `.github/workflows/schedule.yml` | Daily GHA workflow |
| `test/test_charging_planner.py` | Planner tests |
| `test/test_deliver.py` | Dispatcher tests — redundant-delivery protection |
| `test/test_deliver_chargeamps.py` | Charge Amps tests |
| `test/test_deliver_easee.py` | Easee tests |
| `test/test_deliver_myskoda.py` | MyŠkoda tests |
| `test/README.md` | Test suite documentation, per-class breakdown |
| `README.md` | Project documentation (end-user facing) |
| `CONTEXT.md` | This file (maintainer/contributor facing) |

---

## End-session skill

The `end-session` Claude skill automates the session wrap-up workflow. Trigger with `/end-session` or phrases like "let's wrap up", "commit the files", "update CONTEXT".

**Phases:**
1. Run all test suites — stop if any fail
2. Identify what changed (inferred from conversation or asked)
3. Update `CONTEXT.md` — Reference in place, Changelog prepend, Session log append
4. Update `test/README.md` if test suite changed
5. Copy modified files to `/mnt/user-data/outputs/` (working copy → correct repo paths)
6. Write `commit.sh` — ready-to-run shell script with `git add` and commit message
7. Present all outputs

**Key rules encoded in the skill:**
- Release notes go to GitHub Releases only — never create `RELEASE_NOTES_*.md` in the repo
- `charging_planner_current.py` → outputs as `charging_planner.py`
- `test_charging_planner_current.py` → outputs as `test/test_charging_planner.py`
- JS syntax check (`node --check` on extracted script block) mandatory before presenting `index.html`
- Test counts verified by running tests, never from memory

**Install:** drop `end-session.skill` into Claude's skills directory via Settings → Skills.

---

## Architecture & data flow

```
charging_planner.py          # writes plan-{profile}.json — knows nothing
                              # about chargers or vehicles at all
data/
  plan-{profile}.json         # committed each run, dashboard-facing
  delivered-{profile}-{handler}-{hash}.json   # ground truth for "what's
                                               # actually running" — see
                                               # Delivery below
delivery/
  deliver.py                  # separate process — reads plan-{profile}.json
                               # + config.yaml, decides whether to deliver,
                               # dispatches to the right handler
  deliver_chargeamps.py       # each handler: deliver(plan, charge_point_id,
  deliver_easee.py            # entry, timezone) -> bool. Same interface,
  deliver_myskoda.py          # genuinely different safety models — see
                               # each handler's own section below.
index.html                    # GitHub Pages dashboard — reads plan-*.json
                               # and config.json from data/, no backend
config.yaml                   # source config (committed with empty secrets)
.github/workflows/
  schedule.yml                 # daily GHA workflow: plan -> deliver -> publish
```

The planner and delivery are deliberately two separate processes, not two functions in one script — `charging_planner.py` never imports anything from `delivery/`, and a plan JSON file is the entire interface between them. This is what makes it possible to reason about planning correctness (does the DP pick the cheapest valid slots) and delivery safety (is it ever safe to push this to a real device) as separate concerns, each independently testable, and it's why `delivery/deliver.py` can protect against redundant delivery using only what's in the plan JSON plus its own persisted state, with no access to the planner's internals.

**One real gap this separation currently has**: `charging_planner.py` writes `plan-{profile}.json` unconditionally, and the GHA workflow's "Publish to data/" step copies it regardless of what `deliver.py` decided — so the dashboard can show a plan that was never actually delivered. See "Future work" below.

---

## Price acquisition

### Fallback chain

Built dynamically from the configured area by `_build_fallback_chain(area)`. Each source raises `PricesNotYetAvailable` on failure; the next source in the chain is tried automatically.

```
FI:        ENTSO-E → Elering → Sähkötin → nordpool-predict-fi (forecast)
EE/LV/LT:  ENTSO-E → Elering
SE1–SE4:   ENTSO-E → elprisetjustnu.se
NO1–NO5:   ENTSO-E → hvakosterstrommen.no
other:     ENTSO-E only
```

All sources use EUR/kWh ex-VAT. ENTSO-E returns EUR/MWh for all areas including SE and NO; the regional sources also provide EUR/kWh directly (SEK and NOK fields are present in SE/NO responses but unused — **why**: converting through SEK/NOK would need a live FX rate, an extra failure mode for no benefit when the source already provides EUR directly).

1. **ENTSO-E** — primary. Day-ahead 15-min prices. Retries 5×, backoff 5s. Raises `PricesNotYetAvailable` if slots don't reach tomorrow (catches partial/stale responses e.g. during maintenance).
2. **Elering** (`dashboard.elering.ee/api`) — actual Nord Pool 15-min prices for FI/EE/LV/LT. No API key. `price_source: "Elering"` in plan JSON, no dashboard warning.
3. **Sähkötin** (`sahkotin.fi/api`) — actual Nord Pool 15-min prices, FI only, no API key. `price_source: "Sähkötin"`.
4. **nordpool-predict-fi** — ML forecast blended with realized prices. FI only. `price_source: "forecast"` in plan JSON; triggers dashboard warning.
5. **elprisetjustnu.se** (`fetch_elprisetjustnu_prices`) — SE1–SE4, native 15-min (96 slots/day). No API key. `price_source: "elprisetjustnu.se"`.
6. **hvakosterstrommen.no** (`fetch_hvakosterstrommen_prices`) — NO1–NO5, hourly (24 slots/day) expanded to 4×15-min. No API key. `price_source: "hvakosterstrommen.no"`.

**Why `_build_fallback_chain` returns `(fn, name)` tuples, not just functions** — names are embedded at build time so they survive `mock.patch` replacing module-level names with `MagicMock` objects that have no `__name__` of their own; without this, test mocking would break the chain's own logging.

**Why SE and NO are separate functions rather than one parameterized regional fetcher** — consistent with the existing one-function-per-source pattern (Elering, Sähkötin each get their own function too); a shared regional function would need a source-specific branch inside it anyway, so nothing is actually saved by merging them.

### Forecast supplementation

Both ENTSO-E and Sähkötin return all slots including historical (from the previous evening). Past slots are used by the dashboard histogram; the scheduler ignores them (via the `now_utc` floor — see "Window resolution" below).

After every successful real-price fetch, up to 24h of forecast display slots are fetched beyond the last real slot (**FI only** — `fetch_forecast_display_slots` returns `[]` for other areas internally; non-FI areas with partial prices proceed with what they have, no supplement). These are display-only (grey diagonal bars on the dashboard), never used for selection on their own.

When real prices don't fully cover the charging window, forecast slots supplement them for actual *selection* too — `_check_window_coverage` decides this: real coverage below **90%** of the window (a plain hardcoded literal, `charging_planner.py`, not derived from ENTSO-E's own publication mechanics or configurable per profile) triggers the supplement; `price_source` is set to `"forecast"` and the dashboard shows a warning. Below 90% was already the threshold before this project's forecast-supplement capability existed — originally, below it meant the profile was skipped entirely that day (no plan, no delivery); the 2026-04-xx change was about what happens *below* the threshold (supplement instead of give up), not about the number itself. Nobody currently working on this project chose `0.90` or knows why that specific value — it predates the earliest detailed session-log entries. Worth knowing precisely where it lives (one literal, one place) if it ever needs revisiting, rather than assuming it encodes some derived rule.

**Coverage is measured against the still-useful portion of the window, not the full nominal span** — `_check_window_coverage` takes `now_utc` and clamps its denominator to `max(win_start_utc, now_utc)`. Without this, a *live* window (see "Window resolution") would always appear under-covered by exactly its already-elapsed duration — real time that was correctly excluded upstream, not prices that failed to publish — and would trigger an unnecessary forecast supplement, or worse, let a supplement backfill time that's already in the past. The forecast-supplement candidate filter applies the identical clamp for the same reason.

### PlanParams price pool separation

Three explicitly named pools, kept separate to stop data leaking across calculations that have genuinely different requirements:
- `display_prices` — all real slots including historical; used only for `price_slots` JSON output (the dashboard histogram wants the full history)
- `future_prices` — real slots from now onwards; used for `price_stats`, the optimal-comparison calculation, and the scheduler itself
- `forecast_slots` — predicted slots; appended to `price_slots` for display, never used in cost calculations or selection directly (selection only reaches forecast data via the coverage-supplement path above, which is deliberately separate)

---

## Window resolution

This is where most of the non-obvious design in this project lives — both *what a window means* and *which specific window instance a given run should target* turned out to need careful, deliberate rules rather than the more obvious literal reading of the config.

### What a window actually represents

`preferred_window_start`/`preferred_window_end` aren't arbitrary search bounds — each end represents a real-world constraint on a specific day's charging session:

- **End = the departure deadline.** The car needs to be at target charge by this time for the next leg.
- **Start = the earliest the car is realistically home and plugged in.** Not "search from here because it's convenient" — it's an implicit claim about vehicle presence that the planner takes on faith from config. It has no way to verify the car is actually there yet; if this is set too early, the DP will silently consider slots the vehicle isn't present for, with no error or warning to catch it. Keeping this honest is the user's responsibility, not something the system cross-checks.
- **The span between them is usually looser than `required_hours` needs.** That slack — window duration minus actual charging time needed — is exactly what `min_slot_minutes`, `min_gap_minutes`, and `max_windows` optimize within; it's buffer for the DP to find the cheapest sub-selection, not time the car is expected to be actively charging throughout.

**`any`/`any` isn't a degenerate case of the same rule — it's a different rule for a different situation.** It represents having *no* deadline and *no* arrival constraint at all (the canonical case: a weekend day with no commute to plan around), so there's nothing to bound the search to on either side. That's why it collapses to `(now, price horizon)` rather than being treated as "just another shape" alongside fixed windows. This distinction is precisely what the 2026-09-20 regression (below) got wrong: because `any`/`any` is trivially satisfiable at any moment, it's easy to mistake for one shape among several that a single classification loop can treat uniformly — but its defining property (no deadline) is exactly the thing that must never be allowed to shadow a *different* day's real deadline. Worth keeping in mind for any future refactor of this area.

### Which window instance a run targets

`_resolve_planning_horizon` decides which window *instance* a plan targets. Two genuinely different cases, matching a distinction the original code always had:

**Bare profile** (no `schedule:`, no top-level `any` flag) — a single, unvarying shape. No "which weekday" question at all: check today's own occurrence first (upcoming or live), then tomorrow's as the fallback once today has elapsed.

**Schedule (or top-level `any`) present** — a schedule entry is indexed by the day the charging is *for*, not by the calendar date its window instance starts on. The "monday" entry describes the session that gets the car ready for Monday, which for an overnight shape actually starts *Sunday* evening. This is exactly how the original code always worked: it resolved the schedule for `tomorrow` and used shape alone (overnight vs. same-day) to decide whether to anchor the resulting window to today or to tomorrow itself.

**Regression caught in production (2026-09-20) and fixed**: an earlier version of this function queried each candidate date's *own* weekday directly instead of indexing by day-of-use. On a Sunday with a weekday-overnight/weekend-`any` schedule, that meant Sunday's own entry (`any`/`any`, trivially always "live") was used directly — Monday's fixed `21:00–06:30` window was never even considered, and slots landed on Monday afternoon instead of Sunday night. Fixed by restoring the day-ahead (tomorrow-indexed) rule for the schedule/`any` branch specifically; see `TestResolvePlanningHorizon.test_schedule_regression_weekend_any_does_not_mask_weekday_overnight` for the exact reproduction.

Within the schedule/`any` branch, two candidates, checked in priority order:
1. **Today's own entry** — what yesterday's daily run would have targeted (that run resolved "tomorrow" relative to itself as today). Relevant only if overnight-shaped: only that shape's window instance can still be open this many hours later, in the early morning. Same-day and `any`-ended shapes can't still be open a full calendar day after the run that targeted them.
2. **Tomorrow's entry** — the normal target for a daily run (day-ahead prices apply to tomorrow). Overnight shapes anchor to today (window starts this evening); other shapes anchor to tomorrow itself. Can never itself classify as elapsed (`now` is by definition still within today) — this candidate always succeeds.

**Classification** (`_classify_window_instance`) — "elapsed" depends on shape, since not every shape has a fixed end:
- both `any` → never elapsed (trivially live, start = now)
- `any` start → elapsed once the target date's occurrence of the fixed end time has passed
- `any` end → elapsed once `required_minutes` no longer fits between now and `any_end_cap` (`min(last available price, plan_horizon)`) — there's no fixed clock-time end to compare against, so "does it still fit" is the only sensible boundary. Note: within the schedule/`any` branch, "fixed start, `any` end" and "`any` start, fixed end" are only ever reached via candidate 2 (always ends up tomorrow-anchored, matching the original code's own behavior for these shapes) — the elapsed-via-required-fits path is real and directly tested (`TestClassifyWindowInstance`) but isn't reachable through the horizon function for this specific shape combination.
- both fixed → elapsed once `now >= end_utc`

**`win_start_utc`/`win_end_utc` are returned unclamped** — they represent the *configured* bounds of whichever instance was targeted, used as-is for the plan's displayed `preferred_window_start`/`preferred_window_end`. `plan_date` is derived from the actual resolved `win_start_utc` (`ws.astimezone(tz).date()`), matching how it has always been defined: the local calendar date the window starts on — for an overnight instance targeting Monday, that's Sunday's date, not Monday's.

Clamping to "never select an elapsed slot" is the caller's job, applied uniformly regardless of which instance was chosen:
- `_plan_one_profile` floors `candidate_prices` to `now_utc` (in addition to the existing `win_start_utc - required_minutes` floor) — this is what makes catching a live window's remainder safe; without it, an elapsed-but-still-known price could otherwise be selected.
- `_check_window_coverage` and the forecast-supplement filter in `_select_slots` both take `now_utc` and clamp their effective window start to `max(win_start_utc, now_utc)` — see "Price acquisition" above.

`_resolve_window_utc` itself is purely mechanical — given a start/end HH:MM and a specific anchor date, it returns UTC bounds with no dependency on the current time at all. Deciding *which* date to anchor to is entirely `_resolve_planning_horizon`'s job; the original auto-anchor heuristic (guessing "today or tomorrow" from the clock, with no day-ahead awareness at all) was the actual delayed-run bug and has been removed rather than left dormant.

---

## Slot selection

`select_charging_windows` dispatches on `max_windows`:

### `max_windows: 1`
Slides a window of `required_hours` over all candidates, picks the cheapest contiguous block (`_best_continuous_window`).

### `max_windows: null` (unlimited, default)
Dynamic programming: finds the globally cheapest combination of blocks covering exactly `required_hours`, with no cap on how many blocks (`_select_with_min_block`).

**DP formulation:**
- State: `dp[i][r]` = minimum cost to schedule `r` more slots from position `i` onwards
- Transitions: skip slot `i`, or start a block of length `k ≥ min_slots_per_block`
- Gap constraint: next block can't start within `min_gap_slots` slots of previous block end
- `max_run[i]` cap: block length is also capped by the longest time-continuous run from index `i` — prevents forming blocks across time gaps created by price ceiling exclusions
- Reconstruction: forward scan finding the **latest** valid block at each step (latest-preferred tiebreaker for equal-price ties)
- Globally optimal — no greedy approximation

### `max_windows: N` (N ≥ 2)
Same DP, extended with a window-count budget (`_select_with_max_windows`):
- State: `dp[w][i][r]` = minimum cost to schedule `r` more slots using at most `w` more window-starts, from position `i` onwards
- Skip transition leaves `w` unchanged; starting a block spends exactly one unit of `w`
- Same `run_end` / `first_valid_after` machinery and same latest-preferred reconstruction as the unbounded case
- `max_windows` is internally capped at `ceil(n_slots / min_slots_per_block)` — the most blocks that could ever be needed — to keep the DP small when configured generously higher than useful
- Complexity: `O(max_windows × n² × n_slots)` — fine for realistic sizes (max_windows in the low single digits, n_slots ≤ ~30)

**`min_slot_minutes`** controls the minimum individual block length — applies in all three cases (for `max_windows: 1` the whole required duration is one block, so it's moot there).

**`min_gap_minutes`** controls the minimum gap between blocks (default 15, divisible by 15, can be 0) — identical semantics across `max_windows: null` and `max_windows: N`.

**Defaults have one source: `CHARGING_DEFAULTS`.** Parsing, validation and the `PlanParams` dataclass read their fallbacks from it rather than restating literals. They used to restate them, and diverged: `CHARGING_DEFAULTS` said `min_gap_minutes: 15` while parsing/validation fell back to `30` and `PlanParams` to `30`, so any path that skipped the defaults merge silently got 30. Pinned by `TestParseConfigs.test_omitted_min_gap_and_min_slot_use_charging_defaults_without_merge`.

**Known gap — spillover ignores a configured `min_gap_minutes`.** `_select_spillover` calls `select_charging_windows` with `min_slot_minutes` deliberately relaxed to one slot, but passes no `min_gap_minutes`, so it always uses that function's own default (15). With the default config this coincides; with `min_gap_minutes: 30`, spillover blocks can sit 15 minutes apart. Unlike the `min_slot` relaxation it isn't clearly deliberate. Undecided whether spillover should honor the configured gap (Guiding Principle 2 suggests yes).

**Graceful degradation when the full requirement can't be reached** — both `_select_with_min_block` and `_select_with_max_windows` fall back to the largest achievable slot count instead of returning nothing, e.g. a live window with only 1h left against a 6h requirement returns that 1h, not an empty plan. Found and fixed after the planning-horizon work: `_best_continuous_window` (`max_windows: 1`) already had this fallback ("return the longest available block"), but the DP paths originally required reaching the *exact* slot count requested — infeasible → `dp[0][n_slots] == INF` → `return []`, discarding perfectly good, cheaper, achievable time. Fix: search downward from `n_slots` for the largest `r` with `dp[...][r] < INF` and reconstruct using that. The resulting shortfall still surfaces normally via `plan_warning`, same as the `max_windows: 1` case always did. Not specific to "required exceeds the window" — the identical bug can be triggered even when the window's raw total span *equals* `required_minutes`, if a price ceiling or naturally gappy real prices fragment the candidates into pieces too small to form valid blocks under `min_slot_minutes`/`min_gap_minutes`; both cases hit the same `dp[...][n_slots] == INF` path and get the same fix.

**Spillover:** When the preferred window doesn't have enough slots, the planner fills the deficit from outside the window (never past `preferred_window_end`). `max_windows: 1` extends the existing block leftward; `null` and `N ≥ 2` both fall back to the cheapest-fill path — spillover slots extend an already-selected block and don't enforce their own window budget. Spillover has no independent awareness of "now" — it's protected purely because `_plan_one_profile` floors *all* candidate prices to `>= now_utc` once, upstream of both the main selection and spillover, so neither can ever reach into elapsed time regardless of which direction they search.

### Retained minutes

Each run reads `data/plan-{name}.json`, counts future `charging: true` minutes, and adds them to `required_hours` before running the DP. Ensures committed charging is never lost if a new plan is built before the previous one completes.

**Invariant:** `total_minutes` in the new plan never exceeds `total_minutes` from the previous plan when all slots are still future.

**Formula:** `min(future_minutes, prev_retained_minutes)` — caps carry-over at the previously retained amount, never grows it.

`retained_minutes` is always written to new JSONs (0 when nothing retained). Optimal calculation uses `required_minutes + retained_minutes` so optimal covers the same total as scheduled.

---

## Plan output

```json
{
  "version": 1,
  "date": "2026-03-30",
  "area": "FI",
  "price_source": "ENTSO-E",
  "timezone": "Europe/Helsinki",
  "generated_at": "2026-03-29T12:27:41+00:00",
  "utc_offset_hours": 3,
  "price_stats": { "min_cents_kwh": 0.45, "avg_cents_kwh": 1.92, "max_cents_kwh": 4.99 },
  "required_minutes": 270,
  "retained_minutes": 0,
  "max_windows": 1,
  "total_minutes": 270,
  "avg_price_cents_kwh": 0.70,
  "avg_optimal_price_cents_kwh": 0.56,
  "preferred_window_start": "21:00",
  "preferred_window_end": "06:30",
  "configured_window_start_utc": "2026-03-29T19:00:00+00:00",
  "schedule_uses_forecast": false,
  "windows": [
    { "start": "22:45", "end": "06:30", "duration_minutes": 270, "avg_price_cents_kwh": 0.70 }
  ],
  "window_starts_utc": ["2026-03-29T19:45:00+00:00"],
  "window_ends_utc":   ["2026-03-30T03:30:00+00:00"],
  "price_slots": [
    { "start_utc": "...", "price_cents_kwh": 0.45, "charging": true, "optimal": true },
    { "start_utc": "...", "price_cents_kwh": 2.05, "charging": false, "optimal": false, "forecasted": true }
  ],
  "plan_warning": null,
  "ocpp_charging_profile": { ... },
  "profile": "overnight"
}
```

Key fields:
- `price_source` — which source provided prices (`"ENTSO-E"`, `"Elering"`, `"Sähkötin"`, `"forecast"`, `"elprisetjustnu.se"`, `"hvakosterstrommen.no"`)
- `price_stats` — min/avg/max across future slots available to the scheduler at run time
- `total_minutes` — `required_minutes + retained_minutes`
- `windows` — local-time charging windows; `window_starts/ends_utc` are the same windows as UTC ISO strings for delivery
- `price_slots` — all slots from previous evening onwards. `charging: true` = scheduled. `optimal: true` = cheapest ignoring window. `forecasted: true` = display-only forecast slot
- `plan_warning` — null or human-readable reason when plan is partial (`"partial plan — price limit X c€/kWh"` or `"partial plan — required hours exceed boundaries"`)
- `ocpp_charging_profile` — OCPP 1.6/2.0.1/2.1 compatible ChargingProfile object for any downstream system
- `generated_at` — when this specific plan was built. Exists specifically to support redundant-delivery protection (below); not otherwise consumed by the planner itself
- `configured_window_start_utc` — the *true, unclamped* window start (see "Window resolution" above) — distinct from `window_starts_utc`, which is the actual delivered slot times and may start later than this if the window was live when the plan was built
- `schedule_uses_forecast` — whether any *scheduled* (`charging: true`) slot specifically relied on forecast data, derived from `price_slots`' own `forecasted`/`charging` flags rather than threaded through as a separate signal. Deliberately more precise than "was forecast data consulted at all during selection" — a coverage check can pull forecast slots into the candidate pool without any of them actually being cheap enough to get selected, in which case the delivered schedule is still fully real-price-based

---

## Delivery

`delivery/deliver.py` is a separate process from the planner (see "Architecture" above) — it reads `plan-{profile}.json` files plus `config.yaml`, and for each `deliveries:` entry, resolves the charge point ID(s) and calls `handler.deliver(plan, charge_point_id, entry, timezone) -> bool` once per resolved charger. All chargers across all profiles are attempted in one run; failures accumulate and are reported via a non-zero exit code (GHA then emails the operator) rather than stopping the whole run at the first failure.

### Redundant-delivery protection

Two triggers close together (e.g. an unreliable GHA `schedule:` cron plus a manual `workflow_dispatch` backup) can each build and deliver a plan for the same profile. Re-delivering isn't free in two distinct ways: it can silently overwrite a manual adjustment made to the charger or vehicle between the two runs (violating Guiding Principle 1 — never interrupt a session already in progress), and it burns real API budget on a request that accomplishes nothing (see "Never make an unnecessary API call" under Design decisions). A skip happens before the handler module is even loaded, so it saves the whole request sequence, not just the resulting charger/vehicle change.

`delivery/deliver.py` keeps a small persisted record per `(profile, handler, charge_point_id)` under `--data-dir` (default `data/`, committed to the repo so it survives across runs): the delivered plan's `window_starts_utc`/`window_ends_utc`, `generated_at`, `configured_window_start_utc`, and `schedule_uses_forecast`. The filename hashes `charge_point_id` (`sha256[:12]`) rather than embedding it directly — for MyŠkoda it's the vehicle's VIN, for Charge Amps a charger serial, both directly identifying, and `data/` is typically public via GitHub Pages alongside the dashboard. **Note:** this only protects records written after the hash was introduced — any plaintext-VIN-named files already committed under the old scheme remain in git history regardless; removing them there requires rewriting history (`git filter-repo`/BFG), not just deleting the current file. Before each delivery attempt, `should_skip_redundant_delivery` checks the (hashed-path) record, in order:

1. **No prior record** → deliver. Nothing to compare against.
2. **Prior schedule used forecast data** → deliver, regardless of window liveness, *unless* this plan is also forecast-based and produced byte-identical windows to the prior one. A forecast-based schedule is an estimate; once real prices are available (or the forecast itself has moved) it should always be allowed to correct it, even if that means interrupting a session the forecast-based plan already started. But two close-together forecast-based triggers with an unchanged result aren't a correction — that's a redundant retrigger before real prices have published yet, and forcing a redelivery there would burn an API call for nothing (falls through to rule 4 instead, which skips it as unchanged).
3. **This run's window is live** (already open — see "Window resolution" above) **and the prior plan predates that same window's start** (`configured_window_start_utc` matches) → skip. The prior plan is an already-committed pre-window schedule; redelivering here risks cutting off whatever it already started. The window-instance match matters: without it, a live run for a *new* window could be wrongly blocked by an unrelated, already-completed plan from a previous cycle (e.g. Monday's completed plan blocking Tuesday's legitimate live delivery).
4. **Otherwise, diff the scheduled windows** against the prior record — identical → skip (nothing changed, nothing to gain from redelivering); different → deliver.

The record is written only after a *successful* delivery — a failed attempt leaves no record, so the next run retries normally rather than being mistaken for "already handled."

**Why identifier-level (`profile`, `handler`, `charge_point_id`), not profile-level.** A profile can deliver to more than one charger — both `deliveries:` entries and `charge_point_id` support lists in the schema. A profile-level record couldn't express Guiding Principle 1 correctly once that happens:
- **Partial failure would get lost.** If charger A succeeds and charger B fails in the same run, each currently gets an accurate independent record — A correctly protected, B correctly retried next time. A profile-level record would have to collapse that into one flag, and either choice (mark "delivered" on any success, or withhold it on any failure) is wrong for the other charger.
- **Independent real-world state would get conflated.** Two chargers under one profile can be in genuinely different states — one mid-session, one idle. A shared record can't distinguish "device A is charging, don't touch it" from "device B already finished, go ahead and correct it."
- **The MyŠkoda rate limit is per-VIN**, not per-profile — so even the API-budget argument for this feature is inherently a per-device concern.

A single-profile, single-charger setup (the common case) can't observe any difference between profile-level and identifier-level granularity — which is exactly why this distinction is easy to get wrong without deliberately designing for the multi-charger case up front.

**The skip *log messages* originally only named the profile** ("Profile 'X': skipping delivery"), which didn't reflect the identifier-level granularity described above — ambiguous about which of X's chargers was actually skipped if there were more than one. Fixed: `should_skip_redundant_delivery` now takes `handler_name`/`charge_point_id` and logs `profile='X' handler='Y' charger='Z'`, matching the format already used by the surrounding "Delivering profile"/"Delivery succeeded"/"Delivery failed" messages. A claim about implementation granularity should be visible in what the implementation actually logs, not just in how it's coded — this was found only by re-reading real production logs after the fact, not by reasoning about the code in isolation.

### Charge Amps handler

The official Charge Amps external API (`eapi.charge.space`) does not support scheduling, override control, or connector state reading — it only exposes basic charger control. Everything used here is only available via the web portal API (`my.charge.space`). `deliver_chargeamps.py` authenticates with the user's own credentials and uses this portal API directly.

**Schedule delivery always switches to Schedule mode.** `isActive: true` → mode switches AND windows are immediately enforced. `isActive: false` → mode switches but windows are NOT enforced. `isActive: true` is correct.

**Override mechanism.** Always reads `isCharging` before delivery. If true, activates schedule override after delivery via `PUT /api/chargepoints/{id}/{connector_id}/schedule/override` — protects the current session. Override clears automatically when the cable disconnects.

**Override error codes treated as success:** `OverridingScheduleExists` (already active), `NoScheduleForConnector` (no active schedule).

**Connector state fields** (confirmed from live API): `isCharging` (bool), `onBySchedule` / `offBySchedule`, `mode` (`"On"` / `"Off"` / `"Schedule"`), `ocppStatus`.

### Easee handler

`deliver_easee.py` written from official Easee API documentation. Untested against real hardware.

Single window → Basic Charge Plan (`POST /api/chargers/{id}/basic_charge_plan`, `repeat: false`). Multiple windows → Weekly Charge Plan (`POST /api/chargers/{id}/weekly_charge_plan`, full 7-day replacement). Day-of-week mapping: `(isoweekday() - 1) % 7` (Monday=0, Sunday=6).

Weekly plan uses UTC times in `"HH:MMZ"` format. All other days cleared — the planner owns the full schedule state.

### MyŠkoda handler

The only handler that delivers to the *vehicle* rather than a charger — a categorically different safety model, since the vehicle's own charging profile is shared state the driver can also see and edit directly in the MyŠkoda app.

**Slot mapping.** The vehicle has exactly `_MAX_VEHICLE_SLOTS = 4` `preferredChargingTimes` slots. Plan window 1 → slot 1, window 2 → slot 2, and so on; unused slots are disabled (`enabled: false`, times preserved unchanged rather than cleared). `max_windows` must be configured to a value between 1 and 4 — `max_windows: null` (unlimited) is rejected outright, even if a given day's plan happens to produce 4 or fewer windows, because an unbounded profile can produce more on a different day and fail unpredictably at delivery time. A defensive runtime check on the plan's actual window count backs this up independently of the config check, in case the two ever drift.

**Active-session detection.** The MyŠkoda API reports that the vehicle is `CHARGING` and which charge mode is active, but not *which slot* is driving the session. When the active mode is `PREFERRED_CHARGING_TIMES`, `_find_active_slot_index` infers the active slot by checking which *enabled* slot's `[startTime, endTime)` window contains the current local time (`_time_in_window`) — a reliable signal specifically because charging under this mode stops exactly at the window's declared end, not because of any weaker heuristic. A unique match → that slot is protected completely untouched (not even its `enabled` flag), and the plan's windows are routed into the remaining slots. No match, an ambiguous match (more than one enabled slot's window contains "now"), or not enough remaining slots to route the plan around the protected one → delivery is skipped entirely for that run rather than risk an unverified guess, the same "skip rather than guess" reasoning behind branch 4 below.

**Four charging-state branches**, in order of how much they trust the vehicle's own current schedule over the new plan:
1. **Not charging** → full delivery: write all plan windows, disable unused slots, set the charge mode.
2. **Charging in `MANUAL`/`TIMER`/`TIMER_CHARGING_WITH_CLIMATISATION`** → `MANUAL` ignores `preferredChargingTimes` entirely (driver-initiated, no schedule involved). `TIMER`/`TIMER_CHARGING_WITH_CLIMATISATION` *do* use `preferredChargingTimes` — but as a soft preference within departure-time-driven charging, not a hard restriction: if the configured departure deadline needs more charging time or a different window than the preferred slots allow, the vehicle overrides them to still make the deadline. Either way, rewriting all 4 slots freely is still safe: it only affects what the vehicle *prefers* going forward, not whatever charging is already in progress under the current session's already-decided timing — the charge mode itself is left unchanged regardless, to avoid disturbing that.
3. **Charging in `PREFERRED_CHARGING_TIMES`** → active-slot detection above; route around the protected slot, don't touch the charge mode.
4. **Charging in an unrecognized mode** → skip entirely. A mode not seen before might use `preferredChargingTimes` in some way not yet understood; guessing is worse than doing nothing for one run.

**Why `max_windows: null` is rejected rather than merely risky** — this mirrors the redundant-delivery-protection design principle of failing predictably: an unbounded profile might work fine for weeks (the DP happens to produce ≤4 windows most days) and then fail unpredictably the one day it doesn't, with an error that gives no hint the *configuration* was the actual problem. Rejecting it at delivery time, every time, with a message naming the actual constraint, converts an intermittent silent failure into a loud, immediate, one-time configuration error.

---

## Design decisions

**Forecast supplement gated to FI** — `fetch_forecast_display_slots` returns `[]` for non-FI areas internally. Non-FI areas with partial prices proceed with what they have.

**OCPP delivery handler removed** — requires direct WebSocket access, incompatible with GHA-first architecture. OCPP ChargingProfile remains in plan JSON for downstream systems.

**Never make an unnecessary API call** — every delivery handler talks to a rate-limited, third-party API (MyŠkoda: 20 requests/hour/VIN, a hard published quota; Charge Amps/Easee: unpublished but real). Redundant-delivery protection (above) checks *before* loading the handler module or making any request at all, not after — a skip costs nothing, not even a login round-trip. This is treated as an architectural constraint, not just an optimization: a design that would work correctly but burn API budget unnecessarily on every redundant trigger is a worse design, even before considering the safety case.

**Never commit directly identifying data to `data/`** — that directory is typically public via GitHub Pages, and the project already has one precedent for getting this wrong (`write_config_json`'s API key redaction, next). A second instance: the redundant-delivery record filenames originally embedded `charge_point_id` directly (a VIN for MyŠkoda, a charger serial for Charge Amps) — fixed by hashing it instead (see "Redundant-delivery protection" above). A VIN isn't a credential, so this was lower-severity than the API key case, but the same principle applies: anything written to a committed, public directory should be checked for what it reveals, not just whether it functions correctly.

**`write_config_json` redacts secrets before writing** — `data/config.json` (committed, dashboard-facing) could previously contain a real `ENTSOE_API_KEY` if it was supplied via environment variable rather than left empty in `config.yaml`, because the in-memory config object was serialized *after* the env-var override was merged in. Fixed to deep-copy and redact `entsoe.api_key` before writing, without mutating the caller's own config (which still needs the real key for fetching).

**ntfy removed** — forecast warning visible on dashboard; delivery failures exit non-zero → GHA emails operator.

**Delivery `enabled` key removed** — it behaved exactly like omitting the entry (the dispatcher skipped it with the same `continue`, no record, no API calls); its only other effect was a dashboard badge reading "off" instead of "—". Commenting an entry out keeps its settings just as well. Deliberately *not* rejected at load time: an old config still containing `enabled: false` is now silently ignored, so that entry **will deliver**. Accepted because the only user's config no longer has the key; if this project ever gains other users, unknown-key validation for delivery entries (config Future work, pain point #2) would close this.

---

## Test suite

431 tests, 3 skipped:
- `test/test_charging_planner.py` (291) — price parsing, window resolution, slot selection DP, gap constraint, spillover, plan building, schedule resolution, retained minutes, area-based fallback chain (unit + integration), console output and GHA summary
- `test/test_deliver.py` (27) — redundant-delivery protection: full decision matrix, persisted-record read/write, `dispatch()`-level integration
- `test/test_deliver_chargeamps.py` (46) — login/cache, connector mode, period fields, period timing
- `test/test_deliver_easee.py` (26) — day-of-week mapping, weekly/basic plan payloads, deliver routing
- `test/test_deliver_myskoda.py` (41) — slot mapping, `max_windows` validation, active-slot detection, all four charging-state branches

See `test/README.md` for the full per-class breakdown.

**The suite must pass with no network access.** Any test that reaches `cmd_plan` must mock every price source it could fall through to — including `fetch_forecast_prices` and `fetch_forecast_display_slots`, not just `fetch_entsoe_prices`. `test_cmd_plan_exits_when_prices_missing` violated this for months: written before forecast supplementation existed, it mocked only ENTSO-E, so the planner fell through to the live forecast API, got real data, and never exited — a failure that looked date-dependent but was network-dependent. Verify by running the suite with outbound sockets blocked (patch `socket.socket.connect` / `socket.create_connection` to raise) whenever a new fallback source is added.

**Cyclomatic complexity** (`radon cc charging_planner.py`): average B (7.8). Functions at C or above:

| Grade | Score | Function |
|-------|-------|----------|
| E | 39 | `_validate_charging_profile` |
| E | 35 | `_select_with_max_windows` |
| E | 33 | `build_plan` |
| E | 32 | `_plan_one_profile` |
| E | 31 | `cmd_plan` |
| D | 29 | `_select_with_min_block` |
| C | 20 | `_resolve_planning_horizon` |
| C | 18 | `_select_spillover` |
| C | 15 | `_resolve_tz`, `print_plan_summary` |
| C | 14 | `select_charging_windows` |
| C | 13 | `_parse_one_profile`, `_best_continuous_window` |
| C | 12 | `_http_request_with_retry`, `fetch_sahkotin_prices`, `fetch_forecast_prices`, `_select_slots`, `_resolve_schedule_window` |

The E-grade functions are large orchestrators (`cmd_plan`, `_plan_one_profile`, `build_plan`) or DP implementations where the complexity is the correctness (`_select_with_min_block`, `_select_with_max_windows` — see "Slot selection" above for why). `_validate_charging_profile`'s complexity is many independent field checks in sequence, not interacting branches — high cyclomatic score, low actual risk.

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
    max_windows: null
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
      - handler: myskoda
        charge_point_id: SKODA_VIN
        api_key_env: SKODA_API_KEY
```

`max_price_cents_kwh` also accepts the string `"avg"` instead of a number — resolves at plan time to that day's average future price, so the ceiling adapts to market conditions rather than needing manual retuning.

---

## Theme candidates

Seven color pairs considered as alternative themes for the dashboard. Current theme uses warm cream + teal.

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

**`config.yaml` simplification** — flagged as the next priority after v2.0.0 ships.

**Guiding question to start from**: what outcome is someone actually trying to achieve, and does the config reflect that — not what the DP needs as inputs. Most of `config.yaml` today is two different kinds of thing, flattened into one list with equal visual weight:
- **What the user actually wants**: "my car should be ready by 7am on weekdays," "charge whenever it's cheap on weekends, I don't care when," "give it a quick top-up sometimes during the day." These map to a real intention — a departure time, roughly how much charge is needed, whether there's a fixed daily rhythm or not.
- **How the DP should go about it**: `min_slot_minutes`, `min_gap_minutes`, `max_windows` are optimization knobs — genuinely useful, but implementation concerns (don't let the charger click on/off every 15 minutes, don't split into more pieces than makes sense) exposed as top-level config because that was the direct route from "the DP needs this parameter" to "here's a YAML key," not because a typical user has an independent opinion about them.

**Hypothesis worth testing** (not a conclusion): an outcome-level surface (departure time, target charge, loose/strict about splitting) with sensible defaults, and today's mechanism-level knobs demoted to an advanced/override tier rather than sitting flat alongside everything else. Needs iteration before committing to anything, not a single redesign pass.

Concrete pain points observed in the current file (`overnight`/`topup` profiles, 62 lines) that any redesign should account for, regardless of which direction it takes:
1. **Top-level window fields duplicate schedule-entry fields with an unstated relationship.** A profile's top-level `preferred_window_start`/`preferred_window_end`/`required_hours` and a `schedule:` entry's own copies of the same fields can say the same thing (as they currently do for both profiles) or diverge — and nothing in the YAML itself explains what the top-level fields are actually *for* once a schedule exists. (They're not vestigial: `_resolve_planning_horizon`'s bare-vs-schedule branch and the day-ahead weekday lookup both depend on them — see "Window resolution" above. But that's implementation detail a config author shouldn't need to know to safely edit their own file.) Someone editing only the top-level fields, expecting them to be the "default," could get a confusing result if a `schedule:` entry silently doesn't match.
2. **Delivery entries mix handler-specific fields into one flat, undocumented-in-YAML dict.** `connector_id`/`max_charging_rate`/`restore_mode` (Charge Amps) vs. `api_key_env`/`set_charge_mode` (MyŠkoda) vs. Easee's own set — no schema distinguishing which keys are valid for which handler, no validation catching a typo'd or misplaced key, relies entirely on `README.md`/`delivery/README.md` for a config author to know what's legal.
3. **Every profile repeats near-identical fields and comments** (`min_gap_minutes`, `max_price_cents_kwh`, etc.) with no shared-defaults mechanism — `CHARGING_DEFAULTS` exists in code but has no YAML-level equivalent an author can lean on to avoid restating the same value and comment per profile.
4. **Unquoted `HH:MM` values misparse.** YAML reads a bare `21:00` as the integer `1260`, which is why every window time must be quoted today. (An earlier version of this note claimed `any` has the same risk — wrong, verified: a bare `any` parses as the string `"any"`. So does a single-string window like `21:00-06:30`, which is why that format needs no quoting at all.)
5. **Window defaults disagree between paths.** When a profile omits its window, the merged load path gives `00:00`–`23:59` (`CHARGING_DEFAULTS`), while `_parse_one_profile`'s own fallback gives `23:45` — and on that un-merged path a missing window is treated as `any`. Different meanings, not just different numbers. Resolved by the redesign making the window required rather than defaulted.

Whatever the eventual shape, it should be validated against real config-authoring friction (dig up how many of the above pain points actually caused a real mistake or question, not just theoretical footguns) before committing to a specific schema change.

**Dashboard can show a plan that was never delivered** — see "Architecture & data flow" above for the mechanism. Usually harmless, since a skipped-as-redundant plan (rule 4) is byte-identical to what's actually running. But rule 3 (live-window protection) is a real gap: the skipped plan reflects a live, time-clamped recompute with different windows than the earlier pre-window plan that's actually delivered and running — so the dashboard would display a schedule that was deliberately never sent, while the vehicle/charger runs something else. Same underlying issue, lower frequency, already existed for plain delivery failures (API error, network issue) before any of this session's work — the publish step has never been conditioned on delivery success.

The fix has the data it needs already: `data/delivered-{profile}-{handler}-{hash}.json` is the ground truth for "what's actually running." Options sketched but not decided:
1. Dashboard reads the delivered record alongside the plan, shows a "not yet delivered" / "showing last delivered plan instead" indicator when they diverge.
2. Dashboard displays the delivered record's windows directly, falling back to the freshly-computed plan only when no delivered record exists yet.
3. Simplest: a "planned" vs "delivered" badge, without changing what's rendered — avoids silent misleading without being maximally informative.

Needs proper design before touching `index.html` — not done as part of the redundant-delivery-protection work itself.

**go-e** — cloud API (`{serial}.api.v3.go-e.io`) works from GHA. Scheduler keys exist in v2 API (`sch_week`, `sch_satur`, `sch_sund`) but the time range object format is undocumented and not found in community reverse-engineering. Blocked until payload structure is discovered from a real charger with a schedule set via the app.

**Wallbox** — weekly recurring schedule model (days bitmask), not per-night. Low priority.

**Zaptec** — not viable. Dynamic current control only, no native schedule delivery.

**SoC-derived required_hours** — not on the roadmap. SoC data stays inside the charger firmware loop; this project uses cloud APIs only.

---

# Changelog

Append-only. One entry per release. For full context see the session log below.

## v2.0.0 — 2026-09-24
- **New:** Redundant-delivery protection (`delivery/deliver.py`). Before each delivery, compares the plan against the last successfully delivered one for the same `(profile, handler, charge_point_id)` and skips when redelivery would be redundant or unsafe: identical windows skipped; a pre-window delivery protected from a later live-window run; forecast-based deliveries always superseded by a real-price or changed plan. Skips happen before the handler loads — no API calls. Records persist under `data/` with hashed charge-point IDs.
- **New:** MyŠkoda delivery handler (`delivery/deliver_myskoda.py`). Maps plan windows to vehicle slots 1–4, disables unused slots, sets `PREFERRED_CHARGING_TIMES`. Detects the slot driving an active `PREFERRED_CHARGING_TIMES` session and routes around it; skips delivery when detection is inconclusive. Requires `max_windows` between 1 and 4. Tested against a real vehicle.
- **Breaking:** `continuous_only` removed, replaced by `max_windows` (`null` = unlimited, `1` = single block, `N` = at most N blocks via a new bounded DP). No backward-compatible alias.
- **Bug fix:** A run firing after a charging window had started skipped straight to the next occurrence, losing the rest of the current window. Now targets the still-open window and plans from now onward; elapsed slots are never selected and no longer count against price coverage.
- **Plan JSON:** New fields `max_windows`, `generated_at`, `configured_window_start_utc`, `schedule_uses_forecast`.
- **Dashboard:** "charging slots" badge shows `continuous` / `optimal` / `max N blocks`; "charger delivery" renamed "delivery".
- **Workflow:** `schedule.yml` exposes `SKODA_VIN` and `SKODA_API_KEY` to the delivery step.

## v1.7.6 — 2026-08-24
- **Bug fix:** Charge Amps delivery failed on Sunday overnight plans where the last window ended on Monday — `to` exceeded the 604800s weekly limit. Periods crossing midnight wrap to `from=0`; periods entirely on Monday shift by -604800. Single PUT, original anchor unchanged.
- **Tests:** 2 regression tests added to `test_deliver_chargeamps.py` (total 46).

## v1.7.5 — 2026-05-24
- **Dashboard:** `niceCeil` computed from visible window max with floor of 6 c€/kWh — off-screen spikes don't compress bars; cheap days stay visually flat.
- **Dashboard:** Negative price bars grow downward from zero baseline; faint zero baseline line when negative prices present.
- **Dashboard:** Zero tick (`0` label on price axis) now tracks zero baseline position — moves up on negative-price days.
- **Dashboard:** Negative bar colour fix — colour now encodes slot role (charging/optimal/unselected) same as positive bars, not price direction.

## v1.7.4 — 2026-04-22
- **Fix:** Python 3.11+ compatibility — invalid Unicode escape sequences inside f-strings in `print_plan_summary` replaced with literal `€`. Python 3.11 raises `DeprecationWarning` for invalid escapes; future versions raise `SyntaxError`.
- **Tests:** 52 new tests covering all previously untested console output — `print_plan_summary`, `_window_bar`, `_gha_fmt_hours`, `_gha_summary_header`, `_gha_summary_profile`, `write_gha_summary`.

## v1.7.3 — 2026-04-22
- **Dashboard:** Negative price bars now render downward from zero baseline in distinct colour; faint zero baseline line shown when negative prices present. Zero price: no bar (correct — zero is zero).
- **Dashboard:** `vs market` sign convention fixed — `scheduled − market` so negative = cheaper (green), positive = more expensive (amber). Consistent with `vs optimal`.
- **Dashboard:** `-0.00` fix in window pill avg prices.
- **Console/GHA:** `vs market` sign convention fix and GHA summary updated to match.
- **README:** Console example and prose updated to absolute c€/kWh format.

## v1.7.2 — 2026-04-21
- **Bug fix:** Elering price unit was EUR/MWh not c€/kWh — divided by 1000 instead of 100. Affected all Elering fallback runs since Session 18; undetected because near-zero test prices were ambiguous.
- **Bug fix:** Warning level audit — `required_minutes rounded up` demoted to INFO; `build_ocpp_charging_profile: no windows` demoted to DEBUG; redundant `_select_slots` partial warning removed.
- **Console:** Avg price and savings lines suppressed when no slots scheduled. `vs market` and `vs optimal` switched from percentage to absolute c€/kWh diff; unified sign convention (negative = cheaper). `-0.00` display artifact fixed.
- **Dashboard:** Blocked plan card shows `no charging scheduled`, `—` hero price, hidden `vs market`, reworded warning. Mode 3 histogram capped at last real price bar for non-FI areas. `vs market`/`vs optimal` absolute diff with consistent sign convention.

## v1.7.1 — 2026-04-13
- **Bug fix:** `_select_with_min_block` violated `min_slot_minutes` when a price ceiling excluded slots on both sides of a cheap slot, creating a time gap the DP did not see. Fixed by precomputing `max_run[i]` and capping block length at it in both the fill and reconstruction passes.
- **Bug fix:** Easee weekly plan day-of-week off by one — `isoweekday() % 7` mapped Sunday→0 (Monday). Fixed to `(isoweekday() - 1) % 7`.

## v1.7.0 — 2026-04-10
- Dashboard histogram price axis now scales to the visible window rather than the global day max.
- Tick algorithm targets 3–5 ticks; extended nice steps for low-price days (0.1, 0.2, 0.25 c€/kWh).
- Float rounding throughout to prevent drift with small intervals.

## v1.6.0 — 2026-04-09
- SE1–SE4 fallback: `fetch_elprisetjustnu_prices` (native 15-min).
- NO1–NO5 fallback: `fetch_hvakosterstrommen_prices` (hourly, expanded to 15-min).

## v1.5.0 — 2026-04-08
- Area-based fallback chain: `_build_fallback_chain(area)` replaces hardcoded try/except in `cmd_plan`.
- Forecast supplement gated to FI areas only.

## v1.4.2 — 2026-04-07
- Bug fix: avg price ceiling was computing average from `display_prices` (including historical slots) instead of future slots only.

## v1.4.1 — 2026-04-07
- Bug fix: JS syntax error in dashboard price limit pill render.

## v1.4.0 — 2026-04-06
- `max_price_cents_kwh: avg` — dynamic ceiling resolving to market average at plan time.
- Elering fallback price source (FI/EE/LV/LT).
- Dashboard histogram price axis with scaled ceiling and tick marks.

## v1.3.1 — 2026-04-05
- Bug fix: `any:any` window could select slots exceeding Charge Amps' 604800-second schedule limit. Planning horizon now caps `any:any` window end.
- Bug fix: forecast display slots not showing in histogram in two edge cases.
- Bug fix: rule 2 supplement now appends forecast after real prices rather than replacing them.

## v1.3.0 — 2026-04-05
- Charge Amps active session protection: reads `isCharging` before delivery, activates schedule override if car is charging.

## v1.2.0 — 2026-04-01
- Plan comparisons: partial plan reason (`plan_warning` field) distinguishes price ceiling from data shortage.
- Console output redesigned to indented layout.
- `CHARGER_EMAIL` → `CHARGER_USERNAME`.

## v1.1.0 — 2026-04-01
- Forecast supplementation for partial window coverage: supplements real prices with forecast rather than skipping the profile.

## v1.0.0 — 2026-04-01
First public release. Charge Amps handler tested and supported.

---

# Session log

Development history. Append-only — entries reflect the state of understanding at the time, not current state.

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

**v1.0.0 released** — first public release tagged on 2026-04-01. Charge Amps handler tested and supported.

**v1.1.0 released** — forecast supplementation for partial window coverage.

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

### Session 17 — 2026-04-06

**`max_price_cents_kwh: "avg"` dynamic ceiling** (v1.4.0):

- `"avg"` (case-insensitive) accepted as a valid value alongside positive numbers and `null`
- `Config` gets `max_price_is_avg: bool = False` field (after `schedule` to avoid dataclass ordering issues)
- Resolved at plan time in `_plan_one_profile` from `display_prices` (horizon-capped real prices) → `resolved_max_price_eur`
- `resolved_max_price_eur` passed as `max_price_override` to `_select_slots` and used throughout slot selection and spillover — `cfg.max_price_eur` stays `None`
- Plan warning shows `"partial plan — price limit avg (X c€/kWh)"` with resolved value when partial
- 7 new tests in `TestAvgPriceCeiling` covering validation, parsing, and end-to-end behaviour
- `config.yaml` and README updated

**Elering price source added** (`fetch_elering_prices`): first fallback after ENTSO-E, covers FI/EE/LV/LT. No API key needed. JSON endpoint `https://dashboard.elering.ee/api/nps/price?fields={fi|ee|lv|lt}&start=...&end=...`. Returns hourly data with `timestamp` (Unix seconds UTC) and `price` (EUR/MWh) — expanded to 4×15-min slots, divided by 1000 for EUR/kWh. Fallback chain is now: ENTSO-E → Elering → Sähkötin (FI only) → nordpool-predict-fi (FI only).

**172 tests** passing.

**Dashboard histogram height doubled** (`index.html`): histogram height increased from 56px to 112px for better readability of price differences between slots. Scaling (10–90%) unchanged.

### Session 18 — 2026-04-07

**Area-based fallback chain dispatch** (not yet implemented — research notes, implemented in Session 19):

The fallback chain should be built dynamically based on the configured area rather than being a single fixed sequence:

```
FI:       ENTSO-E → Elering → Sähkötin → nordpool-predict-fi (forecast)
EE/LV/LT: ENTSO-E → Elering
SE1-SE4:  ENTSO-E → elprisetjustnu.se
NO1-NO5:  ENTSO-E → hvakosterstrommen.no
other:    ENTSO-E only
```

**Potential fallback sources for SE and NO areas** (implemented in Session 20):

- **elprisetjustnu.se** — Sweden (SE1–SE4), free, no auth. URL: `https://www.elprisetjustnu.se/api/v1/prices/{YYYY}/{MM-DD}_{area}.json`. Native 15-min resolution (96 slots/day). Returns `EUR_per_kWh` and `SEK_per_kWh` — only EUR used.
- **hvakosterstrommen.no** — Norway (NO1–NO5), free, no auth. URL: `https://www.hvakosterstrommen.no/api/v1/prices/{YYYY}/{MM-DD}_{area}.json`. Hourly resolution (24 slots/day), expanded to 15-min. Returns `EUR_per_kWh` and `NOK_per_kWh` — only EUR used.

Both use EUR/kWh ex-VAT sourced from ENTSO-E, consistent with every other source in the pipeline. SEK/NOK fields are ignored.

**Dashboard histogram price axis** (v1.4.0):
- Scaled ceiling: `niceCeil` computed as smallest nice number above `maxP` that is a multiple of a nice interval and gives ≤ 4 ticks. Zero is the implicit bottom tick. Nice steps: `[0.5, 1, 2, 5, 10, 25, 50, 100, 150, 200, 500, 1000]`.
- Bars scale from 0 to `niceCeil` (not minP to maxP), so relative prices are visually proportional.
- Price ticks on right side of histogram — sibling flex div, 4px tick mark + label. Zero tick always shown at bottom. Ticks positioned using `top: calc(pct%)` within the axis div.
- Avg line and bar heights use same `niceCeil` scale.
- Histogram layout: flex row with histogram `flex:1` and tick axis `width: auto` (sized by `calc(6px + digits×6px)`).

**v1.4.1 released** — bug fix only.
**v1.4.2 released** — bug fix only.

**Dashboard JS syntax error** (`index.html`): mismatched quote in the price limit pill render — `"` instead of `'` to close a ternary expression caused `node --check` to report "Invalid regular expression flags". Fixed. Going forward: always run `node --check` on extracted JS after edits to `index.html`.

**Bug fix: avg price ceiling used wrong slot pool**: `max_price_cents_kwh: "avg"` was computing the average from `display_prices` (all real slots including historical ones from yesterday evening), which skewed the ceiling artificially low. Fixed to use future slots only — same pool the planner selects from — consistent with `price_stats.avg` shown on the dashboard.

**Elering API format**: ~~prices are in c€/kWh~~ — this was incorrect. Confirmed in Session 25 that the API returns EUR/MWh (same as ENTSO-E), not c€/kWh. The error was undetected in Session 18 because test prices happened to be near-zero, making EUR/MWh values indistinguishable from c€/kWh. Fixed in Session 25: `entry["price"] / 1000.0`. Native 15-min resolution. Response always returns all 4 areas (fi, ee, lv, lt) regardless of `fields` param — correct area selected by key from `data[elering_field]`.

**schedule.yml**: cron updated to `00 11 * * *` (11:00 UTC = 14:00 EEST summer / 13:00 EET winter, GHA delay lands ~14:30 EEST).

**config.yaml updated**: topup `required_hours: 2`, `max_price_cents_kwh: avg`, weekday required_hours `1.5`, weekend `4.5`, `min_gap_minutes: 15`; overnight weekend schedule `any:any` added, `min_gap_minutes: 15`.

### Session 19 — 2026-04-08

**Area-based fallback chain dispatch** (v1.5.0):

`_build_fallback_chain(area)` replaces the hardcoded nested try/except in `cmd_plan`. Returns `[(fetch_fn, source_name)]` tuples — the name is stored alongside the function at build time so it survives `mock.patch` replacement in tests.

Chain by area:
```
FI:        ENTSO-E → Elering → Sähkötin → nordpool-predict-fi (forecast)
EE/LV/LT:  ENTSO-E → Elering
SE1–SE4:   ENTSO-E only  (elprisetjustnu not yet implemented)
NO1–NO5:   ENTSO-E only  (hvakosterstrommen not yet implemented)
other:     ENTSO-E only
```

Both short codes (`FI`, `EE`) and full EIC codes (`10YFI-1--------U`) are recognised so the chain is correct regardless of how `area` is configured.

**Forecast supplement gated to FI**: the block that supplements real prices with forecast when they don't reach tomorrow noon was previously unconditional — it called `fetch_forecast_prices` for any area, which would raise `PricesNotYetAvailable` for non-FI and then exit. Now gated with `cfg0.area.upper() in _FI_AREAS` so non-FI areas with partial price coverage simply proceed with what they have (the window coverage check handles it).

**`_SOURCE_NAMES` dict** maps function names to display names (`"ENTSO-E"`, `"Elering"`, `"Sähkötin"`, `"forecast"`). Used in `cmd_plan` warning messages and `price_source` field in the plan JSON.

**Adding a new area/source** requires two steps: implement `fetch_<source>_prices(area)` raising `PricesNotYetAvailable` on failure, then add an area set and `chain.append()` call in `_build_fallback_chain`. No other changes needed.

**`test_rule3` fix**: was only patching `fetch_sahkotin_prices` and `fetch_forecast_prices` — missed `fetch_elering_prices` which is now between ENTSO-E and Sähkötin in the FI chain. Updated to patch all three real sources.

**38 new tests**: `TestBuildFallbackChain` (18 unit tests for chain composition across all area groups) and `TestAreaFallbackChainIntegration` (20 integration tests running `cmd_plan` with all fetchers explicitly patched). "Never called" tests use `_run_all_patched` which patches all four fetchers directly so `assert_not_called()` operates on the exact mock that `cmd_plan` calls.

**210 tests** passing (172 + 38 new), 3 skipped.

**v1.5.0 released** — area-based fallback chain.

### Session 20 — 2026-04-09

**SE and NO fallback sources implemented** — two dedicated fetch functions, each self-contained:

- `fetch_elprisetjustnu_prices` — Sweden (SE1–SE4). elprisetjustnu.se, native 15-min resolution (96 slots/day). `EUR_per_kWh`, `time_start`, `time_end`. SEK field unused.
- `fetch_hvakosterstrommen_prices` — Norway (NO1–NO5). hvakosterstrommen.no, hourly resolution (24 slots/day), expanded to 4×15-min. Same fields. NOK field unused.

Both return EUR/kWh ex-VAT sourced from ENTSO-E, consistent with every other source in the pipeline. Fetch yesterday, today, and tomorrow separately (one calendar day per request). Tomorrow 404 handled silently.

**Chain updated**:
```
SE1–SE4:  ENTSO-E → elprisetjustnu.se
NO1–NO5:  ENTSO-E → hvakosterstrommen.no
```

`_SE_AREAS` derived from `_ELPRISETJUSTNU_AREAS`, `_NO_AREAS` from `_HVAKOSTERSTROMMEN_AREAS` — EIC codes live in one place. `_build_fallback_chain` has separate `if a in _SE_AREAS` and `if a in _NO_AREAS` branches. `price_source` in plan JSON is `"elprisetjustnu.se"` or `"hvakosterstrommen.no"`.

**7 new tests** in `TestBuildFallbackChain` (chain order, EIC codes, SE≠NO chains differ, no Elering in SE/NO) and 8 new tests in `TestAreaFallbackChainIntegration`.

**217 tests** passing, 3 skipped.

**v1.6.0 released** — SE and NO fallback price sources.

### Session 21 — 2026-04-10

**Dashboard histogram price axis improvements** (v1.7.0):

**Scale from visible slots**: `maxP` is now computed from the slots visible in the current histogram window rather than `plan.price_stats.max_cents_kwh` (the global max from the full selection pool). Off-screen price spikes no longer compress the visible bars.

**Better tick selection**: the algorithm now targets 3–5 evenly-spaced ticks. Previously it found the smallest ceiling with ≤4 ticks — for a visible max of 22 c€/kWh this produced a single tick at 25 with nothing in between. New algorithm picks the interval that maximises tick count in the 3–5 range.

**Extended `niceSteps`**: added `0.1, 0.2, 0.25` for low-price days. `maxP=0.75` now produces ticks at 0.20, 0.40, 0.60, 0.80 instead of collapsing to 0.5 and 1.0.

**Float rounding**: `Math.round(...* 1000) / 1000` applied throughout to avoid drift with small intervals (e.g. `0.8000000000000002`). Tick labels render as integers for whole numbers, decimal for fractional values.

**Tick axis width** fixed to use the formatted label string length rather than `String(niceCeil).length`, which broke for decimal ceilings like `0.80`.

**v1.7.0 released** — histogram price axis improvements.

### Session 22 — 2026-04-13

**Bug fix: `_select_with_min_block` violated `min_slot_minutes` when price ceiling created time gaps in candidate array.**

Root cause: the DP operated on candidate array index positions, not real time positions. A price ceiling (e.g. `max_price_cents_kwh: avg`) can exclude slots adjacent to a cheap slot, leaving it isolated in time but index-adjacent to the next candidate. The DP formed a "block" across the time gap; `_group_continuous` then split it by real time, leaving the isolated slot as a 1-slot (15-min) block — violating `min_slot_minutes=30`.

Triggered on 2026-04-13 by the `topup` profile: avg ceiling 9.85 c€/kWh, slot at 23:45 local (5.40 c€/kWh) isolated by neighbors at 10.48 and 10.30. First occurrence because this specific combination — cheap isolated slot with both neighbors above the avg ceiling — had not appeared in prior runs.

**Fix:** precompute `max_run[i]` = longest time-continuous run from index `i`. Two candidates are time-adjacent iff `ordered[i].end == ordered[i+1].start`. Block length capped at `max_run[i]` in both the DP fill pass and the reconstruction pass.

**Log message:** `log.warning("N block(s) still shorter than 30 min")` → `log.error("DP produced a block shorter than 30 min — this is a bug")`. The "still" wording implied a retry loop that never existed. Now unreachable if the DP is correct.

**1 new test** in `TestSelectWithMinBlock`: `test_isolated_cheap_slot_with_price_ceiling` — reproduces the exact 2026-04-13 scenario, asserts the isolated slot is not selected and no sub-30-min block is produced.

**177 tests** in `test_charging_planner.py`, 3 skipped.

**Easee delivery handler `deliver_easee.py` added** (doc-based, untested against real hardware). Day-of-week bug fixed before first release: `isoweekday() % 7` mapped Monday→1 and Sunday→0 (colliding with Monday). Fixed to `(isoweekday() - 1) % 7`. 26 tests in `test_deliver_easee.py`.

**`README_tests.md` created** — documents all 244 tests across `test_charging_planner.py` (177), `test_deliver_chargeamps.py` (41), and `test_deliver_easee.py` (26). One paragraph per test class describing what it covers; regression tests called out explicitly.

**v1.7.1 released** — min_slot_minutes DP bug fix + Easee day-of-week fix.

### Session 23 — 2026-04-18

**Warning level audit** (`charging_planner.py`): reviewed all `log.warning` and `log.error` calls. Four fixes: `required_minutes rounded up` demoted WARNING → INFO (normal expected behaviour for non-multiple-of-15 input); `build_ocpp_charging_profile: no windows` demoted WARNING → DEBUG (expected on empty/partial plans, not actionable); `⚠ Profile 'X': only N min scheduled` in `_select_slots` removed entirely (redundant — immediately followed by the more informative `plan_warning` log from `_plan_one_profile`); `Supplemented with N forecast slots` confirmed already INFO, no change needed.

**Dashboard histogram Mode 3 fix** (`index.html`): Mode 3 (now-centered ±12h, tracking forward in real time) previously scrolled into empty space for non-FI areas that never receive forecast display slots. Fix: precompute `hasForecastSlots` (any `forecasted: true` slot in the plan) and `lastRealEndLm` (end of last non-forecast slot). In Mode 3 and the no-charging-slots fallback, `rangeEnd` is now capped at `lastRealEndLm` when no forecast slots are present. FI behaviour unchanged. Same cap applied to the no-charging-slots fallback which uses the same now±12h logic.

**First successful live run with `any:any` Sunday window**: topup profile scheduled 4h30m at 1.24 c€/kWh (↓68% vs market avg 3.93 c€/kWh), all slots optimal. Delivery succeeded mid active charging session — schedule override protected the current session, `restore_mode: true` restored connector to On mode, no interruption to charging current observed on load balancer chart.

### Session 24 — 2026-04-20

**Console output fixes** (`charging_planner.py`): two display-only changes, no effect on JSON or selection logic. (1) Avg price and savings lines suppressed entirely when no slots were scheduled (`total_minutes == 0`) — previously showed `0.00 c€/kWh` and `↓ 100% below market avg`, which were artifacts of the zero-division path on a fully-blocked plan. (2) `-0.00` min price display fixed — `-0.001 c€/kWh` rounded to 2 decimal places produced the string `"-0.00"`. Fix checks the formatted string directly and substitutes `"0.00"` only for that exact artifact; real negative prices like `-0.05` display unchanged.

**Dashboard fix** (`index.html`): same `-0.00` fix applied to the min price in the right hero panel using the same string-check approach.

**First live run with `avg` ceiling producing a fully-blocked plan**: topup profile on 2026-04-20 had all window slots above the dynamic avg ceiling (5.45 c€/kWh), spillover also found nothing below ceiling. Plan correctly produced `total_minutes: 0` with `plan_warning: "partial plan — price limit avg (5.45 c€/kWh)"`. Overnight ran normally at 7.00 c€/kWh (no ceiling).

### Session 25 — 2026-04-21

**Dashboard blocked plan display** (`index.html`): improved rendering when `total_minutes == 0`. Header now shows `no charging scheduled` in muted grey instead of `charges <date>`. Hero price shows `—` with no accent colour instead of `0.00` in green. `vs market` line hidden entirely. Warning rewritten from `(price limit avg (5.45 c€/kWh))` to `due to price limit (5.45 c€/kWh)` — strips the `avg` keyword and redundant outer parens, shows the resolved numeric value which is more useful than the config keyword. Regex extracts the price from both static and dynamic avg ceiling warning strings. `-0.00` min price display fix also applied to dashboard hero (same string-check approach as console). JS syntax checking (`node --check` on extracted script block) added as mandatory step before presenting `index.html`.

**First live observation of fully-blocked plan** on 2026-04-20: topup profile blocked by `avg` ceiling (5.45 c€/kWh), all window slots above ceiling. Dashboard rendering revealed the display issues fixed this session.

### Session 26 — 2026-04-21

**Bug fix: Elering price unit incorrect** (`charging_planner.py`): `fetch_elering_prices` was dividing by 100 assuming c€/kWh, but the API returns EUR/MWh. Fixed to divide by 1000 (EUR/MWh → EUR/kWh), consistent with ENTSO-E. The error was introduced in Session 18 where live confirmation happened on a near-zero price day, making EUR/MWh values (0.3, 0.5 EUR/MWh) indistinguishable from c€/kWh — the bug only became visible on 2026-04-21 when ENTSO-E was down and Elering was the fallback, with historical slots at 38–92 EUR/MWh clearly wrong as c€/kWh. Session 18 note in CONTEXT corrected with strikethrough. Reference section was already correct (original assumption in Session 17 was EUR/MWh). Confirmed via external API documentation: Elering returns EUR/MWh ex-VAT.

**ENTSO-E 503 on two consecutive days** (Apr 20–21): fallback chain working correctly — Elering used both days. Apr 20: plan produced but prices were wrong (bug above, not yet fixed). Apr 21: bug fixed, prices correct.

### Session 27 — 2026-04-21

**`vs market` and `vs optimal` switched to absolute c€/kWh difference** (both `charging_planner.py` and `index.html`): percentage form breaks when prices are near zero or negative — `↓102%` and `↓105%` appeared on today's near-zero price day. Absolute difference is unambiguous at any price level: `vs market -3.05 c€/kWh`, `vs optimal +0.28 c€/kWh`. Positive = you pay more than the reference, negative = you pay less. Both metrics now use the same unit for consistency. `savings()` function in dashboard rewritten to return absolute diff. GHA summary updated to match. Console threshold of 0.005 c€/kWh suppresses noise-level `vs optimal` display. `-0.00` avg price artifact fixed in console hero using existing `_fmt_price` helper.

**First observation of near-zero/negative price day**: Elering bug fix (Session 26) confirmed correct — today's topup scheduled at -0.007 c€/kWh avg, overnight at 0.009 c€/kWh, both from genuinely near-zero Finnish day-ahead prices on 2026-04-21/22.

### Session 28 — 2026-04-21

**Sign convention fix for `vs market`** (`charging_planner.py`, `index.html`): `vs market` was using `market - scheduled` (positive = cheaper) while `vs optimal` uses `scheduled - optimal` (positive = more expensive). Unified both to `scheduled - reference` so negative always means cheaper and positive always means more expensive, consistent with `vs optimal`. Green for negative, amber for positive in both console and dashboard. GHA summary updated to match. Duplicate line removed from console output path.

**`README.md` updated**: console example block and two prose references updated from percentage format (`↓ 62% below market avg`, `vs optimal ↑N%`) to absolute format (`vs market -1.02 c€/kWh`, `vs optimal +X.XX c€/kWh`).
### Session 29 — 2026-04-22

**`vs market` sign convention fix** (`charging_planner.py`, `index.html`): `vs market` was using `market - scheduled` (positive = cheaper) while `vs optimal` uses `scheduled - optimal` (positive = more expensive). Unified to `scheduled - reference` throughout — negative = cheaper (green), positive = more expensive (amber). GHA summary updated. Duplicate line removed from console output path.

**Negative price bar rendering** (`index.html`): negative price slots now render as downward-growing bars from the zero baseline in `--accent2` colour. Zero price slots render as no bar (zero is zero — not special-cased). Faint zero baseline line added when `minP < 0`. Positive bars anchor at `bottom: zeroBottomPct%` — identical to `bottom: 0` on all-positive days, correct on mixed days. `topPct`/`bottomPct` unused variables removed.

**`-0.00` fix in window pills** (`index.html`): same string-check guard applied to `w.avg_price_cents_kwh.toFixed(2)` in pill rendering.

**v1.7.3 released.**

### Session 30 — 2026-04-22

**Python 3.11+ compatibility fix** (`charging_planner.py`): two invalid Unicode escape sequences inside f-strings in `print_plan_summary` replaced with literal `€`. Python 3.11 raises `DeprecationWarning` for such escapes; future versions will raise `SyntaxError`. Output was correct at runtime but the source was non-compliant.

**Console output test suite** (52 new tests, `test/test_charging_planner.py`): six new test classes covering all previously untested output functions. `TestPrintPlanSummary` — header fields, market price stats, charging window count and times, savings vs market (below/above/near), optional fields (retained minutes, plan warning, vs-optimal line), ANSI colour suppression and enabling. `TestWindowBar` — bar block length including minimum-2 floor, duration formatting for hours+minutes/exact hours/minutes-only, price label. `TestGhaFmtHours` — all three duration format branches. `TestGhaSummaryHeader` — all markdown fields including UTC offset. `TestGhaSummaryProfile` — profile name, required hours, window table, no-windows message, incomplete-plan warning, savings amount. `TestWriteGhaSummary` — no-op when env var absent, file write, skipped-profiles section, graceful `OSError` handling. Standard library imports `contextlib`, `io`, `os`, `tempfile` added.

**229 tests** in `test_charging_planner.py`, 3 skipped. **296 total** (229 + 41 + 26).

**v1.7.4 released.**

### Session 32 — 2026-08-15

**Bug fix: Charge Amps 604800s weekly limit exceeded for Sunday overnight plans** (`delivery/deliver_chargeamps.py`): the `smartChargingSchedules` API rejects any period where `to > 604800s` (7 days from the Monday anchor). This was triggered when a plan's last window ended on Monday local time — e.g. an overnight slot Sun 23:45 → Mon 01:00, or any:any Saturday plans with Monday slots. Fix: for periods where `to > 604800`, two cases: (1) window crosses Sunday→Monday midnight — `from=0, to=original_to-604800`, landing the Monday portion at the week start; (2) window falls entirely on Monday — both `from` and `to` shifted by -604800, preserving the correct offset within Monday. Single PUT, original anchor unchanged. Charge Amps silently dropped the 15-minute 23:45→00:00 remnant (below their 30-minute minimum), which is expected. 2 new tests added (`test_window_crossing_monday_midnight_wraps_to_zero`, `test_monday_window_from_any_any_plan`), 2 existing tests updated. chargeamps test count: 44 → 46.

### Session 33 — 2026-09-14

**New delivery handler: `deliver_myskoda.py`** — delivers charging plan to a Škoda EV via the official MyŠkoda Public API (`public.api.connect.skoda-auto.cz`). Auth via `X-API-Key` header (key from MyŠkoda app, stored in `SKODA_API_KEY` secret). GET vehicle charging profiles, update slot 4 of the named profile with the planned window (local `HH:MM`), disable slots 1–3, PUT the complete profile back, then set charge mode to `PREFERRED_CHARGING_TIMES`. Only compatible with `continuous_only: true` plans (single window). Rate limit: 20 req/hour, delivery uses 3. Profile name configurable via `profile_name` — optional when vehicle has exactly one charging profile (auto-selected with a log message), required when multiple profiles exist (clear error if omitted). Finnish vehicle had profile named "Koti" not "Home". `SKODA_VIN` and `SKODA_API_KEY` added to `schedule.yml` delivery step. First live delivery succeeded on 2026-09-14 (vehicle not at home at time of delivery — behaviour on arrival to be observed). First live delivery confirmed correct in MyŠkoda app (slot 4 set, slot 1 disabled, charge mode updated). Vehicle was not at saved location at time of first delivery — API returned 202 but charge mode did not stick. Second delivery with vehicle at home confirmed both preferred time window and charge mode set correctly. Observed: when vehicle is away from home, API accepts the update but vehicle ignores or reverts it on arrival. Charging state awareness added: handler checks `charging.status.state` and `charging.settings.preferredChargeMode` before delivery — skips mode change when vehicle is actively charging; preserves slots 1–3 when charging in PREFERRED_CHARGING_TIMES mode to protect the active session; skips entirely for unknown charging modes. **v2.0.0 drafted but not yet released** — running in production under observation; version bump pending confirmation of at-home charging behaviour and other edge cases.

### Session 34 — 2026-09-14

**`deliver_myskoda.py` improvements**: (1) Charging state awareness — handler reads `charging.status.state` and `charging.settings.preferredChargeMode` from the GET response and branches: not charging → full delivery; charging in MANUAL/TIMER/TIMER_CHARGING_WITH_CLIMATISATION → update slot 4 + disable slots 1–3 but skip mode change; charging in PREFERRED_CHARGING_TIMES → update slot 4 but preserve slots 1–3 and skip mode change (one of the slots is driving the active session); unknown mode while charging → skip entirely. (2) `set_charge_mode` changed from bool to mode string or `false` — accepts any valid MyŠkoda charge mode, passed through as-is, default `PREFERRED_CHARGING_TIMES`. (3) `profile_name` now optional when vehicle has exactly one charging profile — auto-selected with log message. (4) Confirmed: midnight-crossing window (21:00–01:00) works correctly in MyŠkoda app. Confirmed: when vehicle not at saved location, API returns 202 but changes do not stick; when at home, both preferred time window and charge mode set correctly. `max_windows` generalisation added to Future work section.

### Session 35 — 2026-09-24

v2.0.0 released. Replaced `continuous_only` with `max_windows` (new bounded DP `_select_with_max_windows`); generalized the MyŠkoda handler from slot-4-only to slots 1–4 with time-window-based active-slot detection; added redundant-delivery protection to `deliver.py` after unreliable GHA cron timing (up to 11h late) prompted a second, manual trigger. Fixed the delayed-run bug with a new `_resolve_planning_horizon`, then fixed a regression it introduced in production (2026-09-20: a weekend `any`/`any` entry masked Monday's fixed window because schedules must be indexed by day-of-use, not by the date a window starts). Found and fixed before release: `write_config_json` writing an env-supplied ENTSO-E key into `data/config.json`; plaintext VINs/charger serials in delivered-record filenames (now hashed; old names remain in git history); the DP returning an empty plan instead of a partial one when the exact slot count was unreachable; skip logs not naming the handler/charger; and `test_cmd_plan_exits_when_prices_missing`, long mislabeled "date-dependent", which was actually hitting the live forecast API — the suite now passes fully offline. Added Guiding principles to the top of this file, restructured the Reference section around data flow with systematic "why" coverage, and rewrote README around the planner/delivery separation. All four skip rules and the live-window planning path were verified against real production runs. 428 tests (was 296), 3 skipped. Next: `config.yaml` simplification (see Future work).
