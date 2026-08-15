"""
Tests for delivery/deliver_chargeamps.py
========================================
Focuses on the scheduling logic that converts plan windows to Charge Amps
schedulePeriods — specifically that slots land at the correct times relative
to the Monday anchor, across timezones and DST transitions.

Run from the repo root:
    python -m unittest discover -s test -v

Or directly:
    python test/test_deliver_chargeamps.py
"""

import sys
import unittest
import unittest.mock as mock
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

sys.path.insert(0, ".")
sys.path.insert(0, "..")

# deliver_chargeamps.py lives in delivery/
sys.path.insert(0, "delivery")

from deliver_chargeamps import (
    _ca_build_periods,
    _ca_get_connector_mode,
    _ca_login,
    _ca_set_connector_mode,
    deliver,
)

UTC   = timezone.utc
FI_TZ = ZoneInfo("Europe/Helsinki")


# ===========================================================================
# Helpers
# ===========================================================================

def make_plan(window_starts_utc: list[str], window_ends_utc: list[str]) -> dict:
    return {
        "window_starts_utc": window_starts_utc,
        "window_ends_utc":   window_ends_utc,
    }


def make_entry(**overrides) -> dict:
    defaults = {"connector_id": 1, "max_charging_rate": 16.0}
    defaults.update(overrides)
    return defaults


# ===========================================================================
# _ca_build_periods — anchor calculation
# ===========================================================================

class TestAnchorCalculation(unittest.TestCase):
    """The anchor is always Monday 00:00 local time expressed as UTC.

    This is what Charge Amps uses as the origin for its weekly schedule.
    All period 'from'/'to' values are seconds offset from this anchor.
    """

    def test_anchor_is_monday_midnight_local(self):
        # 2026-03-15 is a Sunday in Helsinki (EET = UTC+2).
        # Window ends Mon 06:30 Helsinki — to > 604800, so anchor advances one week.
        # Advanced anchor = 2026-03-16 00:00 EET = 2026-03-15 22:00 UTC
        plan = make_plan(
            ["2026-03-15T20:00:00+00:00"],  # Sunday 22:00 Helsinki
            ["2026-03-16T04:30:00+00:00"],  # Monday 06:30 Helsinki
        )
        _, anchor_iso = _ca_build_periods(plan, "Europe/Helsinki", 16.0)
        self.assertEqual(anchor_iso, "2026-03-08T22:00:00Z")

    def test_anchor_no_advance_within_week(self):
        # Window ending Sunday 23:00 Helsinki — to <= 604800, anchor stays this Monday.
        # 2026-03-15 is Sunday; this Monday = 2026-03-09 00:00 EET = 2026-03-08 22:00 UTC
        plan = make_plan(
            ["2026-03-15T18:00:00+00:00"],  # Sunday 20:00 Helsinki
            ["2026-03-15T21:00:00+00:00"],  # Sunday 23:00 Helsinki
        )
        _, anchor_iso = _ca_build_periods(plan, "Europe/Helsinki", 16.0)
        self.assertEqual(anchor_iso, "2026-03-08T22:00:00Z")

    def test_anchor_is_same_week_when_window_starts_monday(self):
        # 2026-03-16 is a Monday in Helsinki
        # Anchor = 2026-03-16 00:00 EET = 2026-03-15 22:00 UTC
        plan = make_plan(
            ["2026-03-16T00:00:00+00:00"],
            ["2026-03-16T06:00:00+00:00"],
        )
        _, anchor_iso = _ca_build_periods(plan, "Europe/Helsinki", 16.0)
        self.assertEqual(anchor_iso, "2026-03-15T22:00:00Z")

    def test_anchor_utc_timezone(self):
        # With UTC timezone anchor should be Monday 00:00 UTC
        # 2026-03-16 is Monday
        plan = make_plan(
            ["2026-03-16T02:00:00+00:00"],
            ["2026-03-16T06:00:00+00:00"],
        )
        _, anchor_iso = _ca_build_periods(plan, "UTC", 16.0)
        self.assertEqual(anchor_iso, "2026-03-16T00:00:00Z")

    def test_anchor_dst_transition_week(self):
        # 2026-03-29 is DST transition Sunday in Helsinki (EET→EEST).
        # Window ends Mon 04:30 EEST — to > 604800, anchor advances one week.
        # Advanced anchor = 2026-03-30 00:00 EEST = 2026-03-29 21:00 UTC
        plan = make_plan(
            ["2026-03-29T20:00:00+00:00"],  # Sunday 22:00 EET (before transition)
            ["2026-03-30T01:30:00+00:00"],  # Monday 04:30 EEST
        )
        _, anchor_iso = _ca_build_periods(plan, "Europe/Helsinki", 16.0)
        self.assertEqual(anchor_iso, "2026-03-22T22:00:00Z")


# ===========================================================================
# _ca_build_periods — period timing (from/to offsets)
# ===========================================================================

class TestPeriodTiming(unittest.TestCase):
    """Period 'from'/'to' must be exact seconds from the Monday anchor.

    These offsets are what Charge Amps uses to place the charging window in
    the weekly calendar — wrong values mean charging at the wrong time.
    """

    def _periods_and_anchor(self, start_iso, end_iso, tz="Europe/Helsinki", rate=16.0):
        plan = make_plan([start_iso], [end_iso])
        periods, anchor = _ca_build_periods(plan, tz, rate)
        return periods, anchor

    def test_single_window_from_offset(self):
        # Window: Sunday 2026-03-15 20:00–22:00 Helsinki (EET = UTC+2), ending before Monday
        # Anchor: Monday 2026-03-09 00:00 EET = 2026-03-08 22:00 UTC
        # Start:  2026-03-15 18:00 UTC = Sunday 20:00 Helsinki
        # Offset from anchor: 6 days + 20 hours = (6*24 + 20) * 3600 = 590400 s
        periods, _ = self._periods_and_anchor(
            "2026-03-15T18:00:00+00:00",
            "2026-03-15T20:00:00+00:00",
        )
        self.assertEqual(len(periods), 1)
        self.assertEqual(periods[0]["from"], 6 * 24 * 3600 + 20 * 3600)

    def test_single_window_to_offset(self):
        # End: 2026-03-16 04:30 UTC = Monday 06:30 Helsinki — crosses midnight.
        # Anchor advances one week; to = 628200 - 604800 = 23400s from next anchor.
        periods, anchor_iso = self._periods_and_anchor(
            "2026-03-15T20:00:00+00:00",
            "2026-03-16T04:30:00+00:00",
        )
        self.assertEqual(len(periods), 1)
        self.assertLessEqual(periods[0]["to"], 604800)

    def test_window_duration_preserved(self):
        # A 4h window not crossing Monday midnight — duration exactly preserved.
        # Sat 22:00–02:00 Helsinki (does not reach Monday)
        periods, _ = self._periods_and_anchor(
            "2026-03-14T20:00:00+00:00",
            "2026-03-15T00:00:00+00:00",  # 4h window, ends Sun 02:00 Helsinki
        )
        duration = periods[0]["to"] - periods[0]["from"]
        self.assertEqual(duration, 4 * 3600)

    def test_two_windows_correct_offsets(self):
        # Split window: 01:00–02:30 and 03:00–03:30 Helsinki on 2026-03-17 (Tuesday)
        # Anchor: Monday 2026-03-16 00:00 EET = 2026-03-15 22:00 UTC
        # Window 1 start: 2026-03-16 23:00 UTC = 01:00 Helsinki Tuesday
        # Offset: (1*24 + 1) * 3600 = 90000 s
        # Window 1 end: 2026-03-17 00:30 UTC = 02:30 Helsinki
        # Offset: (1*24 + 2.5) * 3600 = 95400 s
        plan = make_plan(
            ["2026-03-16T23:00:00+00:00", "2026-03-17T01:00:00+00:00"],
            ["2026-03-17T00:30:00+00:00", "2026-03-17T01:30:00+00:00"],
        )
        periods, _ = _ca_build_periods(plan, "Europe/Helsinki", 16.0)
        self.assertEqual(len(periods), 2)
        self.assertEqual(periods[0]["from"], 1 * 24 * 3600 + 1 * 3600)
        self.assertEqual(periods[0]["to"],   1 * 24 * 3600 + int(2.5 * 3600))
        self.assertEqual(periods[1]["from"], 1 * 24 * 3600 + 3 * 3600)
        self.assertEqual(periods[1]["to"],   1 * 24 * 3600 + int(3.5 * 3600))

    def test_from_always_less_than_to(self):
        periods, _ = self._periods_and_anchor(
            "2026-03-15T20:00:00+00:00",
            "2026-03-16T04:30:00+00:00",
        )
        for p in periods:
            self.assertLess(p["from"], p["to"])

    def test_consecutive_windows_ordered(self):
        plan = make_plan(
            ["2026-03-16T23:00:00+00:00", "2026-03-17T01:00:00+00:00"],
            ["2026-03-17T00:30:00+00:00", "2026-03-17T01:30:00+00:00"],
        )
        periods, _ = _ca_build_periods(plan, "Europe/Helsinki", 16.0)
        froms = [p["from"] for p in periods]
        self.assertEqual(froms, sorted(froms))


# ===========================================================================
# _ca_build_periods — period fields
# ===========================================================================

class TestPeriodFields(unittest.TestCase):
    """Each period must have the correct fields with correct types/values."""

    def _periods(self, rate=16.0):
        plan = make_plan(
            ["2026-03-15T20:00:00+00:00"],
            ["2026-03-16T04:30:00+00:00"],
        )
        periods, _ = _ca_build_periods(plan, "Europe/Helsinki", rate)
        return periods

    def test_period_has_required_fields(self):
        for p in self._periods():
            self.assertIn("id", p)
            self.assertIn("from", p)
            self.assertIn("to", p)
            self.assertIn("maxCurrent", p)

    def test_max_current_value(self):
        periods = self._periods(rate=10.0)
        self.assertEqual(periods[0]["maxCurrent"], 10.0)

    def test_max_current_default(self):
        periods = self._periods(rate=16.0)
        self.assertEqual(periods[0]["maxCurrent"], 16.0)

    def test_id_is_nonempty_string(self):
        for p in self._periods():
            self.assertIsInstance(p["id"], str)
            self.assertGreater(len(p["id"]), 0)

    def test_ids_are_unique_across_windows(self):
        plan = make_plan(
            ["2026-03-16T23:00:00+00:00", "2026-03-17T01:00:00+00:00"],
            ["2026-03-17T00:30:00+00:00", "2026-03-17T01:30:00+00:00"],
        )
        periods, _ = _ca_build_periods(plan, "Europe/Helsinki", 16.0)
        ids = [p["id"] for p in periods]
        self.assertEqual(len(ids), len(set(ids)))

    def test_from_to_are_integers(self):
        for p in self._periods():
            self.assertIsInstance(p["from"], int)
            self.assertIsInstance(p["to"], int)


# ===========================================================================
# _ca_build_periods — edge cases
# ===========================================================================

class TestBuildPeriodsEdgeCases(unittest.TestCase):

    def test_empty_plan_returns_empty(self):
        plan = make_plan([], [])
        periods, anchor = _ca_build_periods(plan, "Europe/Helsinki", 16.0)
        self.assertEqual(periods, [])
        self.assertEqual(anchor, "")

    def test_anchor_iso_format(self):
        plan = make_plan(
            ["2026-03-15T20:00:00+00:00"],
            ["2026-03-16T04:30:00+00:00"],
        )
        _, anchor_iso = _ca_build_periods(plan, "Europe/Helsinki", 16.0)
        # Must be parseable and end with Z
        self.assertTrue(anchor_iso.endswith("Z"))
        datetime.strptime(anchor_iso, "%Y-%m-%dT%H:%M:%SZ")  # must not raise

    def test_z_suffix_in_window_timestamps_handled(self):
        # Timestamps with Z suffix (not +00:00) must be handled correctly
        plan = make_plan(
            ["2026-03-15T20:00:00Z"],
            ["2026-03-16T04:30:00Z"],
        )
        periods, anchor = _ca_build_periods(plan, "Europe/Helsinki", 16.0)
        self.assertEqual(len(periods), 1)
        self.assertGreater(periods[0]["to"], periods[0]["from"])

    def test_window_crossing_monday_midnight_wraps_to_zero(self):
        # Sun 23:45 → Mon 01:00 Helsinki — to would be 608400s, exceeds 604800.
        # Wrapped: from=0, to=608400-604800=3600 (Monday 00:00→01:00 on charger).
        # Anchor stays this Monday — 2026-08-10 00:00 Helsinki = 2026-08-09 21:00 UTC
        plan = make_plan(
            ["2026-08-16T20:45:00+00:00"],  # Sun 23:45 Helsinki
            ["2026-08-16T22:00:00+00:00"],  # Mon 01:00 Helsinki
        )
        periods, anchor_iso = _ca_build_periods(plan, "Europe/Helsinki", 16.0)
        self.assertEqual(anchor_iso, "2026-08-09T21:00:00Z")
        self.assertEqual(len(periods), 1)
        self.assertEqual(periods[0]["from"], 0)
        self.assertEqual(periods[0]["to"], 3600)

    def test_window_fully_on_monday_fits_in_this_week(self):
        # Mon 01:00–02:00 Helsinki: anchor = that same Monday 00:00 Helsinki.
        # Window starts Mon 01:00 = 3600s, ends Mon 02:00 = 7200s — no wrap needed.
        # 2026-08-17 00:00 Helsinki = 2026-08-16 21:00 UTC (anchor)
        plan = make_plan(
            ["2026-08-16T22:00:00+00:00"],  # Mon 01:00 Helsinki
            ["2026-08-16T23:00:00+00:00"],  # Mon 02:00 Helsinki
        )
        periods, anchor_iso = _ca_build_periods(plan, "Europe/Helsinki", 16.0)
        self.assertEqual(len(periods), 1)
        self.assertEqual(periods[0]["from"], 3600)
        self.assertEqual(periods[0]["to"], 7200)
        self.assertLessEqual(periods[0]["to"], 604800)

    def test_monday_window_from_any_any_plan(self):
        # Saturday any:any plan with windows on Sunday and Monday.
        # Anchor = Mon 2026-08-10 00:00 Helsinki = 2026-08-09 21:00 UTC (from Saturday start).
        # Saturday slot: no wrap.
        # Monday 00:30–02:30 slot: from=608400, to=615600 — both > 604800, shift by -604800.
        plan = make_plan(
            ["2026-08-15T14:00:00+00:00",   # Sat 17:00 Helsinki
             "2026-08-16T21:30:00+00:00"],  # Mon 00:30 Helsinki
            ["2026-08-15T15:00:00+00:00",   # Sat 18:00 Helsinki
             "2026-08-16T23:30:00+00:00"],  # Mon 02:30 Helsinki
        )
        periods, anchor_iso = _ca_build_periods(plan, "Europe/Helsinki", 16.0)
        self.assertEqual(anchor_iso, "2026-08-09T21:00:00Z")
        self.assertEqual(len(periods), 2)
        # Saturday slot unchanged
        self.assertEqual(periods[0]["from"], int((datetime.fromisoformat("2026-08-15T14:00:00+00:00") - datetime.fromisoformat("2026-08-09T21:00:00+00:00")).total_seconds()))
        # Monday slot shifted by -604800: from=606600-604800=1800, to=613800-604800=9000
        self.assertEqual(periods[1]["from"], 1800)   # 00:30 into Monday
        self.assertEqual(periods[1]["to"],   9000)   # 02:30 into Monday
        self.assertLessEqual(periods[1]["to"], 604800)

    def test_normal_window_no_wrapping(self):
        # Normal Tuesday window — no wrapping, anchor stays this week's Monday.
        # 2026-08-11 is Tuesday; Mon 2026-08-10 00:00 Helsinki = 2026-08-09 21:00 UTC
        plan = make_plan(
            ["2026-08-11T18:00:00+00:00"],
            ["2026-08-12T03:30:00+00:00"],
        )
        periods, anchor_iso = _ca_build_periods(plan, "Europe/Helsinki", 16.0)
        self.assertEqual(len(periods), 1)
        self.assertGreater(periods[0]["from"], 0)
        self.assertLessEqual(periods[0]["to"], 604800)
        self.assertEqual(anchor_iso, "2026-08-09T21:00:00Z")


# deliver() — public interface
# ===========================================================================

def _make_chargepoint(mode: str = "On", connector_id: int = 1) -> dict:
    """Minimal chargepoint dict matching the API response shape."""
    return {
        "id": "CHARGER-001",
        "connectors": [
            {"connectorId": connector_id, "mode": mode},
        ],
    }


class TestDeliver(unittest.TestCase):
    """Tests for the deliver() public interface — mocks all network calls."""

    PLAN  = make_plan(
        ["2026-03-15T20:00:00+00:00"],
        ["2026-03-16T04:30:00+00:00"],
    )
    ENTRY = make_entry(connector_id=1, max_charging_rate=16.0, restore_mode=True)
    TZ    = "Europe/Helsinki"
    CP_ID = "CHARGER-001"

    def _mock_login(self):
        import deliver_chargeamps as mod
        mod._token_cache.clear()
        return mock.patch("deliver_chargeamps._ca_login",
                          return_value=("tok", "ent_tok"))

    def _mock_get(self, mode: str = "On"):
        return mock.patch("deliver_chargeamps._ca_get_chargepoint",
                          return_value=_make_chargepoint(mode))

    def _mock_put(self):
        return mock.patch("deliver_chargeamps._ca_put_schedule")

    def _mock_set_mode(self):
        return mock.patch("deliver_chargeamps._ca_set_connector_mode")

    # ── Basic success / failure ──────────────────────────────────────────────

    def test_deliver_returns_true_on_success(self):
        with self._mock_login(), self._mock_get(), self._mock_put(), self._mock_set_mode():
            result = deliver(self.PLAN, self.CP_ID, self.ENTRY, self.TZ)
        self.assertTrue(result)

    def test_deliver_returns_false_on_login_failure(self):
        import deliver_chargeamps as mod
        mod._token_cache.clear()
        with mock.patch("deliver_chargeamps._ca_login",
                        side_effect=RuntimeError("bad credentials")):
            result = deliver(self.PLAN, self.CP_ID, self.ENTRY, self.TZ)
        self.assertFalse(result)

    def test_deliver_returns_false_on_get_chargepoint_failure(self):
        with self._mock_login():
            with mock.patch("deliver_chargeamps._ca_get_chargepoint",
                            side_effect=Exception("network error")):
                result = deliver(self.PLAN, self.CP_ID, self.ENTRY, self.TZ)
        self.assertFalse(result)

    def test_deliver_returns_false_on_put_failure(self):
        with self._mock_login(), self._mock_get():
            with mock.patch("deliver_chargeamps._ca_put_schedule",
                            side_effect=Exception("network error")):
                result = deliver(self.PLAN, self.CP_ID, self.ENTRY, self.TZ)
        self.assertFalse(result)

    # ── Argument passthrough ─────────────────────────────────────────────────

    def test_deliver_passes_connector_id_from_entry(self):
        entry = make_entry(connector_id=2, max_charging_rate=16.0)
        with self._mock_login(), self._mock_get(), self._mock_set_mode():
            with mock.patch("deliver_chargeamps._ca_put_schedule") as mock_put:
                deliver(self.PLAN, self.CP_ID, entry, self.TZ)
                self.assertEqual(mock_put.call_args[0][2], 2)

    def test_deliver_passes_max_charging_rate_from_entry(self):
        entry = make_entry(connector_id=1, max_charging_rate=10.0)
        with self._mock_login(), self._mock_get(), self._mock_set_mode():
            with mock.patch("deliver_chargeamps._ca_put_schedule") as mock_put:
                deliver(self.PLAN, self.CP_ID, entry, self.TZ)
                self.assertEqual(mock_put.call_args[0][4], 10.0)

    def test_deliver_passes_timezone_to_put(self):
        with self._mock_login(), self._mock_get(), self._mock_set_mode():
            with mock.patch("deliver_chargeamps._ca_put_schedule") as mock_put:
                deliver(self.PLAN, self.CP_ID, self.ENTRY, "UTC")
                self.assertEqual(mock_put.call_args[0][3], "UTC")

    def test_deliver_uses_default_connector_id_1(self):
        with self._mock_login(), self._mock_get(), self._mock_set_mode():
            with mock.patch("deliver_chargeamps._ca_put_schedule") as mock_put:
                deliver(self.PLAN, self.CP_ID, {}, self.TZ)
                self.assertEqual(mock_put.call_args[0][2], 1)

    def test_deliver_uses_default_max_charging_rate_16(self):
        with self._mock_login(), self._mock_get(), self._mock_set_mode():
            with mock.patch("deliver_chargeamps._ca_put_schedule") as mock_put:
                deliver(self.PLAN, self.CP_ID, {}, self.TZ)
                self.assertEqual(mock_put.call_args[0][4], 16.0)

    # ── Mode restore ─────────────────────────────────────────────────────────

    def test_mode_restored_when_was_on(self):
        """If mode was 'On' before delivery, it must be restored afterwards."""
        with self._mock_login(), self._mock_get("On"), self._mock_put():
            with mock.patch("deliver_chargeamps._ca_set_connector_mode") as mock_set:
                deliver(self.PLAN, self.CP_ID, self.ENTRY, self.TZ)
                mock_set.assert_called_once()
                self.assertEqual(mock_set.call_args[0][2], "On")

    def test_mode_restored_when_was_off(self):
        """If mode was 'Off' before delivery, it must be restored afterwards."""
        with self._mock_login(), self._mock_get("Off"), self._mock_put():
            with mock.patch("deliver_chargeamps._ca_set_connector_mode") as mock_set:
                deliver(self.PLAN, self.CP_ID, self.ENTRY, self.TZ)
                mock_set.assert_called_once()
                self.assertEqual(mock_set.call_args[0][2], "Off")

    def test_mode_not_restored_when_already_schedule(self):
        """If mode was already 'Schedule', no mode restore call should be made."""
        with self._mock_login(), self._mock_get("Schedule"), self._mock_put():
            with mock.patch("deliver_chargeamps._ca_set_connector_mode") as mock_set:
                deliver(self.PLAN, self.CP_ID, self.ENTRY, self.TZ)
                mock_set.assert_not_called()

    def test_returns_true_even_if_mode_restore_fails(self):
        """Schedule was delivered — mode restore failure is a warning, not an error."""
        with self._mock_login(), self._mock_get("On"), self._mock_put():
            with mock.patch("deliver_chargeamps._ca_set_connector_mode",
                            side_effect=Exception("restore failed")):
                result = deliver(self.PLAN, self.CP_ID, self.ENTRY, self.TZ)
        self.assertTrue(result)

    def test_restore_mode_false_skips_set_but_still_reads(self):
        """When restore_mode is false, mode is not restored but chargepoint is still
        read to detect active charging for schedule override."""
        entry = make_entry(connector_id=1, max_charging_rate=16.0, restore_mode=False)
        with self._mock_login(), self._mock_put():
            with mock.patch("deliver_chargeamps._ca_get_chargepoint",
                            return_value=_make_chargepoint(mode="On")) as mock_get, \
                 mock.patch("deliver_chargeamps._ca_set_connector_mode") as mock_set:
                result = deliver(self.PLAN, self.CP_ID, entry, self.TZ)
                mock_get.assert_called_once()
                mock_set.assert_not_called()
        self.assertTrue(result)


# ===========================================================================
# _ca_get_connector_mode — mode extraction
# ===========================================================================

class TestGetConnectorMode(unittest.TestCase):

    def test_returns_mode_for_matching_connector(self):
        cp = _make_chargepoint(mode="On", connector_id=1)
        self.assertEqual(_ca_get_connector_mode(cp, 1), "On")

    def test_returns_schedule_mode(self):
        cp = _make_chargepoint(mode="Schedule", connector_id=1)
        self.assertEqual(_ca_get_connector_mode(cp, 1), "Schedule")

    def test_returns_off_mode(self):
        cp = _make_chargepoint(mode="Off", connector_id=1)
        self.assertEqual(_ca_get_connector_mode(cp, 1), "Off")

    def test_returns_none_for_missing_connector(self):
        cp = _make_chargepoint(mode="On", connector_id=1)
        self.assertIsNone(_ca_get_connector_mode(cp, 2))

    def test_returns_none_for_empty_connectors(self):
        cp = {"id": "X", "connectors": []}
        self.assertIsNone(_ca_get_connector_mode(cp, 1))


# ===========================================================================
# _ca_login — token caching
# ===========================================================================

class TestLoginCache(unittest.TestCase):
    """Login must only hit the network once per process — token is cached."""

    def setUp(self):
        import deliver_chargeamps as mod
        mod._token_cache.clear()

    def test_second_call_uses_cache(self):
        import deliver_chargeamps as mod
        mod._token_cache["token"]     = "cached_tok"
        mod._token_cache["ent_token"] = "cached_ent"
        # _ca_request should never be called if cache is warm
        with mock.patch("deliver_chargeamps._ca_request") as mock_req:
            token, ent = _ca_login()
            mock_req.assert_not_called()
        self.assertEqual(token, "cached_tok")
        self.assertEqual(ent,   "cached_ent")

    def test_missing_credentials_raises(self):
        with mock.patch.dict("os.environ",
                             {"CHARGER_USERNAME": "", "CHARGER_PASSWORD": ""}):
            with self.assertRaises(RuntimeError) as ctx:
                _ca_login()
            self.assertIn("CHARGER_USERNAME", str(ctx.exception))

    def test_login_stores_tokens_in_cache(self):
        import deliver_chargeamps as mod
        login_resp = {
            "token": "new_tok",
            "user":  {"claims": ["org-uuid-123:Owner"]},
        }
        ent_resp = {"token": "new_ent_tok"}

        def fake_request(path, **kwargs):
            if "/auth/login" in path:
                return login_resp
            if "/entitlements/context" in path:
                return ent_resp
            raise AssertionError(f"Unexpected path: {path}")

        with mock.patch.dict("os.environ",
                             {"CHARGER_USERNAME": "u@x.com", "CHARGER_PASSWORD": "pw"}):
            with mock.patch("deliver_chargeamps._ca_request", side_effect=fake_request):
                _ca_login()

        self.assertEqual(mod._token_cache["token"],     "new_tok")
        self.assertEqual(mod._token_cache["ent_token"], "new_ent_tok")


if __name__ == "__main__":
    unittest.main(verbosity=2)
