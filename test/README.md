# Test Suite

246 tests across three files. Run from the repo root:

```
python -m unittest test_charging_planner test_deliver_chargeamps test_deliver_easee -v
```

Or individually:

```
python -m unittest test_charging_planner -v       # 177 tests, 3 skipped
python -m unittest test_deliver_chargeamps -v     # 46 tests
python -m unittest test_deliver_easee -v          # 26 tests
```

The 3 skipped tests require a live ENTSO-E API key in the environment and are marked `@unittest.skip`.

---

## test_charging_planner.py (177 tests)

### TestConfigValidation (11)
Validation of `config.yaml` fields: required keys, type checks, range checks for `required_hours`, `min_slot_minutes`, `min_gap_minutes`, `max_price_cents_kwh`, and `preferred_window`.

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
Resolves overnight and same-day windows to UTC start/end datetimes, anchoring to the correct calendar day.

### TestFilterPreferredWindow (5)
Splits a slot list into inside/outside the preferred window. Overnight windows, slots on window boundaries, `any` window sentinel.

### TestSelectChargingWindows (7)
High-level slot selection: cheapest slots selected, required minutes met, price ceiling respected, `continuous_only` returns one block, `min_slot_minutes` enforced.

### TestBestContinuousWindow (3)
Returns the cheapest continuous run; respects temporal continuity (index adjacency is not sufficient); handles partial coverage.

### TestSelectSpillover (5)
Spillover from outside the preferred window: not triggered when window is satisfied, stays before window end, `continuous_only` extends leftward, handles remaining < min slot.

### TestSelectWithMinBlock (13)
Direct tests for `_select_with_min_block`: no blocks shorter than minimum, isolated cheap slot replaced, total minutes correct after disqualification, latest slot preferred on equal price, real price data, gap constraint respected.

**`test_isolated_cheap_slot_with_price_ceiling`** — regression for the 2026-04-13 production bug: a cheap slot isolated by two above-ceiling neighbours must not be selected when it cannot form a valid block.

### TestBuildPlan (4)
`build_plan` output structure: required keys present, price stats, windows, OCPP profile.

### TestOcppChargingProfile (13)
OCPP 1.6, 2.0.1, and 2.1 profile generation: schema validity, `validFrom`/`validTo` match window bounds, periods ordered, `startPeriod` offsets correct for single and multiple windows, duration covers full span.

### TestXmlParsing (10)
ENTSO-E XML parsing: slot count, 15-min duration, sort order, ordinal sequencing, no duplicate starts, forward-fill between explicit points, resolution detection.

### TestEndToEnd (6)
Full `cmd_plan` run with mocked prices: one plan file per profile, required keys, OCPP profile present, JSON written to output dir, exits cleanly when prices unavailable.

### TestPriceSourceRules (9)
Price source selection rules (rules 1–4): real prices used when sufficient, forecast display appended, forecast supplement used when window not covered, `price_source` field set correctly, supplement slots tagged `forecasted: true`.

### TestRealEntsoEData (12)
Integration tests against a bundled ENTSO-E XML fixture: prices in plausible range, known peak price, `min_slot_minutes` respected, overnight windows stay within window, real-world slot selection.

### TestBuildFallbackChain (21)
`_build_fallback_chain` composition for every supported area: FI, EE, LV, LT, SE1–SE4, NO1–NO5, unknown area. EIC code equivalence, chain order, cross-area assertions (SE chain ≠ NO chain, no Elering in SE/NO).

### TestAreaFallbackChainIntegration (24)
`cmd_plan` with all fetchers patched: for each area family (FI, EE, SE1, NO1) — ENTSO-E success, each fallback tried in order when prior fails, sources that should never be called are asserted not called, plan exits when all sources fail.

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
