"""
Tests for deliver_myskoda.py
=============================
Focuses on the multi-window slot mapping (window 1 -> slot 1, window 2 ->
slot 2, ...), the max_windows / vehicle-slot-count validation, and the
charging-state safety logic that decides whether to preserve unused slots
or change the charge mode.

Run from the repo root:
    python -m unittest test_deliver_myskoda.py -v
"""

import copy
import sys
import unittest
import unittest.mock as mock

sys.path.insert(0, ".")
sys.path.insert(0, "..")

import deliver_myskoda
from deliver_myskoda import _build_updated_profile, deliver


# ===========================================================================
# Helpers
# ===========================================================================

def make_profile(n_slots: int = 4) -> dict:
    """A charging profile with n_slots preferredChargingTimes entries,
    all disabled, distinct placeholder times so overwrite/preserve is
    observable in assertions."""
    return {
        "id": "profile-1",
        "name": "Home",
        "preferredChargingTimes": [
            {"id": f"slot-{i}", "enabled": False,
             "startTime": "00:00", "endTime": "00:00"}
            for i in range(n_slots)
        ],
    }


def make_plan(window_starts_utc: list[str], window_ends_utc: list[str],
             max_windows=1, total_minutes: int = 60, profile: str = "overnight") -> dict:
    return {
        "profile": profile,
        "window_starts_utc": window_starts_utc,
        "window_ends_utc": window_ends_utc,
        "max_windows": max_windows,
        "total_minutes": total_minutes,
    }


def make_entry(**overrides) -> dict:
    defaults = {"api_key_env": "SKODA_API_KEY"}
    defaults.update(overrides)
    return defaults


def make_vehicle_response(
    profiles: list[dict] | None = None,
    is_at_location: bool = True,
    charging_state: str = "NOT_READY",
    active_mode: str = "",
) -> dict:
    if profiles is None:
        profiles = [make_profile()]
    return {
        "vehicle": {
            "chargingProfiles": {"profiles": profiles},
            "charging": {
                "isVehicleInSavedLocation": is_at_location,
                "status": {"state": charging_state},
                "settings": {"preferredChargeMode": active_mode},
            },
        }
    }


# ===========================================================================
# _build_updated_profile — slot mapping
# ===========================================================================

class TestBuildUpdatedProfile(unittest.TestCase):

    def test_single_window_sets_slot_1_only(self):
        profile = make_profile(4)
        updated = _build_updated_profile(profile, [("21:00", "23:00")])
        slots = updated["preferredChargingTimes"]
        self.assertTrue(slots[0]["enabled"])
        self.assertEqual(slots[0]["startTime"], "21:00")
        self.assertEqual(slots[0]["endTime"], "23:00")
        for s in slots[1:]:
            self.assertFalse(s["enabled"])

    def test_two_windows_set_slots_1_and_2(self):
        profile = make_profile(4)
        updated = _build_updated_profile(
            profile, [("21:00", "23:00"), ("01:00", "03:00")]
        )
        slots = updated["preferredChargingTimes"]
        self.assertTrue(slots[0]["enabled"])
        self.assertEqual((slots[0]["startTime"], slots[0]["endTime"]), ("21:00", "23:00"))
        self.assertTrue(slots[1]["enabled"])
        self.assertEqual((slots[1]["startTime"], slots[1]["endTime"]), ("01:00", "03:00"))
        self.assertFalse(slots[2]["enabled"])
        self.assertFalse(slots[3]["enabled"])

    def test_four_windows_fill_all_slots(self):
        profile = make_profile(4)
        windows = [("21:00", "22:00"), ("22:00", "23:00"),
                   ("23:00", "00:00"), ("00:00", "01:00")]
        updated = _build_updated_profile(profile, windows)
        slots = updated["preferredChargingTimes"]
        for i, (start, end) in enumerate(windows):
            self.assertTrue(slots[i]["enabled"])
            self.assertEqual((slots[i]["startTime"], slots[i]["endTime"]), (start, end))

    def test_windows_written_in_plan_order_not_price_order(self):
        # The mapping is purely positional — window 1 always goes to slot 1,
        # regardless of what the window "means"; ordering is the caller's
        # responsibility (plan windows are chronological).
        profile = make_profile(4)
        updated = _build_updated_profile(
            profile, [("05:00", "06:00"), ("21:00", "22:00")]
        )
        slots = updated["preferredChargingTimes"]
        self.assertEqual(slots[0]["startTime"], "05:00")
        self.assertEqual(slots[1]["startTime"], "21:00")

    def test_unused_slots_disabled_by_default(self):
        profile = make_profile(4)
        profile["preferredChargingTimes"][2]["enabled"] = True  # pre-existing
        profile["preferredChargingTimes"][3]["enabled"] = True
        updated = _build_updated_profile(profile, [("21:00", "23:00")])
        slots = updated["preferredChargingTimes"]
        self.assertFalse(slots[2]["enabled"])
        self.assertFalse(slots[3]["enabled"])

    def test_preserve_other_slots_leaves_unused_slots_untouched(self):
        profile = make_profile(4)
        profile["preferredChargingTimes"][2]["enabled"] = True
        profile["preferredChargingTimes"][2]["startTime"] = "10:00"
        updated = _build_updated_profile(
            profile, [("21:00", "23:00")], preserve_other_slots=True
        )
        slots = updated["preferredChargingTimes"]
        # Slot we wrote to is still overwritten
        self.assertTrue(slots[0]["enabled"])
        self.assertEqual(slots[0]["startTime"], "21:00")
        # Unused slot is untouched — enabled state AND time preserved
        self.assertTrue(slots[2]["enabled"])
        self.assertEqual(slots[2]["startTime"], "10:00")

    def test_more_windows_than_slots_raises(self):
        profile = make_profile(2)  # only 2 slots configured on vehicle
        with self.assertRaises(ValueError):
            _build_updated_profile(
                profile, [("21:00", "22:00"), ("22:00", "23:00"), ("23:00", "00:00")]
            )

    def test_no_slots_on_profile_raises(self):
        profile = {"id": "p1", "name": "Home", "preferredChargingTimes": []}
        with self.assertRaises(ValueError):
            _build_updated_profile(profile, [("21:00", "23:00")])

    def test_original_profile_not_mutated(self):
        profile = make_profile(4)
        original = copy.deepcopy(profile)
        _build_updated_profile(profile, [("21:00", "23:00")])
        self.assertEqual(profile, original)

    def test_slot_id_preserved(self):
        profile = make_profile(4)
        updated = _build_updated_profile(profile, [("21:00", "23:00")])
        self.assertEqual(updated["preferredChargingTimes"][0]["id"], "slot-0")


# ===========================================================================
# deliver() — max_windows / vehicle-slot-count validation
# ===========================================================================

class TestDeliverValidation(unittest.TestCase):

    def _run(self, plan, entry=None):
        entry = entry or make_entry()
        with mock.patch.dict("os.environ", {"SKODA_API_KEY": "key"}), \
             mock.patch("deliver_myskoda._get_charging_profiles",
                        return_value=make_vehicle_response()) as mock_get, \
             mock.patch("deliver_myskoda._put_profile") as mock_put, \
             mock.patch("deliver_myskoda._put_charge_mode") as mock_mode:
            result = deliver(plan, "VIN123", entry, "Europe/Helsinki")
            return result, mock_get, mock_put, mock_mode

    def test_max_windows_null_rejected(self):
        plan = make_plan(["2026-09-15T21:00:00Z"], ["2026-09-15T23:00:00Z"],
                         max_windows=None)
        result, _, mock_put, _ = self._run(plan)
        self.assertFalse(result)
        mock_put.assert_not_called()

    def test_max_windows_above_4_rejected(self):
        plan = make_plan(["2026-09-15T21:00:00Z"], ["2026-09-15T23:00:00Z"],
                         max_windows=5)
        result, _, mock_put, _ = self._run(plan)
        self.assertFalse(result)
        mock_put.assert_not_called()

    def test_max_windows_1_to_4_accepted(self):
        for n in (1, 2, 3, 4):
            plan = make_plan(["2026-09-15T21:00:00Z"], ["2026-09-15T23:00:00Z"],
                             max_windows=n)
            result, _, mock_put, _ = self._run(plan)
            self.assertTrue(result, f"max_windows={n} should be accepted")
            mock_put.assert_called_once()

    def test_actual_windows_exceed_vehicle_slots_rejected(self):
        # Defensive check: even if max_windows is nominally valid, more than
        # 4 actual windows in the plan must be rejected (vehicle has 4 slots).
        starts = [f"2026-09-15T{h:02d}:00:00Z" for h in (18, 19, 20, 21, 22)]
        ends   = [f"2026-09-15T{h:02d}:30:00Z" for h in (18, 19, 20, 21, 22)]
        plan = make_plan(starts, ends, max_windows=4, total_minutes=150)
        result, _, mock_put, _ = self._run(plan)
        self.assertFalse(result)
        mock_put.assert_not_called()

    def test_zero_total_minutes_rejected(self):
        plan = make_plan([], [], max_windows=1, total_minutes=0)
        result, mock_get, mock_put, _ = self._run(plan)
        self.assertFalse(result)
        mock_get.assert_not_called()
        mock_put.assert_not_called()

    def test_empty_windows_rejected(self):
        plan = make_plan([], [], max_windows=1, total_minutes=60)
        result, _, mock_put, _ = self._run(plan)
        self.assertFalse(result)
        mock_put.assert_not_called()

    def test_missing_api_key_rejected(self):
        plan = make_plan(["2026-09-15T21:00:00Z"], ["2026-09-15T23:00:00Z"])
        with mock.patch.dict("os.environ", {}, clear=True):
            result = deliver(plan, "VIN123", make_entry(), "Europe/Helsinki")
        self.assertFalse(result)


# ===========================================================================
# deliver() — slot mapping end-to-end (via mocked _put_profile payload)
# ===========================================================================

class TestDeliverSlotMapping(unittest.TestCase):

    def _captured_payload(self, plan, profiles=None):
        with mock.patch.dict("os.environ", {"SKODA_API_KEY": "key"}), \
             mock.patch("deliver_myskoda._get_charging_profiles",
                        return_value=make_vehicle_response(profiles=profiles)), \
             mock.patch("deliver_myskoda._put_profile") as mock_put, \
             mock.patch("deliver_myskoda._put_charge_mode"):
            result = deliver(plan, "VIN123", make_entry(), "Europe/Helsinki")
        self.assertTrue(result)
        _, kwargs = mock_put.call_args
        # _put_profile(vin, profile, api_key) — profile is positional arg[1]
        profile_arg = mock_put.call_args[0][1]
        return profile_arg

    def test_three_windows_fill_first_three_slots(self):
        plan = make_plan(
            ["2026-09-15T18:00:00Z", "2026-09-15T20:00:00Z", "2026-09-15T22:00:00Z"],
            ["2026-09-15T19:00:00Z", "2026-09-15T21:00:00Z", "2026-09-15T23:00:00Z"],
            max_windows=3, total_minutes=180,
        )
        profile = self._captured_payload(plan)
        slots = profile["preferredChargingTimes"]
        self.assertTrue(slots[0]["enabled"])
        self.assertTrue(slots[1]["enabled"])
        self.assertTrue(slots[2]["enabled"])
        self.assertFalse(slots[3]["enabled"])

    def test_windows_converted_to_local_time(self):
        # 21:00 UTC in winter Helsinki (UTC+2) is 23:00 local
        plan = make_plan(["2026-01-15T21:00:00Z"], ["2026-01-15T23:00:00Z"],
                         max_windows=1)
        profile = self._captured_payload(plan)
        slot0 = profile["preferredChargingTimes"][0]
        self.assertEqual(slot0["startTime"], "23:00")
        self.assertEqual(slot0["endTime"], "01:00")


# ===========================================================================
# deliver() — charging-state safety logic
# ===========================================================================

class TestDeliverChargingState(unittest.TestCase):

    def _run(self, charging_state, active_mode, profiles=None):
        plan = make_plan(["2026-09-15T21:00:00Z"], ["2026-09-15T23:00:00Z"],
                         max_windows=1)
        vresp = make_vehicle_response(
            profiles=profiles, charging_state=charging_state, active_mode=active_mode,
        )
        with mock.patch.dict("os.environ", {"SKODA_API_KEY": "key"}), \
             mock.patch("deliver_myskoda._get_charging_profiles", return_value=vresp), \
             mock.patch("deliver_myskoda._put_profile") as mock_put, \
             mock.patch("deliver_myskoda._put_charge_mode") as mock_mode:
            result = deliver(plan, "VIN123", make_entry(), "Europe/Helsinki")
        return result, mock_put, mock_mode

    def test_not_charging_updates_slot_and_mode(self):
        result, mock_put, mock_mode = self._run("READY", "")
        self.assertTrue(result)
        mock_put.assert_called_once()
        mock_mode.assert_called_once()

    def test_charging_in_manual_mode_updates_slot_but_not_mode(self):
        result, mock_put, mock_mode = self._run("CHARGING", "MANUAL")
        self.assertTrue(result)
        mock_put.assert_called_once()
        mock_mode.assert_not_called()

    def test_charging_in_preferred_times_preserves_unused_slots(self):
        profiles = [make_profile(4)]
        profiles[0]["preferredChargingTimes"][2]["enabled"] = True
        profiles[0]["preferredChargingTimes"][2]["startTime"] = "10:00"
        vresp = make_vehicle_response(
            profiles=profiles, charging_state="CHARGING",
            active_mode="PREFERRED_CHARGING_TIMES",
        )
        plan = make_plan(["2026-09-15T21:00:00Z"], ["2026-09-15T23:00:00Z"],
                         max_windows=1)
        with mock.patch.dict("os.environ", {"SKODA_API_KEY": "key"}), \
             mock.patch("deliver_myskoda._get_charging_profiles", return_value=vresp), \
             mock.patch("deliver_myskoda._put_profile") as mock_put, \
             mock.patch("deliver_myskoda._put_charge_mode") as mock_mode:
            result = deliver(plan, "VIN123", make_entry(), "Europe/Helsinki")
        self.assertTrue(result)
        mock_mode.assert_not_called()
        profile_arg = mock_put.call_args[0][1]
        self.assertTrue(profile_arg["preferredChargingTimes"][2]["enabled"])
        self.assertEqual(profile_arg["preferredChargingTimes"][2]["startTime"], "10:00")

    def test_charging_in_unknown_mode_skips_delivery_entirely(self):
        result, mock_put, mock_mode = self._run("CHARGING", "SOME_NEW_MODE")
        self.assertTrue(result)  # skip is not a failure
        mock_put.assert_not_called()
        mock_mode.assert_not_called()


if __name__ == "__main__":
    unittest.main()
