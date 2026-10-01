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
import logging
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

    def test_unchanged_plan_skip_reason_is_a_real_string(self):
        # should_skip_redundant_delivery returns the reason itself now, not
        # a bare bool, specifically so dispatch() can show it on the
        # delivery card without re-deriving the same wording separately.
        prior = make_record(schedule_uses_forecast=True)
        plan = make_plan(schedule_uses_forecast=True)
        reason = should_skip_redundant_delivery(plan, prior, "overnight")
        self.assertIsInstance(reason, str)
        self.assertIn("unchanged", reason)

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

    def test_live_window_skip_reason_is_a_real_string(self):
        prior = make_record(
            generated_at="2026-03-17T14:00:00+00:00",
            configured_window_start_utc="2026-03-17T19:00:00+00:00",
        )
        plan = make_plan(
            generated_at="2026-03-17T22:00:00+00:00",
            configured_window_start_utc="2026-03-17T19:00:00+00:00",
            window_starts_utc=("2026-03-17T22:00:00+00:00",),
        )
        reason = should_skip_redundant_delivery(plan, prior, "overnight")
        self.assertIsInstance(reason, str)
        self.assertIn("already delivered before it opened", reason)

    def test_live_run_identical_windows_reports_unchanged_not_interruption_risk(self):
        # Regression (2026-09-30 production log): GHA fired the scheduled run
        # ~5.5h late, after the window had opened. The live run re-derived
        # exactly the same windows as the pre-window delivery (all slots
        # were still ahead), but the card said redelivering "risks
        # interrupting" a plan — misleading, since nothing differed. The
        # skip is the same either way; the reason must be the accurate one.
        prior = make_record(
            generated_at="2026-03-17T14:00:00+00:00",
            configured_window_start_utc="2026-03-17T19:00:00+00:00",
        )
        plan = make_plan(
            generated_at="2026-03-17T19:02:00+00:00",   # live: after 19:00 window start
            configured_window_start_utc="2026-03-17T19:00:00+00:00",
        )
        reason = should_skip_redundant_delivery(plan, prior, "overnight")
        self.assertEqual(reason, "unchanged from the already-delivered plan")
        self.assertNotIn("interrupt", reason)

    def test_live_window_skip_reason_mentions_windows_differ(self):
        prior = make_record(
            generated_at="2026-03-17T14:00:00+00:00",
            configured_window_start_utc="2026-03-17T19:00:00+00:00",
        )
        plan = make_plan(
            generated_at="2026-03-17T22:00:00+00:00",
            configured_window_start_utc="2026-03-17T19:00:00+00:00",
            window_starts_utc=("2026-03-17T22:00:00+00:00",),
        )
        reason = should_skip_redundant_delivery(plan, prior, "overnight")
        self.assertIn("windows differ", reason)

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

    def test_skip_log_identifies_specific_handler_and_charger(self):
        # A profile can deliver to more than one charger — "Profile 'X':
        # skipping delivery" alone would be ambiguous about which of X's
        # chargers was actually skipped. The underlying decision has always
        # been scoped per (profile, handler, charge_point_id); the log
        # output needs to say so too, not just the return value.
        prior = make_record()
        plan = make_plan()
        with self.assertLogs("deliver", level="DEBUG") as cm:
            should_skip_redundant_delivery(plan, prior, "overnight", "myskoda", "VIN123")
        self.assertTrue(any("myskoda" in msg and "VIN123" in msg for msg in cm.output))

    def test_live_window_skip_log_also_identifies_specific_handler_and_charger(self):
        prior = make_record(
            generated_at="2026-03-17T14:00:00+00:00",
            configured_window_start_utc="2026-03-17T19:00:00+00:00",
        )
        plan = make_plan(
            generated_at="2026-03-17T22:00:00+00:00",
            configured_window_start_utc="2026-03-17T19:00:00+00:00",
            window_starts_utc=("2026-03-17T22:00:00+00:00",),
        )
        with self.assertLogs("deliver", level="DEBUG") as cm:
            should_skip_redundant_delivery(plan, prior, "overnight", "chargeamps", "CHG-42")
        self.assertTrue(any("chargeamps" in msg and "CHG-42" in msg for msg in cm.output))

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

class TestDeliverModuleConfigLoading(unittest.TestCase):
    """Regression coverage for a real bug: deliver.py used to have its own,
    completely independent load_config function — a bare yaml.safe_load with
    no call to charging_planner.translate_config at all. When config.yaml
    moved to the new profiles:/schedule:/delivery: format, charging_planner's
    own load_config (used by the planner) translated it correctly, but
    deliver.py's separate copy never got the update: it looked for the old
    charging: key, found nothing, and every profile's deliveries silently
    vanished — "No delivery entries found in any charging profile — nothing
    to do." No error, no traceback, just quietly not delivering.

    Every other test in this file calls deliver's functions directly within
    one already-imported Python process — which shares whatever module
    state already exists and would never have caught this, since the bug
    was specifically that deliver.py's own import/definition of load_config
    differed from charging_planner's. Only a real subprocess invocation,
    exercising deliver.py's actual __main__ entry point and its own
    sys.path/import resolution, reproduces the failure mode. That test is
    below; the fix — deleting the duplicate and importing
    charging_planner.load_config directly — makes divergence like this
    structurally impossible, not just currently absent.
    """

    def test_deliver_has_no_local_load_config(self):
        # The whole fix: there is exactly one load_config in this codebase.
        self.assertFalse(hasattr(deliver, "load_config"),
                         "deliver.py must not define its own load_config — "
                         "it must use deliver.cp.load_config (charging_planner's)")

    def test_deliver_uses_charging_planners_load_config(self):
        import charging_planner
        self.assertIs(deliver.cp.load_config, charging_planner.load_config)

    def test_new_format_config_found_via_deliver_modules_own_import(self):
        # Exercises deliver.py's own imported load_config, not a separately
        # imported charging_planner in the test process — proving the
        # translation actually happens on the path deliver.py itself uses.
        raw = {
            "area": "FI", "timezone": "Europe/Helsinki",
            "profiles": [{
                "name": "overnight",
                "schedule": {"mon-sun": {"window": "any-any", "required": 2}},
                "delivery": [{"myskoda": {"vin": "SKODA_VIN"}}],
            }],
        }
        with tempfile.TemporaryDirectory() as d:
            path = str(Path(d) / "config.yaml")
            import yaml
            with open(path, "w") as f:
                yaml.safe_dump(raw, f)
            cfg = deliver.cp.load_config(path)
            with mock.patch.dict("os.environ", {"SKODA_VIN": "SKODA_VIN_VALUE"}):
                entries = deliver._extract_deliveries(cfg)
        self.assertEqual(len(entries), 1)
        profile_name, timezone, entry, charge_point_ids = entries[0]
        self.assertEqual(profile_name, "overnight")
        self.assertEqual(entry["handler"], "myskoda")
        self.assertEqual(charge_point_ids, ["SKODA_VIN_VALUE"])

    def test_real_subprocess_finds_deliveries_with_new_format_config(self):
        # The test that actually reproduces the original bug: runs
        # delivery/deliver.py as a real subprocess, exactly as the GHA
        # workflow and a person on the command line both do — from the repo
        # root, as `python delivery/deliver.py ... --config config.yaml`.
        # Prior to the fix, this specific invocation path (not any direct
        # function call) printed "No delivery entries found" for a
        # perfectly valid new-format config.
        import os
        import subprocess

        repo_root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            config_path = d / "config.yaml"
            config_path.write_text(
                "area: FI\n"
                "timezone: Europe/Helsinki\n"
                "profiles:\n"
                "  - name: overnight\n"
                "    schedule:\n"
                "      mon-sun: { window: any-any, required: 2 }\n"
                "    delivery:\n"
                "      - myskoda: { vin: SKODA_VIN }\n"
            )
            plan_path = d / "plan-overnight.json"
            plan_path.write_text(json.dumps({
                "profile": "overnight", "date": "2026-09-24",
                "window_starts_utc": ["2026-09-24T20:30:00+00:00"],
                "window_ends_utc": ["2026-09-24T21:00:00+00:00"],
                "generated_at": "2026-09-24T11:45:33+00:00",
                "configured_window_start_utc": "2026-09-24T18:00:00+00:00",
                "schedule_uses_forecast": False,
            }))
            env = dict(os.environ, SKODA_VIN="VIN1", SKODA_API_KEY="dummy")
            result = subprocess.run(
                ["python3", str(repo_root / "delivery" / "deliver.py"),
                 str(plan_path), "--config", str(config_path)],
                cwd=str(d), env=env, capture_output=True, text=True, timeout=30,
            )
        combined = result.stdout + result.stderr
        self.assertNotIn("No delivery entries found", combined)
        self.assertIn("Delivering profile 'overnight'", combined)


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

    def test_record_path_does_not_expose_charge_point_id(self):
        # data/ is committed to the repo (typically public via GitHub
        # Pages) — a VIN or charger serial must never appear in the
        # filename in readable form. A filesystem-unsafe ID (e.g. a VIN
        # containing "/" or ":") is also handled correctly as a side effect
        # of hashing, but that's not the primary property being tested here.
        vin = "TMBJC7NY2MF019901"
        path = _delivered_record_path("data", "overnight", "myskoda", vin)
        self.assertNotIn(vin, path.name)
        self.assertNotIn("/", path.name)
        self.assertNotIn(":", path.name)

    def test_record_path_is_deterministic(self):
        # Same inputs must always hash to the same path, so a record
        # written by one run is found by the next.
        p1 = _delivered_record_path("data", "overnight", "myskoda", "VIN1")
        p2 = _delivered_record_path("data", "overnight", "myskoda", "VIN1")
        self.assertEqual(p1, p2)

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


class TestDispatchErrorCapture(unittest.TestCase):
    """dispatch()'s _LastErrorCapture attaches a real logging.Handler to the
    root logger for the duration of each handler call — real, new
    machinery with a real failure mode if the cleanup (`finally:
    logging.getLogger().removeHandler(capture)`) were ever wrong: handlers
    would accumulate across calls, or a later call could pick up a stale
    message from an earlier one. Covers the behavior, not print_delivery_card
    itself (see TestPrintDeliveryCard in test_charging_planner.py for that —
    a pure display function, light content checks only)."""

    CONFIG = {
        "entsoe": {"timezone": "Europe/Helsinki"},
        "charging": [{
            "name": "overnight",
            "deliveries": [{"handler": "myskoda", "charge_point_id": "SKODA_VIN"}],
        }],
    }

    def _fake_handler_that_logs_and_fails(self, message):
        module = mock.MagicMock()
        def _deliver(*a, **kw):
            logging.getLogger("deliver_myskoda").error(message)
            return False
        module.deliver = _deliver
        return module

    def test_bare_false_failure_reason_reaches_the_card(self):
        # The gap this closes: every handler's own normal failure path logs
        # via log.error then returns bare False, never raising — so
        # dispatch()'s except-Exception block alone would never see the
        # reason. Confirms the captured message actually reaches stdout via
        # the card, not just that dispatch() still returns False correctly.
        module = self._fake_handler_that_logs_and_fails("Charge Amps login failed: HTTP 401")
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.dict("os.environ", {"SKODA_VIN": "VIN123"}), \
             mock.patch("deliver._load_handler", return_value=module):
            out = self._capture_stdout(dispatch, {"overnight": make_plan()}, self.CONFIG, data_dir=tmp)
        self.assertIn("Charge Amps login failed: HTTP 401", out)

    def test_root_logger_has_no_leftover_capture_handler_after_dispatch(self):
        # If removeHandler were ever skipped (e.g. moved out of a finally:),
        # this would accumulate one extra handler on the root logger per
        # delivery attempt across the life of a long-running process.
        before = len(logging.getLogger().handlers)
        module = self._fake_handler_that_logs_and_fails("some failure")
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.dict("os.environ", {"SKODA_VIN": "VIN123"}), \
             mock.patch("deliver._load_handler", return_value=module):
            dispatch({"overnight": make_plan()}, self.CONFIG, data_dir=tmp)
        self.assertEqual(len(logging.getLogger().handlers), before)

    def test_second_calls_reason_is_not_stale_from_first(self):
        module1 = self._fake_handler_that_logs_and_fails("first failure")
        module2_mock = mock.MagicMock()
        module2_mock.deliver = mock.MagicMock(return_value=True)  # succeeds, logs nothing
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.dict("os.environ", {"SKODA_VIN": "VIN123"}), \
             mock.patch("deliver._load_handler", side_effect=[module1, module2_mock]):
            dispatch({"overnight": make_plan()}, self.CONFIG, data_dir=tmp)
            out = self._capture_stdout(dispatch, {"overnight": make_plan()}, self.CONFIG, data_dir=tmp)
        self.assertNotIn("first failure", out)

    @staticmethod
    def _capture_stdout(fn, *args, **kwargs) -> str:
        import io, contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            fn(*args, **kwargs)
        return buf.getvalue()


if __name__ == "__main__":
    unittest.main()
