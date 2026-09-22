"""
Tests for deliver.py (the dispatcher)
=======================================
Focuses on should_skip_redundant_delivery — the logic that prevents two
close-together triggers (an unreliable scheduled cron plus a manual backup
trigger) from re-delivering redundantly or unsafely — and the persisted
delivered-record read/write helpers it depends on. See CONTEXT.md
"Redundant delivery protection" for the full rationale.

Run from the repo root:
    python -m unittest test_deliver.py -v
"""

import json
import sys
import tempfile
import unittest
import unittest.mock as mock
from pathlib import Path

sys.path.insert(0, ".")
sys.path.insert(0, "..")

import deliver
from deliver import (
    _delivered_record_path,
    _load_delivered_record,
    _save_delivered_record,
    dispatch,
    should_skip_redundant_delivery,
)


# ===========================================================================
# Helpers
# ===========================================================================

def make_plan(
    window_starts_utc=("2026-03-17T19:00:00+00:00",),
    window_ends_utc=("2026-03-17T20:00:00+00:00",),
    generated_at="2026-03-17T19:05:00+00:00",
    configured_window_start_utc="2026-03-17T19:00:00+00:00",
    schedule_uses_forecast=False,
    profile="overnight",
) -> dict:
    return {
        "profile": profile,
        "window_starts_utc": list(window_starts_utc),
        "window_ends_utc": list(window_ends_utc),
        "generated_at": generated_at,
        "configured_window_start_utc": configured_window_start_utc,
        "schedule_uses_forecast": schedule_uses_forecast,
    }


def make_record(**overrides) -> dict:
    plan = make_plan(**overrides)
    return {
        "window_starts_utc": plan["window_starts_utc"],
        "window_ends_utc": plan["window_ends_utc"],
        "generated_at": plan["generated_at"],
        "configured_window_start_utc": plan["configured_window_start_utc"],
        "schedule_uses_forecast": plan["schedule_uses_forecast"],
    }


# ===========================================================================
# should_skip_redundant_delivery
# ===========================================================================

class TestShouldSkipRedundantDelivery(unittest.TestCase):

    def test_no_prior_record_delivers(self):
        plan = make_plan()
        self.assertFalse(should_skip_redundant_delivery(plan, None, "overnight"))

    def test_prior_forecast_based_always_delivers(self):
        # Case 1: a forecast-based prior schedule must always yield, even
        # when the new plan's windows are byte-identical to the prior one.
        plan = make_plan()
        prior = make_record(schedule_uses_forecast=True)
        self.assertFalse(should_skip_redundant_delivery(plan, prior, "overnight"))

    def test_prior_forecast_based_delivers_even_when_new_run_is_live(self):
        # Case 3: forecast-override takes priority over live-window
        # protection — a forecast-based prior must yield even mid-window.
        prior = make_record(
            generated_at="2026-03-17T14:00:00+00:00",          # predates the window
            configured_window_start_utc="2026-03-17T19:00:00+00:00",
            schedule_uses_forecast=True,
        )
        plan = make_plan(
            generated_at="2026-03-17T22:00:00+00:00",          # live — after window start
            configured_window_start_utc="2026-03-17T19:00:00+00:00",
            window_starts_utc=("2026-03-17T22:00:00+00:00",),  # different from prior
        )
        self.assertFalse(should_skip_redundant_delivery(plan, prior, "overnight"))

    def test_prior_forecast_based_and_new_also_forecast_identical_skips(self):
        # The gap this fix closes: two close-together forecast-based runs
        # (both still before real prices publish) with identical windows
        # shouldn't force a redundant redelivery just because the prior
        # happened to be an estimate — that's not a correction, it's the
        # same estimate confirmed again, and burns an API call for nothing.
        prior = make_record(schedule_uses_forecast=True)
        plan = make_plan(schedule_uses_forecast=True)  # same default windows as make_record
        self.assertTrue(should_skip_redundant_delivery(plan, prior, "overnight"))

    def test_prior_forecast_based_and_new_forecast_but_different_windows_delivers(self):
        # Two forecast-based runs, but the forecast itself moved between
        # them (different windows) — still a real correction, deliver.
        prior = make_record(schedule_uses_forecast=True)
        plan = make_plan(schedule_uses_forecast=True,
                         window_starts_utc=("2026-03-17T20:00:00+00:00",))
        self.assertFalse(should_skip_redundant_delivery(plan, prior, "overnight"))

    def test_live_run_with_prewindow_prior_same_instance_skips(self):
        # Case 2: the prior plan was built before its own window opened
        # (14:00, window starts 19:00) and this run is live (22:00, after
        # 19:00) targeting that exact same window instance — skip, the
        # prior plan already has a committed session running.
        prior = make_record(
            generated_at="2026-03-17T14:00:00+00:00",
            configured_window_start_utc="2026-03-17T19:00:00+00:00",
        )
        plan = make_plan(
            generated_at="2026-03-17T22:00:00+00:00",
            configured_window_start_utc="2026-03-17T19:00:00+00:00",
            window_starts_utc=("2026-03-17T22:00:00+00:00",),  # clamped-to-now, differs from prior
        )
        self.assertTrue(should_skip_redundant_delivery(plan, prior, "overnight"))

    def test_live_run_different_window_instance_does_not_skip(self):
        # The exact regression scenario from the design discussion: prior
        # plan is for MONDAY night, already delivered and completed. A
        # later, unrelated live run is for TUESDAY night. Must not be
        # blocked just because *some* prior plan predates *its own* window.
        prior = make_record(
            generated_at="2026-03-16T14:00:00+00:00",
            configured_window_start_utc="2026-03-16T19:00:00+00:00",  # Monday
            window_starts_utc=("2026-03-16T19:00:00+00:00",),
            window_ends_utc=("2026-03-17T04:30:00+00:00",),
        )
        plan = make_plan(
            generated_at="2026-03-17T22:00:00+00:00",
            configured_window_start_utc="2026-03-17T19:00:00+00:00",  # Tuesday — different instance
            window_starts_utc=("2026-03-17T22:00:00+00:00",),
            window_ends_utc=("2026-03-18T04:30:00+00:00",),
        )
        self.assertFalse(should_skip_redundant_delivery(plan, prior, "overnight"))

    def test_upcoming_run_with_prewindow_prior_falls_through_to_diff(self):
        # Same window instance, but this run is NOT live (still before the
        # window opens) — the live-window protection doesn't apply; falls
        # through to the plain diff check instead.
        prior = make_record(
            generated_at="2026-03-17T10:00:00+00:00",
            configured_window_start_utc="2026-03-17T19:00:00+00:00",
            window_starts_utc=("2026-03-17T19:00:00+00:00",),
        )
        plan = make_plan(
            generated_at="2026-03-17T14:00:00+00:00",   # still before 19:00 — not live
            configured_window_start_utc="2026-03-17T19:00:00+00:00",
            window_starts_utc=("2026-03-17T19:00:00+00:00",),  # happens to match — unchanged
        )
        self.assertTrue(should_skip_redundant_delivery(plan, prior, "overnight"),
                        "not live, but windows are identical — plain diff should still skip")

    def test_identical_windows_skips(self):
        prior = make_record()
        plan = make_plan()
        self.assertTrue(should_skip_redundant_delivery(plan, prior, "overnight"))

    def test_different_windows_delivers(self):
        prior = make_record(window_starts_utc=("2026-03-17T19:00:00+00:00",))
        plan = make_plan(window_starts_utc=("2026-03-17T20:00:00+00:00",))
        self.assertFalse(should_skip_redundant_delivery(plan, prior, "overnight"))

    def test_different_window_ends_delivers(self):
        prior = make_record(window_ends_utc=("2026-03-17T20:00:00+00:00",))
        plan = make_plan(window_ends_utc=("2026-03-17T21:00:00+00:00",))
        self.assertFalse(should_skip_redundant_delivery(plan, prior, "overnight"))

    def test_missing_timestamps_falls_back_to_diff_only(self):
        # A record from before these fields existed (or malformed) — must
        # not crash, and the live-window protection simply doesn't apply.
        prior = {"window_starts_utc": ["2026-03-17T19:00:00+00:00"],
                 "window_ends_utc": ["2026-03-17T20:00:00+00:00"]}
        plan = make_plan()
        self.assertTrue(should_skip_redundant_delivery(plan, prior, "overnight"))

    def test_live_window_protection_is_independent_of_plan_completeness(self):
        # The function never looks at required_minutes, total_minutes, or
        # plan_warning at all — a fully-satisfied, warning-free plan is
        # protected from redundant redelivery exactly the same as a partial
        # one would be. Planning correctness and delivery safety are
        # orthogonal checks; verified here with the actual field shape a
        # real "requirement fully met, run after midnight" plan produces.
        prior = make_record(
            generated_at="2026-03-14T14:00:00+00:00",           # normal pre-window run
            configured_window_start_utc="2026-03-14T19:00:00+00:00",
            window_starts_utc=("2026-03-14T19:00:00+00:00",),
            window_ends_utc=("2026-03-14T21:00:00+00:00",),
        )
        plan = make_plan(
            generated_at="2026-03-15T01:30:00+00:00",           # after midnight, live
            configured_window_start_utc="2026-03-14T19:00:00+00:00",
            window_starts_utc=("2026-03-15T01:30:00+00:00",),
            window_ends_utc=("2026-03-15T03:30:00+00:00",),
        )
        # plan.get("plan_warning") is deliberately absent here — this dict
        # has no completeness signal at all, by design.
        self.assertNotIn("plan_warning", plan)
        self.assertTrue(should_skip_redundant_delivery(plan, prior, "overnight"))


# ===========================================================================
# Persisted record read/write
# ===========================================================================

class TestDeliveredRecordPersistence(unittest.TestCase):

    def test_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _delivered_record_path(tmp, "overnight", "myskoda", "VIN123")
            plan = make_plan()
            _save_delivered_record(path, plan)
            record = _load_delivered_record(path)
            self.assertEqual(record["window_starts_utc"], plan["window_starts_utc"])
            self.assertEqual(record["schedule_uses_forecast"], plan["schedule_uses_forecast"])

    def test_missing_file_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _delivered_record_path(tmp, "overnight", "myskoda", "VIN123")
            self.assertIsNone(_load_delivered_record(path))

    def test_corrupt_file_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _delivered_record_path(tmp, "overnight", "myskoda", "VIN123")
            path.write_text("{not valid json")
            self.assertIsNone(_load_delivered_record(path))

    def test_record_path_sanitizes_charge_point_id(self):
        # A VIN or charger ID could contain characters unsafe for filenames.
        path = _delivered_record_path("data", "overnight", "chargeamps", "charger/id:1")
        self.assertNotIn("/", path.name)
        self.assertNotIn(":", path.name)

    def test_distinct_chargers_get_distinct_records(self):
        p1 = _delivered_record_path("data", "overnight", "myskoda", "VIN1")
        p2 = _delivered_record_path("data", "overnight", "myskoda", "VIN2")
        self.assertNotEqual(p1, p2)

    def test_save_creates_data_dir_if_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            nested = Path(tmp) / "does" / "not" / "exist"
            path = _delivered_record_path(str(nested), "overnight", "myskoda", "VIN1")
            _save_delivered_record(path, make_plan())
            self.assertTrue(path.exists())


# ===========================================================================
# dispatch() integration — redundant-delivery protection end to end
# ===========================================================================

class TestDispatchRedundantDelivery(unittest.TestCase):

    CONFIG = {
        "entsoe": {"timezone": "Europe/Helsinki"},
        "charging": [{
            "name": "overnight",
            "deliveries": [{
                "handler": "myskoda",
                "charge_point_id": "SKODA_VIN",
            }],
        }],
    }

    def _fake_handler(self, return_value=True):
        module = mock.MagicMock()
        module.deliver = mock.MagicMock(return_value=return_value)
        return module

    def test_second_identical_run_skips_handler_call(self):
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.dict("os.environ", {"SKODA_VIN": "VIN123"}), \
             mock.patch("deliver._load_handler", return_value=self._fake_handler()) as load:
            plan = make_plan()
            ok1 = dispatch({"overnight": plan}, self.CONFIG, data_dir=tmp)
            self.assertTrue(ok1)
            handler = load.return_value
            self.assertEqual(handler.deliver.call_count, 1)

            ok2 = dispatch({"overnight": plan}, self.CONFIG, data_dir=tmp)
            self.assertTrue(ok2)
            self.assertEqual(handler.deliver.call_count, 1, "second identical run must not redeliver")

    def test_changed_plan_delivers_again(self):
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.dict("os.environ", {"SKODA_VIN": "VIN123"}), \
             mock.patch("deliver._load_handler", return_value=self._fake_handler()) as load:
            handler = load.return_value
            dispatch({"overnight": make_plan()}, self.CONFIG, data_dir=tmp)
            self.assertEqual(handler.deliver.call_count, 1)

            changed = make_plan(window_starts_utc=("2026-03-17T20:00:00+00:00",))
            dispatch({"overnight": changed}, self.CONFIG, data_dir=tmp)
            self.assertEqual(handler.deliver.call_count, 2)

    def test_failed_delivery_does_not_persist_record(self):
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.dict("os.environ", {"SKODA_VIN": "VIN123"}), \
             mock.patch("deliver._load_handler", return_value=self._fake_handler(return_value=False)) as load:
            handler = load.return_value
            plan = make_plan()
            ok1 = dispatch({"overnight": plan}, self.CONFIG, data_dir=tmp)
            self.assertFalse(ok1)

            # A second run must still attempt delivery — nothing was
            # recorded since the first attempt failed.
            ok2 = dispatch({"overnight": plan}, self.CONFIG, data_dir=tmp)
            self.assertFalse(ok2)
            self.assertEqual(handler.deliver.call_count, 2)

    def test_forecast_based_prior_always_redelivers(self):
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.dict("os.environ", {"SKODA_VIN": "VIN123"}), \
             mock.patch("deliver._load_handler", return_value=self._fake_handler()) as load:
            handler = load.return_value
            forecast_plan = make_plan(schedule_uses_forecast=True)
            dispatch({"overnight": forecast_plan}, self.CONFIG, data_dir=tmp)
            self.assertEqual(handler.deliver.call_count, 1)

            # Same windows, but the prior was forecast-based — must redeliver.
            real_plan = make_plan(schedule_uses_forecast=False)
            dispatch({"overnight": real_plan}, self.CONFIG, data_dir=tmp)
            self.assertEqual(handler.deliver.call_count, 2)

    def test_identical_forecast_based_rerun_skips(self):
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.dict("os.environ", {"SKODA_VIN": "VIN123"}), \
             mock.patch("deliver._load_handler", return_value=self._fake_handler()) as load:
            handler = load.return_value
            forecast_plan = make_plan(schedule_uses_forecast=True)
            dispatch({"overnight": forecast_plan}, self.CONFIG, data_dir=tmp)
            self.assertEqual(handler.deliver.call_count, 1)

            # Triggered again before prices publish — same forecast, same
            # windows. Must not redeliver just because the prior was an
            # estimate; nothing actually changed.
            dispatch({"overnight": forecast_plan}, self.CONFIG, data_dir=tmp)
            self.assertEqual(handler.deliver.call_count, 1,
                             "identical forecast-based redelivery must be skipped")


if __name__ == "__main__":
    unittest.main()
