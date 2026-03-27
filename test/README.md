# Test reports

Collection of test reports for the implementation

---

## Charging Planner

### Test suite

119 tests in `test_charging_planner.py`, run with:

```bash
python3 -m unittest discover -s test -v
```

| Area | Tests |
|---|---|
| Configuration validation | 16 |
| Config parsing | 10 |
| Schedule window resolution | 6 |
| OCPP charging profile | 13 |
| XML parsing | 10 |
| Real ENTSO-E data | 9 |
| Slot selection (min-block) | 6 |
| Slot selection (general) | 7 |
| Best continuous window | 3 |
| Spillover | 4 |
| Gap merging | 6 |
| Plan building | 4 |
| Window filtering | 5 |
| Time utilities | 10 |
| End-to-end pipeline | 6 |
| Window coverage check | 5 |
| **Total** | **119** |

The 3 OCPP schema validation tests require the spec zip files placed in `test/ocpp16/`, `test/ocpp201/`, `test/ocpp21/` — they skip cleanly without them. All other tests run with no dependencies beyond `pyyaml`.

The real ENTSO-E data tests use an actual API response from 2026-03-14 inlined as a fixture — no network access needed.

### Cyclomatic complexity

Measured across 88 blocks (classes, functions, methods) in all four Python files:

| Grade | Threshold | Count |
|---|---|---|
| A/B | CC ≤ 10 | 71 |
| C | CC 11–15 | 9 |
| D | CC 16–20 | 6 |
| F | CC > 20 | 2 |

The two F-grade functions are `_validate_charging_profile` (CC=27) — grown through incremental addition of schedule, `any`, and mixed-value validation — and `_select_with_min_block` (CC=20), a selection algorithm whose complexity is inherent to its logic. The D-grade functions (`_send_delivery_ntfy`, `_send_set_charging_profile`, `close_gap_merge`, `_select_spillover`, `build_plan`, `_resolve_tz`) are similarly algorithm-heavy. Average complexity: B (5.9).

---

## Charge Amps Delivery

### Test suite

41 tests in `test_deliver_chargeamps.py`, run with:

```bash
python3 -m unittest discover -s test -v
```

| Area | Tests |
|---|---|
| Anchor calculation | 4 |
| Period timing (from/to offsets) | 6 |
| Period fields | 6 |
| Edge cases | 3 |
| deliver() interface | 14 |
| Connector mode extraction | 5 |
| Login cache | 3 |
| **Total** | **41** |

These tests focus on the scheduling logic that converts plan windows to Charge Amps `schedulePeriods` — specifically that slots land at the correct times relative to the Monday anchor, across timezones and DST transitions. The deliver() tests also cover mode read/restore behaviour: the charger's original mode (`On`, `Off`, or `Schedule`) is read before delivery and restored afterwards if it was not already `Schedule`. All network calls are mocked; no credentials or live API access required.

---

## Delivery Dispatcher (ntfy)

### Test suite

24 tests in `test_deliver.py`, run with:

```bash
python3 -m unittest discover -s test -v
```

| Area | Tests |
|---|---|
| Message content | 17 |
| Sending behaviour | 7 |
| **Total** | **24** |

These tests cover the ntfy notification logic in `delivery/deliver.py` — message content (profile summary, window times, delivery status per charger, skipped profiles), and sending behaviour (disabled, missing topic, topic from env vs config, empty plans, send failure handling). All HTTP calls are mocked.
