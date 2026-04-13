"""
Tests for deliver_easee.py
==========================
Focuses on the scheduling logic that maps plan windows to Easee API payloads —
day-of-week mapping, time formatting, single vs multiple window routing, and
edge cases like windows spanning midnight UTC or landing on every day of the week.

Run from the repo root:
    python -m unittest test_deliver_easee.py -v

Or directly:
    python test_deliver_easee.py
"""

import sys
import unittest
import unittest.mock as mock
from datetime import datetime, timezone

sys.path.insert(0, ".")
sys.path.insert(0, "..")

import deliver_easee
from deliver_easee import _easee_put_weekly_plan, _easee_put_basic_plan, deliver

UTC = timezone.utc


# ===========================================================================
# Helpers
# ===========================================================================

def dt(*args) -> datetime:
    """Shorthand for UTC datetime construction."""
    return datetime(*args, tzinfo=UTC)


def make_plan(window_starts_utc: list[str], window_ends_utc: list[str]) -> dict:
    return {
        "window_starts_utc": window_starts_utc,
        "window_ends_utc":   window_ends_utc,
    }


def make_entry(**overrides) -> dict:
    defaults = {"max_charging_rate": 16}
    defaults.update(overrides)
    return defaults


def captured_weekly_payload(windows: list[tuple[datetime, datetime]], rate: int = 16) -> dict:
    """Call _easee_put_weekly_plan with a mocked _easee_request and return the payload."""
    with mock.patch("deliver_easee._easee_request") as mock_req:
        _easee_put_weekly_plan("CHARGER1", windows, rate, token="tok")
        _, kwargs = mock_req.call_args
        return kwargs["body"]


def captured_basic_payload(start: datetime, stop: datetime) -> dict:
    """Call _easee_put_basic_plan with a mocked _easee_request and return the payload."""
    with mock.patch("deliver_easee._easee_request") as mock_req:
        _easee_put_basic_plan("CHARGER1", start, stop, token="tok")
        _, kwargs = mock_req.call_args
        return kwargs["body"]


# ===========================================================================
# _easee_put_weekly_plan — day-of-week mapping
# ===========================================================================

class TestWeeklyPlanDayOfWeek(unittest.TestCase):
    """Verify that each window lands on the correct dayOfWeek slot (0=Monday)."""

    def _day_ranges(self, payload: dict) -> dict[int, list]:
        """Return {dayOfWeek: ranges} for non-empty days."""
        return {d["dayOfWeek"]: d["ranges"] for d in payload["days"] if d["ranges"]}

    def test_monday(self):
        # 2026-04-13 is a Monday
        windows = [(dt(2026, 4, 13, 22, 0), dt(2026, 4, 14, 4, 0))]
        payload = captured_weekly_payload(windows)
        day_ranges = self._day_ranges(payload)
        self.assertIn(0, day_ranges)
        self.assertEqual(len(day_ranges), 1)

    def test_tuesday(self):
        # 2026-04-14 is a Tuesday
        windows = [(dt(2026, 4, 14, 22, 0), dt(2026, 4, 15, 4, 0))]
        payload = captured_weekly_payload(windows)
        day_ranges = self._day_ranges(payload)
        self.assertIn(1, day_ranges)

    def test_wednesday(self):
        windows = [(dt(2026, 4, 15, 22, 0), dt(2026, 4, 16, 4, 0))]
        payload = captured_weekly_payload(windows)
        self.assertIn(2, self._day_ranges(payload))

    def test_thursday(self):
        windows = [(dt(2026, 4, 16, 22, 0), dt(2026, 4, 17, 4, 0))]
        payload = captured_weekly_payload(windows)
        self.assertIn(3, self._day_ranges(payload))

    def test_friday(self):
        windows = [(dt(2026, 4, 17, 22, 0), dt(2026, 4, 18, 4, 0))]
        payload = captured_weekly_payload(windows)
        self.assertIn(4, self._day_ranges(payload))

    def test_saturday(self):
        windows = [(dt(2026, 4, 18, 22, 0), dt(2026, 4, 19, 4, 0))]
        payload = captured_weekly_payload(windows)
        self.assertIn(5, self._day_ranges(payload))

    def test_sunday(self):
        # 2026-04-19 is a Sunday — previously broken (isoweekday()%7 gave 0)
        windows = [(dt(2026, 4, 19, 22, 0), dt(2026, 4, 20, 4, 0))]
        payload = captured_weekly_payload(windows)
        day_ranges = self._day_ranges(payload)
        self.assertIn(6, day_ranges, "Sunday must map to dayOfWeek=6, not 0")
        self.assertNotIn(0, day_ranges, "Sunday must not collide with Monday slot")

    def test_sunday_not_monday(self):
        """Regression: before the fix isoweekday()%7 mapped Sunday→0 (Monday)."""
        windows = [(dt(2026, 4, 19, 22, 0), dt(2026, 4, 20, 4, 0))]
        payload = captured_weekly_payload(windows)
        monday_ranges = payload["days"][0]["ranges"]
        self.assertEqual(monday_ranges, [], "Sunday window must not bleed into Monday slot")


# ===========================================================================
# _easee_put_weekly_plan — payload structure
# ===========================================================================

class TestWeeklyPlanPayload(unittest.TestCase):

    def test_all_seven_days_present(self):
        windows = [(dt(2026, 4, 13, 22, 0), dt(2026, 4, 14, 4, 0))]
        payload = captured_weekly_payload(windows)
        self.assertEqual(len(payload["days"]), 7)
        dow_values = [d["dayOfWeek"] for d in payload["days"]]
        self.assertEqual(dow_values, list(range(7)))

    def test_non_target_days_are_empty(self):
        # Window on Monday — all other days must have empty ranges
        windows = [(dt(2026, 4, 13, 22, 0), dt(2026, 4, 14, 4, 0))]
        payload = captured_weekly_payload(windows)
        for day in payload["days"]:
            if day["dayOfWeek"] != 0:
                self.assertEqual(day["ranges"], [],
                    f"dayOfWeek={day['dayOfWeek']} should be empty")

    def test_is_enabled_true(self):
        windows = [(dt(2026, 4, 13, 22, 0), dt(2026, 4, 14, 4, 0))]
        payload = captured_weekly_payload(windows)
        self.assertTrue(payload["isEnabled"])

    def test_time_format(self):
        # Start 22:30 UTC, stop 05:15 UTC
        windows = [(dt(2026, 4, 13, 22, 30), dt(2026, 4, 14, 5, 15))]
        payload = captured_weekly_payload(windows)
        r = payload["days"][0]["ranges"][0]
        self.assertEqual(r["startTime"], "22:30Z")
        self.assertEqual(r["stopTime"], "05:15Z")

    def test_charging_current_limit(self):
        windows = [(dt(2026, 4, 13, 22, 0), dt(2026, 4, 14, 4, 0))]
        payload = captured_weekly_payload(windows, rate=10)
        r = payload["days"][0]["ranges"][0]
        self.assertEqual(r["chargingCurrentLimit"], 10)

    def test_two_windows_same_day(self):
        # Split plan: both windows on Tuesday
        windows = [
            (dt(2026, 4, 14, 21, 0), dt(2026, 4, 14, 23, 0)),
            (dt(2026, 4, 14, 23, 30), dt(2026, 4, 15, 1, 30)),
        ]
        payload = captured_weekly_payload(windows)
        tuesday_ranges = payload["days"][1]["ranges"]
        self.assertEqual(len(tuesday_ranges), 2)

    def test_two_windows_different_days(self):
        # Split plan spanning midnight UTC: one window Sunday, one Monday
        windows = [
            (dt(2026, 4, 19, 22, 0), dt(2026, 4, 20, 0, 0)),
            (dt(2026, 4, 20, 0, 0),  dt(2026, 4, 20, 2, 0)),
        ]
        payload = captured_weekly_payload(windows)
        sunday_ranges = payload["days"][6]["ranges"]
        monday_ranges = payload["days"][0]["ranges"]
        self.assertEqual(len(sunday_ranges), 1)
        self.assertEqual(len(monday_ranges), 1)


# ===========================================================================
# _easee_put_basic_plan — payload structure
# ===========================================================================

class TestBasicPlanPayload(unittest.TestCase):

    def test_payload_fields(self):
        start = dt(2026, 4, 13, 22, 0)
        stop  = dt(2026, 4, 14, 4, 30)
        payload = captured_basic_payload(start, stop)
        self.assertEqual(payload["id"], "CHARGER1")
        self.assertEqual(payload["chargeStartTime"], "2026-04-13T22:00:00.000Z")
        self.assertEqual(payload["chargeStopTime"],  "2026-04-14T04:30:00.000Z")
        self.assertFalse(payload["repeat"])
        self.assertTrue(payload["isEnabled"])

    def test_repeat_is_false(self):
        payload = captured_basic_payload(dt(2026, 4, 13, 22, 0), dt(2026, 4, 14, 4, 0))
        self.assertFalse(payload["repeat"])

    def test_datetime_format(self):
        # Verify the exact ISO format with milliseconds and Z suffix
        payload = captured_basic_payload(dt(2026, 4, 13, 23, 45), dt(2026, 4, 14, 6, 15))
        self.assertRegex(payload["chargeStartTime"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.000Z$")
        self.assertRegex(payload["chargeStopTime"],  r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.000Z$")


# ===========================================================================
# deliver() — routing: single window → basic plan, multiple → weekly plan
# ===========================================================================

class TestDeliverRouting(unittest.TestCase):

    def _run_deliver(self, plan: dict, entry: dict | None = None):
        if entry is None:
            entry = make_entry()
        with mock.patch("deliver_easee._easee_login", return_value="tok"), \
             mock.patch("deliver_easee._easee_put_basic_plan") as mock_basic, \
             mock.patch("deliver_easee._easee_put_weekly_plan") as mock_weekly:
            result = deliver(plan, "CHARGER1", entry, "Europe/Helsinki")
            return result, mock_basic, mock_weekly

    def test_single_window_calls_basic_plan(self):
        plan = make_plan(
            ["2026-04-13T22:00:00Z"],
            ["2026-04-14T04:00:00Z"],
        )
        result, mock_basic, mock_weekly = self._run_deliver(plan)
        self.assertTrue(result)
        mock_basic.assert_called_once()
        mock_weekly.assert_not_called()

    def test_multiple_windows_calls_weekly_plan(self):
        plan = make_plan(
            ["2026-04-13T21:00:00Z", "2026-04-13T23:30:00Z"],
            ["2026-04-13T23:00:00Z", "2026-04-14T01:30:00Z"],
        )
        result, mock_basic, mock_weekly = self._run_deliver(plan)
        self.assertTrue(result)
        mock_weekly.assert_called_once()
        mock_basic.assert_not_called()

    def test_empty_plan_returns_true_without_api_call(self):
        plan = make_plan([], [])
        result, mock_basic, mock_weekly = self._run_deliver(plan)
        self.assertTrue(result)
        mock_basic.assert_not_called()
        mock_weekly.assert_not_called()

    def test_login_failure_returns_false(self):
        plan = make_plan(["2026-04-13T22:00:00Z"], ["2026-04-14T04:00:00Z"])
        with mock.patch("deliver_easee._easee_login", side_effect=RuntimeError("bad creds")):
            result = deliver(plan, "CHARGER1", make_entry(), "Europe/Helsinki")
        self.assertFalse(result)

    def test_basic_plan_failure_returns_false(self):
        plan = make_plan(["2026-04-13T22:00:00Z"], ["2026-04-14T04:00:00Z"])
        with mock.patch("deliver_easee._easee_login", return_value="tok"), \
             mock.patch("deliver_easee._easee_put_basic_plan", side_effect=Exception("API down")):
            result = deliver(plan, "CHARGER1", make_entry(), "Europe/Helsinki")
        self.assertFalse(result)

    def test_weekly_plan_failure_returns_false(self):
        plan = make_plan(
            ["2026-04-13T21:00:00Z", "2026-04-13T23:30:00Z"],
            ["2026-04-13T23:00:00Z", "2026-04-14T01:30:00Z"],
        )
        with mock.patch("deliver_easee._easee_login", return_value="tok"), \
             mock.patch("deliver_easee._easee_put_weekly_plan", side_effect=Exception("API down")):
            result = deliver(plan, "CHARGER1", make_entry(), "Europe/Helsinki")
        self.assertFalse(result)

    def test_max_charging_rate_passed_to_weekly_plan(self):
        plan = make_plan(
            ["2026-04-13T21:00:00Z", "2026-04-13T23:30:00Z"],
            ["2026-04-13T23:00:00Z", "2026-04-14T01:30:00Z"],
        )
        with mock.patch("deliver_easee._easee_login", return_value="tok"), \
             mock.patch("deliver_easee._easee_put_weekly_plan") as mock_weekly:
            deliver(plan, "CHARGER1", make_entry(max_charging_rate=10), "Europe/Helsinki")
            _, kwargs = mock_weekly.call_args
            self.assertEqual(kwargs.get("max_charging_rate") or mock_weekly.call_args[0][2], 10)

    def test_basic_plan_receives_correct_datetimes(self):
        plan = make_plan(
            ["2026-04-13T22:00:00Z"],
            ["2026-04-14T04:30:00Z"],
        )
        with mock.patch("deliver_easee._easee_login", return_value="tok"), \
             mock.patch("deliver_easee._easee_put_basic_plan") as mock_basic:
            deliver(plan, "CHARGER1", make_entry(), "Europe/Helsinki")
            args = mock_basic.call_args[0]
            # args: charger_id, start_utc, stop_utc, token
            start, stop = args[1], args[2]
            self.assertEqual(start, dt(2026, 4, 13, 22, 0))
            self.assertEqual(stop,  dt(2026, 4, 14, 4, 30))


# ===========================================================================

if __name__ == "__main__":
    unittest.main(verbosity=2)
