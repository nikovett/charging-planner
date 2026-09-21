# Test Suite

388 tests across four files. Run from the repo root:

```
python -m unittest test_charging_planner test_deliver_chargeamps test_deliver_easee test_deliver_myskoda -v
```

Or individually:

```
python -m unittest test_charging_planner -v       # 275 tests, 3 skipped
python -m unittest test_deliver_chargeamps -v     # 46 tests
python -m unittest test_deliver_easee -v          # 26 tests
python -m unittest test_deliver_myskoda -v        # 41 tests
```

The 3 skipped tests require a live ENTSO-E API key in the environment and are marked `@unittest.skip`.

---

## test_charging_planner.py (275 tests)

### TestConfigValidation (18)
Validation of `config.yaml` fields: required keys, type checks, range checks for `required_hours`, `min_slot_minutes`, `min_gap_minutes`, `max_price_cents_kwh`, `preferred_window`, and `max_windows` (null/positive-int accepted; zero, negative, float, bool, and string rejected — `bool` is a subclass of `int` in Python, so it needs an explicit check).

### TestAvgPriceCeiling (7)
`max_price_cents_kwh: avg` — validation accepts it, case-insensitive parsing, `max_price_is_avg` flag set, resolves to market average at plan time, numeric ceilings leave flag false.

### TestParseConfigs (12)
Profile merging over `CHARGING_DEFAULTS`, schedule parsing, multi-profile configs, weekday/default fallback, `any` window, duplicate day detection.

### TestResolveScheduleWindow (5)
Selects the correct schedule entry for the current day, falls back to default, rejects duplicate days and invalid day names.

### TestHhmmToUtc (4)
Converts `HH:MM` local strings to UTC datetimes, positive/negative offsets, overnight handling.

### TestIsOvernight (2)
Detects overnight windows (end ≤ start) vs same-day windows.

### TestResolveWindowUtc (4)
Resolves overnight and same-day HH:MM windows to UTC start/end datetimes for a specific anchor date. Purely mechanical — no dependency on current time (see TestResolvePlanningHorizon for the "which date" decision).

### TestResolvePlanningHorizon (18)
The full scenario matrix for `_resolve_planning_horizon`: bare (no schedule/`any`-flag) profiles check today's own occurrence directly, then tomorrow as fallback. Schedule/`any`-flag profiles index by day-of-use (a schedule entry describes the session that gets the car ready for *that* day — for an overnight shape, its window actually starts the evening before), checking today's own entry (overnight-tail only) then tomorrow's entry (the normal, day-ahead target). Covers a real production regression: on a Sunday with a weekday-overnight/weekend-`any` schedule, using Sunday's own trivially-"live" `any`/`any` entry meant Monday's fixed window was never considered — `test_schedule_regression_weekend_any_does_not_mask_weekday_overnight` reproduces this exactly. Also covers the original delayed-run fix (a run firing after a window's start used to skip straight to the next occurrence instead of catching the still-open remainder).

### TestClassifyWindowInstance (3)
Direct tests of `_classify_window_instance`'s "fixed start, `any` end" elapsed behavior (required minutes no longer fitting before `any_end_cap`) — a real, correct capability that isn't reachable through `_resolve_planning_horizon` for this specific shape combination (it's always tomorrow-anchored there, matching the original code's own behavior), so it's covered directly instead.

### TestFilterPreferredWindow (5)
Splits a slot list into inside/outside the preferred window. Overnight windows, slots on window boundaries, `any` window sentinel.

### TestSelectChargingWindows (7)
High-level slot selection: cheapest slots selected, required minutes met, price ceiling respected, `max_windows: 1` returns one block, `min_slot_minutes` enforced.

### TestBestContinuousWindow (3)
Returns the cheapest continuous run; respects temporal continuity (index adjacency is not sufficient); handles partial coverage.

### TestSelectSpillover (5)
Spillover from outside the preferred window: not triggered when window is satisfied, stays before window end, `max_windows: 1` extends leftward, handles remaining < min slot.

### TestSelectWithMinBlock (17)
Direct tests for `_select_with_min_block` (the `max_windows: null`, unbounded path): no blocks shorter than minimum, isolated cheap slot replaced, total minutes correct after disqualification, latest slot preferred on equal price, real price data, gap constraint respected, and window-coverage/`cmd_plan` exit-code checks that share this class — including `now_utc` clamping the coverage-check denominator and forecast-supplement filter to a live window's still-useful portion (a live window's already-elapsed time must never register as "missing" coverage, and a forecast supplement must never backfill it).

**`test_isolated_cheap_slot_with_price_ceiling`** — regression for the 2026-04-13 production bug: a cheap slot isolated by two above-ceiling neighbours must not be selected when it cannot form a valid block.

### TestSelectWithMaxWindows (10)
Direct tests for `_select_with_max_windows` (the `max_windows: N ≥ 2` path): uses at most N blocks, picks the cheapest N clusters over more expensive ones, `max_windows: 1` matches `_best_continuous_window` exactly, `max_windows: null` matches the unbounded path exactly, a generously high `max_windows` also matches the unbounded path, `min_gap_minutes`/`min_slot_minutes` enforced identically to the unbounded case, infeasible window budgets return `[]` cleanly, latest-slot tiebreak on equal price.

### TestBuildPlan (6)
`build_plan` output structure: required keys present (including `max_windows`), price stats, windows, OCPP profile, `max_windows` defaults to `null` and reflects the configured value.

### TestOcppChargingProfile (13)
OCPP 1.6, 2.0.1, and 2.1 profile generation: schema validity, `validFrom`/`validTo` match window bounds, periods ordered, `startPeriod` offsets correct for single and multiple windows, duration covers full span.

### TestXmlParsing (10)
ENTSO-E XML parsing: slot count, 15-min duration, sort order, ordinal sequencing, no duplicate starts, forward-fill between explicit points, resolution detection.

### TestEndToEnd (8)
Full `cmd_plan` run with mocked prices: one plan file per profile, required keys, OCPP profile present, JSON written to output dir, exits cleanly when prices unavailable. Plus two delayed-run regression tests: a run firing mid-window still targets tonight's live window rather than skipping to the next night, and — with an artificially-cheap already-elapsed slot planted to tempt the DP — confirms it's never selected.

### TestPriceSourceRules (9)
Price source selection rules (rules 1–4): real prices used when sufficient, forecast display appended, forecast supplement used when window not covered, `price_source` field set correctly, supplement slots tagged `forecasted: true`.

### TestRealEntsoEData (12)
Integration tests against a bundled ENTSO-E XML fixture: prices in plausible range, known peak price, `min_slot_minutes` respected, overnight windows stay within window, real-world slot selection.

### TestBuildFallbackChain (21)
`_build_fallback_chain` composition for every supported area: FI, EE, LV, LT, SE1–SE4, NO1–NO5, unknown area. EIC code equivalence, chain order, cross-area assertions (SE chain ≠ NO chain, no Elering in SE/NO).

### TestAreaFallbackChainIntegration (24)
`cmd_plan` with all fetchers patched: for each area family (FI, EE, SE1, NO1) — ENTSO-E success, each fallback tried in order when prior fails, sources that should never be called are asserted not called, plan exits when all sources fail.

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

### TestWriteConfigJson (4)
`write_config_json` secret redaction — regression coverage for a real bug where a real `ENTSOE_API_KEY` merged in from the environment could be written into the committed `data/config.json`. Real key redacted, empty key stays empty, caller's in-memory config not mutated, other fields (including `max_windows`) preserved.

---

## test_deliver_chargeamps.py (46 tests)

### TestAnchorCalculation (4)
Monday anchor is always Monday 00:00 local time expressed as UTC, across timezones and DST transitions.

### TestPeriodTiming (6)
`schedulePeriods` offsets are correct seconds from anchor for single and multiple windows.

### TestPeriodFields (6)
Each period has required fields, correct types, unique IDs, `from` < `to`.

### TestBuildPeriodsEdgeCases (6)
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
