# Test reports

Collection of test reports for the implementation

---

## Charging Planner Code quality

### Test suite

116 tests in `test_charging_planner.py`, run with:

```bash
python3 -m unittest discover -s test -v
```

| Area | Tests |
|---|---|
| Configuration validation | 11 |
| Config parsing | 6 |
| OCPP charging profile | 13 |
| XML parsing | 10 |
| Real ENTSO-E data | 9 |
| Slot selection (min-block) | 6 |
| Slot selection (general) | 7 |
| Best continuous window | 3 |
| Spillover | 4 |
| Gap merging | 6 |
| Plan building | 5 |
| Window filtering | 5 |
| Time utilities | 10 |
| End-to-end pipeline | 6 |
| Window coverage check | 5 |
| ntfy notification | 7 |
| **Total** | **116** |

The 3 OCPP schema validation tests require the spec zip files placed in `test/ocpp16/`, `test/ocpp201/`, `test/ocpp21/` — they skip cleanly without them. All other tests run with no dependencies beyond `pyyaml`.

The real ENTSO-E data tests use an actual API response from 2026-03-14 inlined as a fixture — no network access needed.

### Cyclomatic complexity

Measured across 76 functions:

| Grade | Threshold | Count |
|---|---|---|
| A/B | CC ≤ 10 | 68 |
| C | CC 11–15 | 6 |
| D | CC 16–20 | 2 |
| F | CC > 20 | 0 |

The two D-grade functions (`_select_with_min_block` CC=19, `close_gap_merge` CC=17) are selection algorithms whose complexity is inherent to their logic. All other functions are C or better.

---

## Charge Amps Delivery

### Test suite

30 tests in `test_deliver_chargeamps.py`, run with:

```bash
python3 -m unittest discover -s test -v
```

| Area | Tests |
|---|---|
| Anchor calculation | 4 |
| Period timing (from/to offsets) | 6 |
| Period fields | 6 |
| Edge cases | 3 |
| deliver() interface | 8 |
| Login cache | 3 |
| **Total** | **30** |

These tests focus on the scheduling logic that converts plan windows to Charge Amps `schedulePeriods` — specifically that slots land at the correct times relative to the Monday anchor, across timezones and DST transitions. All network calls are mocked; no credentials or live API access required.
