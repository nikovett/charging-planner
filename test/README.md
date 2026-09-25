# Test Suite

482 tests across five files. Run from the repo root:

```
PYTHONPATH=.:test:delivery python -m unittest test_charging_planner test_deliver test_deliver_chargeamps test_deliver_easee test_deliver_myskoda -v
```

Or individually:

```
PYTHONPATH=.:test:delivery python -m unittest test_charging_planner -v       # 338 tests, 3 skipped
PYTHONPATH=.:test:delivery python -m unittest test_deliver -v                # 31 tests
PYTHONPATH=.:test:delivery python -m unittest test_deliver_chargeamps -v     # 46 tests
PYTHONPATH=.:test:delivery python -m unittest test_deliver_easee -v          # 26 tests
PYTHONPATH=.:test:delivery python -m unittest test_deliver_myskoda -v        # 41 tests
```

`PYTHONPATH` is needed because the test modules import the planner and handlers directly by module name. The 3 skipped tests (`TestOcppChargingProfile`) validate the built OCPP charging profile against the real OCPP 1.6/2.0.1/2.1 JSON schemas — they skip gracefully (`self.skipTest`) when the local schema file isn't present in the environment, rather than failing or trying to fetch it over the network.

---

## test_charging_planner.py (338 tests)

#### Config

### TestConfigValidation (18)
Validation of `config.yaml` fields: required keys, type checks, range checks for `required_hours`, `min_slot_minutes`, `min_gap_minutes`, `max_price_cents_kwh`, `preferred_window`, and `max_windows` (null/positive-int accepted; zero, negative, float, bool, and string rejected — `bool` is a subclass of `int` in Python, so it needs an explicit check).

### TestParseConfigs (15)
Profile merging over `CHARGING_DEFAULTS`, schedule parsing, multi-profile configs, weekday/default fallback, `any` window, duplicate day detection. Plus defaults regressions: `min_gap_minutes` defaults to 15; parsing a profile that omits `min_gap_minutes`/`min_slot_minutes` through the un-merged path yields the `CHARGING_DEFAULTS` values (it used to yield 30 for the gap); `PlanParams` field defaults match `CHARGING_DEFAULTS`.

### TestParseDayKey (9)
`_parse_day_key`, the config-translation day-range parser: single day, forward range, two-day range, comma list, mixed range-and-list, case-insensitivity, and rejection of a backward range, an unknown day, and an unknown day inside a range.

### TestParseWindowString (9)
`_parse_window_string`: `any` (including case-insensitive), a normal `HH:MM-HH:MM` range, a same-day range, and rejection of a bare `HH:MM` with no dash and a non-string value.

### TestTranslateConfig (21)
`translate_config`, the user-facing `config.yaml` format (`profiles:`, `schedule: {mon-fri: {...}}`, `delivery:`) to the internal shape `parse_configs` has always consumed. No `profiles:` key returns the input unchanged (nothing to translate); the old `charging:` key is rejected outright, no backward compatibility; the `entsoe:` block is built from top-level `area`/`timezone`; schedule entries expand to full day names with their window and `required_hours`; a schedule missing coverage for any day of the week is rejected, as is an entry missing `required`; optional profile settings (`max_windows`, `min_slot_minutes`, `min_gap_minutes`) pass through when set and are omitted entirely when not (letting `CHARGING_DEFAULTS` supply them downstream); `price_limit`'s three forms (`none` — the literal YAML string, not Python `None`, `avg`, a number) map correctly to `max_price_cents_kwh`; `delivery:` entries translate through the per-handler key-alias table (MyŠkoda's `vin`, Charge Amps' and Easee's shared `charger`/`connector`/`max_amps`) while a handler with genuinely no alias table entry (a future handler not yet added) passes its keys through unchanged; multiple delivery entries and a malformed (not single-key) entry are both handled correctly; two end-to-end tests write a real temp YAML file and load it through `load_config` itself — one confirming the full translation happens correctly, one confirming the old format is rejected at that entry point too, not just when calling `translate_config` directly.

### TestAvgPriceCeiling (7)
`max_price_cents_kwh: avg` — validation accepts it, case-insensitive parsing, `max_price_is_avg` flag set, resolves to market average at plan time, numeric ceilings leave flag false.


#### Price acquisition

### TestXmlParsing (10)
ENTSO-E XML parsing: slot count, 15-min duration, sort order, ordinal sequencing, no duplicate starts, forward-fill between explicit points, resolution detection.

### TestRealEntsoEData (12)
Integration tests against a bundled ENTSO-E XML fixture: prices in plausible range, known peak price, `min_slot_minutes` respected, overnight windows stay within window, real-world slot selection.

### TestPriceSourceRules (9)
Price source selection rules (rules 1–4): real prices used when sufficient, forecast display appended, forecast supplement used when window not covered, `price_source` field set correctly, supplement slots tagged `forecasted: true`.

### TestBuildFallbackChain (21)
`_build_fallback_chain` composition for every supported area: FI, EE, LV, LT, SE1–SE4, NO1–NO5, unknown area. EIC code equivalence, chain order, cross-area assertions (SE chain ≠ NO chain, no Elering in SE/NO).

### TestAreaFallbackChainIntegration (24)
`cmd_plan` with all fetchers patched: for each area family (FI, EE, SE1, NO1) — ENTSO-E success, each fallback tried in order when prior fails, sources that should never be called are asserted not called, plan exits when all sources fail.

### TestForecastDisplayReuse (3)
Regression: `fetch_forecast_prices` (the scheduling fallback, fetched uncapped from "now") and `fetch_forecast_display_slots` (histogram padding, a narrow 24h window) both hit the same `nordpool-predict-fi` endpoint — the display fetch's range is always a strict subset of what the fallback already retrieved. `cmd_plan` used to make both calls unconditionally, wasting a full network round-trip whenever the fallback genuinely triggered (the common case for a weekend `any`/`any` entry before next-day prices publish). Confirms the display fetch is skipped and the already-fetched data still reaches the plan JSON's forecast slots when the fallback ran; confirms the separate display fetch still happens exactly once when the fallback did not run. Mutation-checked: disabling the reuse path makes the "skipped" test fail as expected.


#### Window resolution

### TestResolveScheduleWindow (5)
Selects the correct schedule entry for the current day, falls back to default, rejects duplicate days and invalid day names.

### TestHhmmToUtc (4)
Converts `HH:MM` local strings to UTC datetimes, positive/negative offsets, overnight handling.

### TestIsOvernight (2)
Detects overnight windows (end ≤ start) vs same-day windows.

### TestResolveWindowUtc (4)
Resolves overnight and same-day HH:MM windows to UTC start/end datetimes for a specific anchor date. Purely mechanical — no dependency on current time (see TestResolvePlanningHorizon for the "which date" decision).

### TestResolvePlanningHorizon (16)
The full scenario matrix for `_resolve_planning_horizon`: bare (no schedule/`any`-flag) profiles check today's own occurrence directly, then tomorrow as fallback. Schedule/`any`-flag profiles index by day-of-use (a schedule entry describes the session that gets the car ready for *that* day — for an overnight shape, its window actually starts the evening before), checking today's own entry (overnight-tail only) then tomorrow's entry (the normal, day-ahead target). Covers a real production regression: on a Sunday with a weekday-overnight/weekend-`any` schedule, using Sunday's own trivially-"live" `any`/`any` entry meant Monday's fixed window was never considered — `test_schedule_regression_weekend_any_does_not_mask_weekday_overnight` reproduces this exactly. Also covers the original delayed-run fix (a run firing after a window's start used to skip straight to the next occurrence instead of catching the still-open remainder). `test_any_end_bound_by_realistic_price_data_not_plan_horizon` — every other test in this class uses a fixture that deliberately makes `plan_horizon` the binding constraint on `any_end_cap`; this one uses realistic, near-term price coverage instead (matching how day-ahead prices actually publish — roughly today + tomorrow) and confirms the *actual* last real price slot is what binds an `any`/`any` window's end, not the generic horizon ceiling. Mutation-checked: removing the real-price bound from `any_end_cap`'s computation makes it fail as expected.

### TestClassifyWindowInstance (3)
Direct tests of `_classify_window_instance`'s "fixed start, `any` end" elapsed behavior (required minutes no longer fitting before `any_end_cap`) — a real, correct capability that isn't reachable through `_resolve_planning_horizon` for this specific shape combination (it's always tomorrow-anchored there, matching the original code's own behavior), so it's covered directly instead.


#### Slot selection

### TestFilterPreferredWindow (5)
Splits a slot list into inside/outside the preferred window. Overnight windows, slots on window boundaries, `any` window sentinel.

### TestSelectChargingWindows (7)
High-level slot selection: cheapest slots selected, required minutes met, price ceiling respected, `max_windows: 1` returns one block, `min_slot_minutes` enforced.

### TestBestContinuousWindow (3)
Returns the cheapest continuous run; respects temporal continuity (index adjacency is not sufficient); handles partial coverage.

### TestSelectSpillover (5)
Spillover from outside the preferred window: not triggered when window is satisfied, stays before window end, `max_windows: 1` extends leftward, handles remaining < min slot.

### TestSelectWithMinBlock (18)
Direct tests for `_select_with_min_block` (the `max_windows: null`, unbounded path): no blocks shorter than minimum, isolated cheap slot replaced, total minutes correct after disqualification, latest slot preferred on equal price, real price data, gap constraint respected, and window-coverage/`cmd_plan` exit-code checks that share this class — including `now_utc` clamping the coverage-check denominator and forecast-supplement filter to a live window's still-useful portion (a live window's already-elapsed time must never register as "missing" coverage, and a forecast supplement must never backfill it). Plus a real production regression: when the full requested slot count can't be reached, returns the largest achievable partial selection instead of nothing — previously an infeasible exact-match meant an entirely empty plan even when perfectly good, cheaper time was available; the partial result is confirmed to still respect `min_slot_minutes` on each block, not just grab whatever's cheapest.

**`test_isolated_cheap_slot_with_price_ceiling`** — regression for the 2026-04-13 production bug: a cheap slot isolated by two above-ceiling neighbours must not be selected when it cannot form a valid block.

**`test_cmd_plan_exits_when_prices_missing`** — mocks both forecast sources as unavailable, not just ENTSO-E. Without that it fell through to the live forecast API over the network, built a valid plan, and failed — long misdiagnosed as date-dependent. The whole suite now passes with the network blocked.

### TestSelectWithMaxWindows (11)
Direct tests for `_select_with_max_windows` (the `max_windows: N ≥ 2` path): uses at most N blocks, picks the cheapest N clusters over more expensive ones, `max_windows: 1` matches `_best_continuous_window` exactly, `max_windows: null` matches the unbounded path exactly, a generously high `max_windows` also matches the unbounded path, `min_gap_minutes`/`min_slot_minutes` enforced identically to the unbounded case, infeasible window budgets return `[]` cleanly, latest-slot tiebreak on equal price. Same graceful-degradation fix as `TestSelectWithMinBlock` above, verified for the bounded-window-count DP specifically.


#### Plan output

### TestBuildPlan (13)
`build_plan` output structure: required keys present (including `max_windows`), price stats, windows, OCPP profile, `max_windows` defaults to `null` and reflects the configured value. `generated_at` and `configured_window_start_utc` reflect the passed-in values (or `null` when not provided) — these feed `delivery/deliver.py`'s redundant-delivery protection. `schedule_uses_forecast` is derived from whether any *scheduled* (not just any available) slot is forecast-sourced, distinguishing "forecast data was consulted" from "forecast data is actually in the delivered schedule."

### TestOcppChargingProfile (13)
OCPP 1.6, 2.0.1, and 2.1 profile generation: schema validity, `validFrom`/`validTo` match window bounds, periods ordered, `startPeriod` offsets correct for single and multiple windows, duration covers full span.

### TestWriteConfigJson (4)
`write_config_json` secret redaction — regression coverage for a real bug where a real `ENTSOE_API_KEY` merged in from the environment could be written into the committed `data/config.json`. Real key redacted, empty key stays empty, caller's in-memory config not mutated, other fields (including `max_windows`) preserved.


#### Display / reporting

### TestPrintPlanSummary (23)
Console plan summary output: header fields, market price stats, charging window count and times, savings vs market (below/above/near), optional fields (retained minutes, plan warning, vs-optimal line), ANSI colour suppression and enabling.

### TestWindowBar (8)
`_window_bar` rendering: bar block length including minimum-2 floor, duration formatting for hours+minutes/exact hours/minutes-only, price label.

### TestGhaFmtHours (3)
Duration formatting for GitHub Actions summaries: hours+minutes, exact hours, minutes-only.

### TestGhaSummaryHeader (7)
GHA step-summary markdown header: date, area, price source, UTC offset, and related fields.

### TestGhaSummaryProfile (7)
GHA step-summary per-profile section: profile name, required hours, window table, no-windows message, incomplete-plan warning, savings amount.

### TestWriteGhaSummary (4)
`write_gha_summary`: no-op when `GITHUB_STEP_SUMMARY` is unset, writes to file, skipped-profiles section included, graceful `OSError` handling.


#### Integration

### TestEndToEnd (11)
Full `cmd_plan` run with mocked prices: one plan file per profile, required keys, OCPP profile present, JSON written to output dir, exits cleanly when prices unavailable. Plus five delayed-run regression tests: a run firing mid-window still targets tonight's live window rather than skipping to the next night; with an artificially-cheap already-elapsed slot planted to tempt the DP, confirms it's never selected; a live window with too little time left to fit `required_hours` still uses 100% of what remains (never rolls to the next occurrence) and honestly reports the shortfall via `plan_warning`; the companion case — a live window with a comfortable 1h buffer over `required_hours` — produces a complete plan with no warning, confirming the shortfall handling above is specific to genuine insufficiency, not just running late; the same too-little-time scenario repeated with `max_windows: null` (the actual default) instead of `1` — the DP behind it used to return a completely empty plan in this situation rather than the same graceful partial result `max_windows: 1` already produced.

### TestLogVerbosity (4)
A normal run's log used to repeat the same handful of facts across four separate `INFO` lines (the target window in UTC, then again in local time with candidate counts, the required-minutes figure re-derived as a multiplication, and the scheduled total/average price/window count) before `print_plan_summary` printed those same three numbers again in the pretty console block immediately after. All four demoted to `log.debug` — confirms they're genuinely absent from a normal (`INFO`-level) run and still present with `--debug` enabled, so nothing was deleted, just quieted. One exception: spillover (minutes scheduled outside the preferred window) isn't shown anywhere else, so it stays at `INFO`, split into its own line — confirms it's reported when spillover genuinely happens and silent when it doesn't. Mutation-checked: reintroducing one demoted line at `INFO` level makes the absence test fail as expected.

---

## test_deliver.py (31 tests)

The dispatcher itself — primarily redundant-delivery protection (see CONTEXT.md "Redundant delivery protection" for the full rationale).

### TestDeliverModuleConfigLoading (4)
Regression coverage for a real bug: `deliver.py` used to have its own, completely independent `load_config` — a bare `yaml.safe_load` never calling `charging_planner.translate_config` — so when `config.yaml` moved to the new `profiles:` format, the planner's own `load_config` translated correctly but `deliver.py`'s separate copy silently found zero delivery entries for every profile, no error, just "No delivery entries found." Confirms there's exactly one `load_config` in the codebase (`hasattr(deliver, "load_config")` is `False`; `deliver.cp.load_config is charging_planner.load_config`); confirms a new-format config loaded via `deliver.py`'s own imported `load_config` resolves delivery entries correctly. The critical one: `test_real_subprocess_finds_deliveries_with_new_format_config` actually runs `delivery/deliver.py` as a real subprocess (`python3 delivery/deliver.py ... --config ...`, matching both the GHA workflow and manual use) — every other test in this file calls into `deliver`'s functions directly within an already-imported process, which shares whatever module state already exists and would never have caught this; only a real subprocess exercises `deliver.py`'s own `sys.path`/import resolution independently. Mutation-checked: temporarily reintroducing the old duplicate `load_config` makes all four tests fail as expected.

### TestShouldSkipRedundantDelivery (15)
The full decision matrix for `should_skip_redundant_delivery`: no prior record delivers; a forecast-based prior schedule yields to a real-price-based one or a differently-forecasted one, including when the new run is live mid-window (forecast-override beats live-window protection); two close-together forecast-based runs with byte-identical windows do *not* force a redundant redelivery just because the prior was an estimate — confirmed against real production data (a manual trigger before ENTSO-E's publish time, followed by a simulated second trigger moments later); a live run whose target window matches a prior plan that predates that same window's start is blocked (protects an already-committed pre-window schedule) — also confirmed against real production data (a manual trigger fired 11 minutes into the window); the same protection does *not* apply across different window instances (the Monday-completed / Tuesday-live scenario — a stale, unrelated prior plan must never block a legitimate new delivery); a non-live run with a pre-window prior falls through to the plain diff instead; identical scheduled windows skip, different windows deliver (both start and end compared independently); a record missing the newer timestamp fields degrades gracefully to diff-only rather than crashing; live-window protection is confirmed independent of plan completeness — a fully-satisfied, warning-free plan (no `plan_warning` key present at all) is still protected exactly like a partial one would be, since the function never inspects `required_minutes`/`total_minutes`/`plan_warning`. Plus two tests (using `assertLogs`, not just the return value) confirming the skip log actually names the specific `handler`/`charger` that was skipped, for both the rule-3 (live-window) and rule-4 (identical) skip messages — real production logs showed "Profile 'X': skipping delivery" with no handler/charger named at all, which the decision itself has always been scoped by but the log output didn't reflect.

### TestDeliveredRecordPersistence (7)
The persisted record file: round-trip read/write, missing file returns `None`, corrupt JSON returns `None` (logged, not raised), the charge-point ID (a VIN or charger serial — directly identifying, and this file is committed to a typically-public `data/`) never appears in the filename, the hash is deterministic across calls with the same inputs, distinct chargers get distinct records, the data directory is created if it doesn't exist yet.

### TestDispatchRedundantDelivery (5)
End-to-end through `dispatch()` with a mocked handler: a second identical run never calls the handler a second time; a changed plan does redeliver; a failed delivery leaves no record, so a retry is attempted normally rather than being mistaken for "already handled"; a forecast-based prior plan is superseded by a real-price-based one even with otherwise-matching windows; an identical forecast-based rerun (both before real prices publish) does not redeliver.

---

## test_deliver_chargeamps.py (46 tests)

### TestAnchorCalculation (5)
Monday anchor is always Monday 00:00 local time expressed as UTC, across timezones and DST transitions.

### TestPeriodTiming (6)
`schedulePeriods` offsets are correct seconds from anchor for single and multiple windows.

### TestPeriodFields (6)
Each period has required fields, correct types, unique IDs, `from` < `to`.

### TestBuildPeriodsEdgeCases (7)
Empty plan, `Z`-suffix timestamps, consecutive windows. Regression tests for the 604800s weekly limit: `test_window_crossing_monday_midnight_wraps_to_zero` (Sun 23:45→Mon 01:00 wraps to from=0), `test_monday_window_from_any_any_plan` (Monday-only slot preserves offset), `test_normal_window_no_wrapping` (normal weekday unchanged).

### TestDeliver (14)
Full `deliver()` call: returns true on success, false on login/GET/PUT failure, passes connector ID and rate from config, reads charger state, mode restore behaviour (was On/Off/Schedule), override not activated when not charging.

### TestGetConnectorMode (5)
Extracts connector mode from chargepoint dict: On, Off, Schedule, missing connector, empty connectors list.

### TestLoginCache (3)
Token cached after first call, second call skips login, missing credentials raises.

---

## test_deliver_easee.py (26 tests)

### TestWeeklyPlanDayOfWeek (8)
One test per day of the week (Monday=0 through Sunday=6).

**`test_sunday_not_monday`** — regression for the pre-release bug: `isoweekday() % 7` mapped Sunday→0, colliding with Monday. Fixed to `(isoweekday() - 1) % 7`.

### TestWeeklyPlanPayload (7)
Payload structure: all 7 days present, non-target days empty, `isEnabled: true`, time format (`HH:MMZ`), `chargingCurrentLimit`, two windows same day, two windows different days (midnight split).

### TestBasicPlanPayload (3)
Field values, `repeat: false`, ISO datetime format with `.000Z` suffix.

### TestDeliverRouting (8)
Single window → basic plan, multiple windows → weekly plan, empty plan returns true without API call, login failure, API failure for both paths, rate passed through, correct datetimes passed to basic plan.

---

## test_deliver_myskoda.py (41 tests)

### TestTimeInWindow (8)
`_time_in_window` — the core primitive for active-slot detection: same-day windows (inside/outside), start is inclusive, end is exclusive (matches charging stopping exactly at window end, not overrunning it), overnight wraparound (both sides of midnight), zero-length windows never match.

### TestFindActiveSlotIndex (5)
`_find_active_slot_index` — infers which slot is driving an active PREFERRED_CHARGING_TIMES session from time-window overlap (the API doesn't report this directly): a single enabled matching slot is returned, no match returns `None`, a disabled slot with a matching time window never counts, more than one matching enabled slot is treated as ambiguous (`None`) rather than guessed at, empty slot list returns `None`.

### TestBuildUpdatedProfile (12)
Direct tests for `_build_updated_profile` — the slot mapping: one window fills slot 1 only, two windows fill slots 1–2, four windows fill all 4 slots, windows are written positionally by default (window 1 → slot 1, regardless of window ordering), unused slots disabled by default (times preserved), `protected_slot_index` leaves a specific slot completely untouched (enabled flag *and* both times) even while other slots are written or disabled around it, `target_slot_indices` supports explicit non-contiguous routing (e.g. windows → slots 0 and 2, skipping 1), a `target_slot_indices`/`windows_hhmm` length mismatch raises, more windows than the vehicle has slots raises, no `preferredChargingTimes` entries raises, the input profile dict is never mutated, slot `id` fields are preserved through the update.

### TestDeliverValidation (7)
`deliver()` config/plan validation: `max_windows: null` rejected (unbounded profiles are never safe to deliver, even if today's plan happens to fit), `max_windows > 4` rejected, `max_windows` 1 through 4 all accepted, more than 4 *actual* plan windows rejected as a defensive check independent of the configured `max_windows`, zero scheduled minutes rejected, empty window list rejected, missing API key rejected.

### TestDeliverSlotMapping (2)
End-to-end: three plan windows land in vehicle slots 1–3 with slot 4 left disabled; UTC window times are correctly converted to the vehicle's local timezone before being written.

### TestDeliverChargingState (7)
Charging-state safety logic, including active-slot detection: not charging → full delivery (slot update + mode change); charging in MANUAL/TIMER modes → slot update but no mode change (these modes don't use preferredChargingTimes slots at all, so nothing needs protecting); charging in PREFERRED_CHARGING_TIMES with a uniquely identifiable active slot → plan windows routed around it, that slot left completely untouched, other slots managed normally, no mode change; no time-window match → delivery skipped entirely; an ambiguous match (two enabled slots both overlap "now") → also skipped; active slot identified but the plan needs all 4 slots (no room to route around it) → skipped; charging in an unknown mode → delivery skipped entirely (still reports success, not failure — skipping is the correct outcome, not an error).
