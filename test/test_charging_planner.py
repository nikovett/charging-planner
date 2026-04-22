"""
Tests for charging_planner.py
==============================
Organised by feature area:

  1. Configuration — parsing, validation, error handling
  2. Time utilities — _hhmm_to_utc, _is_overnight, _resolve_window_utc
  3. Window filtering — filter_preferred_window (same-day and overnight)
  4. Slot selection — select_charging_windows, _best_continuous_window
  5. Spillover — _select_spillover
  6. Plan building
  7. Plan building — build_plan, PlanParams
  8. OCPP profile — build_ocpp_charging_profile, schema validation
  9. XML parsing — _parse_entsoe_xml with realistic XML
 10. End-to-end pipeline — cmd_plan with synthetic prices
"""

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
import unittest.mock as mock
import zipfile
from dataclasses import replace
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

sys.path.insert(0, "..")
from charging_planner import (
    Config,
    ConfigError,
    PlanParams,
    PricesNotYetAvailable,
    Slot,
    TzInfo,
    _best_continuous_window,
    _group_continuous,
    _hhmm_to_utc,
    _is_overnight,
    _parse_entsoe_xml,
    _resolve_schedule_window,
    _resolve_window_utc,
    _select_spillover,
    _gha_fmt_hours,
    _gha_summary_header,
    _gha_summary_profile,
    _window_bar,
    build_ocpp_charging_profile,
    build_plan,
    filter_preferred_window,
    merge_continuous_slots,
    parse_configs,
    print_plan_summary,
    select_charging_windows,
    validate_plan_config,
    write_gha_summary,
)

UTC      = timezone.utc
FI_TZ    = ZoneInfo("Europe/Helsinki")   # EET = UTC+2, EEST = UTC+3
REF_DATE = date(2026, 3, 15)             # Sunday, EET (UTC+2)


# ===========================================================================
# Helpers
# ===========================================================================

def make_slot(start_utc: datetime, duration_minutes: int = 15,
              price_cents: float = 3.0, slot: int = 0) -> Slot:
    return Slot(
        start=start_utc,
        end=start_utc + timedelta(minutes=duration_minutes),
        duration_minutes=duration_minutes,
        price_eur_kwh=price_cents / 100.0,
        slot=slot,
    )


def slots_from(base_utc: datetime, count: int, duration: int = 15,
               price_cents: float = 3.0) -> list[Slot]:
    """Build a contiguous list of slots starting at base_utc."""
    return [
        make_slot(base_utc + timedelta(minutes=duration * i), duration, price_cents, i)
        for i in range(count)
    ]


def make_config(**overrides) -> Config:
    """Minimal valid Config for testing selection functions."""
    defaults = dict(
        api_key="test", area="FI", name="test",
        required_minutes=120,
        continuous_only=False,
        min_slot_minutes=30,
        max_price_eur=None,
        preferred_window_start="00:00",
        preferred_window_end="06:00",
        timezone_str="Europe/Helsinki",
    )
    defaults.update(overrides)
    return Config(**defaults)


def make_plan_params(slots: list[Slot], selected: list[Slot],
                     windows=None, **overrides) -> PlanParams:
    if windows is None:
        windows = merge_continuous_slots(selected)
    defaults = dict(
        target_date=REF_DATE,
        area="FI",
        price_source="SYNTHETIC",
        display_prices=slots,
        future_prices=slots,
        selected=selected,
        windows=windows,
        required_minutes=120,
        tz=FI_TZ,
        timezone_name="Europe/Helsinki",
        preferred_window_start="00:00",
        preferred_window_end="06:00",
    )
    defaults.update(overrides)
    return PlanParams(**defaults)


# ===========================================================================
# 1. Configuration
# ===========================================================================

class TestConfigValidation(unittest.TestCase):

    BASE = {
        "entsoe": {"api_key": "abc", "area": "FI"},
        "charging": {
            "required_hours": 2,
            "preferred_window_start": "00:00",
            "preferred_window_end": "06:00",
        },
    }

    def _cfg(self, **overrides):
        import copy
        cfg = copy.deepcopy(self.BASE)
        cfg["charging"].update(overrides)
        # validate_plan_config expects single dict (called internally by parse_configs)
        return cfg

    def test_valid_config_no_raise(self):
        validate_plan_config(self.BASE)

    def test_missing_api_key_raises(self):
        cfg = {"entsoe": {"area": "FI"}, "charging": self.BASE["charging"]}
        with self.assertRaises(ConfigError):
            validate_plan_config(cfg)

    def test_missing_area_raises(self):
        cfg = {"entsoe": {"api_key": "x"}, "charging": self.BASE["charging"]}
        with self.assertRaises(ConfigError):
            validate_plan_config(cfg)

    def test_negative_required_hours_raises(self):
        with self.assertRaises(ConfigError):
            validate_plan_config(self._cfg(required_hours=-1))

    def test_zero_required_hours_raises(self):
        with self.assertRaises(ConfigError):
            validate_plan_config(self._cfg(required_hours=0))

    def test_bad_min_slot_not_divisible_by_15_raises(self):
        with self.assertRaises(ConfigError):
            validate_plan_config(self._cfg(min_slot_minutes=20))

    def test_bad_preferred_window_raises(self):
        with self.assertRaises(ConfigError):
            validate_plan_config(self._cfg(preferred_window_start="25:00"))

    def test_equal_window_times_raises(self):
        with self.assertRaises(ConfigError):
            validate_plan_config(self._cfg(
                preferred_window_start="06:00",
                preferred_window_end="06:00",
            ))

    def test_overnight_window_is_valid(self):
        # 22:00–06:30 is a valid overnight window
        validate_plan_config(self._cfg(
            preferred_window_start="22:00",
            preferred_window_end="06:30",
        ))

    def test_negative_price_ceiling_raises(self):
        with self.assertRaises(ConfigError):
            validate_plan_config(self._cfg(max_price_cents_kwh=-1))

    def test_null_price_ceiling_valid(self):
        validate_plan_config(self._cfg(max_price_cents_kwh=None))


class TestAvgPriceCeiling(unittest.TestCase):
    """Tests for max_price_cents_kwh: avg dynamic ceiling."""

    # validate_plan_config expects charging as a single dict (not a list)
    BASE_VALIDATE = {
        "entsoe": {"api_key": "test", "area": "FI"},
        "charging": {
            "required_hours": 1,
            "preferred_window_start": "22:00",
            "preferred_window_end": "06:30",
        },
    }

    # parse_configs expects charging as a list
    BASE_PARSE = {
        "entsoe": {"api_key": "test", "area": "FI", "timezone": "Europe/Helsinki"},
        "charging": [{
            "name": "topup",
            "required_hours": 1,
            "preferred_window_start": "22:00",
            "preferred_window_end": "06:30",
        }],
    }

    def _validate_cfg(self, ceil):
        import copy
        cfg = copy.deepcopy(self.BASE_VALIDATE)
        cfg["charging"]["max_price_cents_kwh"] = ceil
        return cfg

    def _parse_cfg(self, ceil):
        import copy
        cfg = copy.deepcopy(self.BASE_PARSE)
        cfg["charging"][0]["max_price_cents_kwh"] = ceil
        return cfg

    def test_avg_accepted_in_validation(self):
        """'avg' is a valid value for max_price_cents_kwh."""
        validate_plan_config(self._validate_cfg("avg"))  # should not raise

    def test_avg_case_insensitive(self):
        """'AVG' and 'Avg' are also valid."""
        for val in ("AVG", "Avg"):
            validate_plan_config(self._validate_cfg(val))  # should not raise

    def test_numeric_still_valid(self):
        """Numeric price ceiling still accepted."""
        validate_plan_config(self._validate_cfg(5.0))  # should not raise

    def test_invalid_string_rejected(self):
        """Arbitrary strings are rejected."""
        with self.assertRaises(ConfigError):
            validate_plan_config(self._validate_cfg("max"))

    def test_avg_sets_max_price_is_avg_flag(self):
        """Parsing 'avg' sets max_price_is_avg=True and max_price_eur=None."""
        configs = parse_configs(self._parse_cfg("avg"))
        self.assertTrue(configs[0].max_price_is_avg)
        self.assertIsNone(configs[0].max_price_eur)

    def test_numeric_ceiling_leaves_flag_false(self):
        """Numeric ceiling leaves max_price_is_avg=False."""
        configs = parse_configs(self._parse_cfg(5.0))
        self.assertFalse(configs[0].max_price_is_avg)
        self.assertAlmostEqual(configs[0].max_price_eur, 0.05)

    def test_avg_resolves_to_market_average(self):
        """When avg ceiling is set, charging slots are at or below market average."""
        import charging_planner as cp
        import tempfile

        _FROZEN_NOW = datetime(2026, 3, 14, 14, 30, tzinfo=UTC)

        class _FrozenDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                return _FROZEN_NOW if tz is None else _FROZEN_NOW.astimezone(tz)

        # 50 cheap slots at 0.01 EUR, 50 expensive at 0.09 EUR → avg = 0.05 EUR = 5 c€/kWh
        base = datetime(2026, 3, 14, 20, 0, tzinfo=UTC)
        slots = []
        for i in range(100):
            t = base + timedelta(minutes=15 * i)
            price = 0.01 if i < 50 else 0.09
            slots.append(Slot(start=t, end=t+timedelta(minutes=15),
                              duration_minutes=15, price_eur_kwh=price, slot=i))

        cfg = self._parse_cfg("avg")

        with tempfile.TemporaryDirectory() as tmpdir:
            with mock.patch("charging_planner.datetime", _FrozenDatetime):
                with mock.patch("charging_planner.fetch_entsoe_prices", return_value=slots):
                    with mock.patch("charging_planner.fetch_forecast_display_slots", return_value=[]):
                        plans = cp.cmd_plan(cfg, output_dir=tmpdir)

        charging = [s for s in plans[0]["price_slots"] if s["charging"]]
        for s in charging:
            self.assertLessEqual(s["price_cents_kwh"], 5.0 + 0.001,
                f"Slot {s['start_utc']} price {s['price_cents_kwh']} exceeds avg ceiling")


class TestParseConfigs(unittest.TestCase):

    BASE_RAW = {
        "entsoe": {"api_key": "abc", "area": "FI", "timezone": "Europe/Helsinki"},
        "charging": [{
            "name": "test",
            "required_hours": 2,
            "preferred_window_start": "00:00",
            "preferred_window_end": "06:00",
        }],
    }

    def test_single_profile_returns_list_of_one(self):
        configs = parse_configs(self.BASE_RAW)
        self.assertEqual(len(configs), 1)
        self.assertIsInstance(configs[0], Config)

    def test_required_hours_key_accessible(self):
        configs = parse_configs(self.BASE_RAW)
        self.assertEqual(configs[0].name, "test")

    def test_required_hours_converted_to_minutes(self):
        configs = parse_configs(self.BASE_RAW)
        self.assertEqual(configs[0].required_minutes, 120)

    def test_max_price_converted_to_eur(self):
        import copy
        raw = copy.deepcopy(self.BASE_RAW)
        raw["charging"][0]["max_price_cents_kwh"] = 5.0
        configs = parse_configs(raw)
        self.assertAlmostEqual(configs[0].max_price_eur, 0.05)

    def test_multiple_profiles(self):
        raw = {
            "entsoe": {"api_key": "abc", "area": "FI", "timezone": "Europe/Helsinki"},
            "charging": [
                {"name": "a", "required_hours": 1, "preferred_window_start": "00:00",
                 "preferred_window_end": "03:00"},
                {"name": "b", "required_hours": 3, "preferred_window_start": "00:00",
                 "preferred_window_end": "07:00"},
            ],
        }
        configs = parse_configs(raw)
        self.assertEqual(len(configs), 2)
        self.assertEqual(configs[0].name, "a")
        self.assertEqual(configs[1].name, "b")

    def test_bad_timezone_raises_config_error(self):
        import copy
        raw = copy.deepcopy(self.BASE_RAW)
        raw["entsoe"]["timezone"] = "Not/ATimezone"
        with self.assertRaises(ConfigError):
            parse_configs(raw)

    def test_schedule_parsed_into_config(self):
        import copy
        raw = copy.deepcopy(self.BASE_RAW)
        raw["charging"][0]["schedule"] = [
            {"days": ["saturday", "sunday"],
             "preferred_window_start": "00:00",
             "preferred_window_end": "23:45"},
        ]
        configs = parse_configs(raw)
        self.assertEqual(len(configs[0].schedule), 1)
        self.assertEqual(configs[0].schedule[0]["days"], ["saturday", "sunday"])

    def test_schedule_any_window_valid(self):
        import copy
        raw = copy.deepcopy(self.BASE_RAW)
        raw["charging"][0]["schedule"] = [
            {"days": ["saturday", "sunday"],
             "preferred_window_start": "any", "preferred_window_end": "any"},
        ]
        configs = parse_configs(raw)
        self.assertEqual(configs[0].schedule[0].get("preferred_window_start"), "any")

    def test_top_level_any_window_valid(self):
        import copy
        raw = copy.deepcopy(self.BASE_RAW)
        raw["charging"][0]["preferred_window_start"] = "any"
        raw["charging"][0]["preferred_window_end"] = "any"
        configs = parse_configs(raw)
        self.assertTrue(configs[0].preferred_window_any)

    def test_empty_schedule_is_valid(self):
        import copy
        raw = copy.deepcopy(self.BASE_RAW)
        raw["charging"][0]["schedule"] = []
        configs = parse_configs(raw)
        self.assertEqual(configs[0].schedule, [])

    def test_schedule_invalid_day_name_raises(self):
        with self.assertRaises(ConfigError):
            validate_plan_config({
                "entsoe": {"api_key": "x", "area": "FI"},
                "charging": {
                    "required_hours": 2,
                    "preferred_window_start": "22:00",
                    "preferred_window_end": "06:00",
                    "schedule": [
                        {"days": ["funday"],
                         "preferred_window_start": "00:00",
                         "preferred_window_end": "23:45"},
                    ],
                },
            })

    def test_schedule_duplicate_day_raises(self):
        with self.assertRaises(ConfigError):
            validate_plan_config({
                "entsoe": {"api_key": "x", "area": "FI"},
                "charging": {
                    "required_hours": 2,
                    "preferred_window_start": "22:00",
                    "preferred_window_end": "06:00",
                    "schedule": [
                        {"days": ["monday"],
                         "preferred_window_start": "00:00",
                         "preferred_window_end": "06:00"},
                        {"days": ["monday"],
                         "preferred_window_start": "22:00",
                         "preferred_window_end": "06:00"},
                    ],
                },
            })


# ===========================================================================
# Schedule window resolution
# ===========================================================================

class TestResolveScheduleWindow(unittest.TestCase):

    def _make_cfg(self, schedule):
        from dataclasses import replace
        import copy
        raw = {
            "entsoe": {"api_key": "abc", "area": "FI", "timezone": "Europe/Helsinki"},
            "charging": [{
                "name": "test",
                "required_hours": 2,
                "preferred_window_start": "22:00",
                "preferred_window_end": "06:30",
                "schedule": schedule,
            }],
        }
        return parse_configs(raw)[0]

    def test_weekday_matches_schedule_entry(self):
        cfg = self._make_cfg([
            {"days": ["monday", "tuesday", "wednesday", "thursday", "friday"],
             "preferred_window_start": "22:00", "preferred_window_end": "06:30"},
            {"days": ["saturday", "sunday"],
             "preferred_window_start": "00:00", "preferred_window_end": "23:45"},
        ])
        # 2026-03-21 is a Saturday
        start, end, _ = _resolve_schedule_window(cfg, date(2026, 3, 21))
        self.assertEqual(start, "00:00")
        self.assertEqual(end, "23:45")

    def test_weekday_falls_back_to_default(self):
        cfg = self._make_cfg([
            {"days": ["saturday", "sunday"],
             "preferred_window_start": "00:00", "preferred_window_end": "23:45"},
        ])
        # 2026-03-16 is a Monday — no matching entry
        start, end, _ = _resolve_schedule_window(cfg, date(2026, 3, 16))
        self.assertEqual(start, "22:00")
        self.assertEqual(end, "06:30")

    def test_empty_schedule_returns_defaults(self):
        cfg = self._make_cfg([])
        start, end, _ = _resolve_schedule_window(cfg, date(2026, 3, 21))
        self.assertEqual(start, "22:00")
        self.assertEqual(end, "06:30")

    def test_first_matching_entry_wins(self):
        cfg = self._make_cfg([
            {"days": ["saturday"],
             "preferred_window_start": "08:00", "preferred_window_end": "20:00"},
            {"days": ["sunday"],
             "preferred_window_start": "00:00", "preferred_window_end": "23:45"},
        ])
        start, end, _ = _resolve_schedule_window(cfg, date(2026, 3, 21))
        self.assertEqual(start, "08:00")

    def test_any_window_returns_sentinel(self):
        cfg = self._make_cfg([
            {"days": ["saturday", "sunday"],
             "preferred_window_start": "any", "preferred_window_end": "any"},
        ])
        start, end, _ = _resolve_schedule_window(cfg, date(2026, 3, 21))
        self.assertEqual(start, "any")
        self.assertEqual(end, "any")




class TestHhmmToUtc(unittest.TestCase):

    def test_utc_timezone(self):
        result = _hhmm_to_utc("12:00", REF_DATE, UTC)
        self.assertEqual(result, datetime(2026, 3, 15, 12, 0, tzinfo=UTC))

    def test_positive_offset(self):
        # Helsinki EET = UTC+2; 00:00 local = 22:00 UTC prev day
        result = _hhmm_to_utc("00:00", REF_DATE, FI_TZ)
        self.assertEqual(result, datetime(2026, 3, 14, 22, 0, tzinfo=UTC))

    def test_with_minutes(self):
        result = _hhmm_to_utc("06:30", REF_DATE, FI_TZ)
        self.assertEqual(result, datetime(2026, 3, 15, 4, 30, tzinfo=UTC))

    def test_dst_transition(self):
        # 2026-03-29: Helsinki clocks forward at 03:00 EET → 04:00 EEST
        # Before transition: 01:00 Helsinki = 23:00 UTC
        # After transition: 04:00 Helsinki = 01:00 UTC
        dst_date = date(2026, 3, 29)
        result = _hhmm_to_utc("04:00", dst_date, FI_TZ)
        self.assertEqual(result, datetime(2026, 3, 29, 1, 0, tzinfo=UTC))


class TestIsOvernight(unittest.TestCase):

    def test_same_day_not_overnight(self):
        self.assertFalse(_is_overnight("00:00", "06:30"))
        self.assertFalse(_is_overnight("08:00", "22:00"))

    def test_overnight_detected(self):
        self.assertTrue(_is_overnight("22:00", "06:30"))
        self.assertTrue(_is_overnight("23:00", "01:00"))


class TestResolveWindowUtc(unittest.TestCase):

    def test_same_day_window_end_after_start(self):
        start, end = _resolve_window_utc("00:00", "06:00", FI_TZ,
                                          _anchor_date=REF_DATE)
        self.assertGreater(end, start)

    def test_same_day_values(self):
        # REF_DATE 2026-03-15, EET = UTC+2
        # 00:00 local = 2026-03-14T22:00Z, 06:00 local = 2026-03-15T04:00Z
        start, end = _resolve_window_utc("00:00", "06:00", FI_TZ,
                                          _anchor_date=REF_DATE)
        self.assertEqual(start, datetime(2026, 3, 14, 22, 0, tzinfo=UTC))
        self.assertEqual(end,   datetime(2026, 3, 15, 4,  0, tzinfo=UTC))

    def test_overnight_end_on_next_day(self):
        # 22:00–06:30 overnight: end must be after start in UTC
        start, end = _resolve_window_utc("22:00", "06:30", FI_TZ,
                                          _anchor_date=REF_DATE)
        self.assertGreater(end, start)

    def test_overnight_end_utc_values(self):
        # REF_DATE 2026-03-15 is EET (UTC+2)
        # 22:00 Helsinki = 20:00 UTC; 06:30 next day Helsinki = 04:30 UTC
        start, end = _resolve_window_utc("22:00", "06:30", FI_TZ,
                                          _anchor_date=REF_DATE)
        self.assertEqual(start, datetime(2026, 3, 15, 20, 0,  tzinfo=UTC))
        self.assertEqual(end,   datetime(2026, 3, 16, 4,  30, tzinfo=UTC))


# ===========================================================================
# 3. Window filtering
# ===========================================================================

class TestFilterPreferredWindow(unittest.TestCase):

    def _run(self, slots, start_hhmm, end_hhmm, anchor=REF_DATE):
        ws, we = _resolve_window_utc(start_hhmm, end_hhmm, FI_TZ,
                                      _anchor_date=anchor)
        return filter_preferred_window(slots, ws, we, start_hhmm, end_hhmm)

    def test_slots_inside_same_day_window(self):
        # 00:00–06:00 Helsinki; slots at 01:00, 03:00, 08:00 local
        base = datetime(2026, 3, 14, 22, 0, tzinfo=UTC)  # 00:00 Helsinki
        slots = [
            make_slot(base + timedelta(hours=1)),   # 01:00 — inside
            make_slot(base + timedelta(hours=3)),   # 03:00 — inside
            make_slot(base + timedelta(hours=8)),   # 08:00 — outside
        ]
        inside, outside = self._run(slots, "00:00", "06:00")
        self.assertEqual(len(inside),  2)
        self.assertEqual(len(outside), 1)

    def test_overnight_evening_slots_inside(self):
        # 22:00–06:30 window; slot at 22:30 Helsinki (tonight) should be inside
        base = datetime(2026, 3, 15, 20, 30, tzinfo=UTC)  # 22:30 Helsinki
        slots = [make_slot(base)]
        inside, _ = self._run(slots, "22:00", "06:30")
        self.assertEqual(len(inside), 1)

    def test_overnight_morning_slots_inside(self):
        # 06:00 Helsinki next morning = 03:00 UTC — inside 22:00–06:30 window
        base = datetime(2026, 3, 16, 3, 0, tzinfo=UTC)  # 06:00 Helsinki
        slots = [make_slot(base)]
        inside, _ = self._run(slots, "22:00", "06:30")
        self.assertEqual(len(inside), 1)

    def test_overnight_midday_slots_outside(self):
        # 14:00 Helsinki = 12:00 UTC — outside 22:00–06:30 window
        base = datetime(2026, 3, 15, 12, 0, tzinfo=UTC)
        slots = [make_slot(base)]
        _, outside = self._run(slots, "22:00", "06:30")
        self.assertEqual(len(outside), 1)

    def test_empty_input(self):
        inside, outside = self._run([], "00:00", "06:00")
        self.assertEqual(inside, [])
        self.assertEqual(outside, [])


# ===========================================================================
# 4. Slot selection
# ===========================================================================

class TestSelectChargingWindows(unittest.TestCase):

    def _slots(self, count=24, price_cents=3.0):
        base = datetime(2026, 3, 14, 22, 0, tzinfo=UTC)
        return slots_from(base, count, price_cents=price_cents)

    def test_selects_required_minutes(self):
        selected = select_charging_windows(self._slots(), required_minutes=60)
        total = sum(s.duration_minutes for s in selected)
        self.assertEqual(total, 60)

    def test_selects_cheapest_slots(self):
        slots = self._slots(24, price_cents=5.0)
        # Make slots 4–7 cheaper
        for i in [4, 5, 6, 7]:
            slots[i] = replace(slots[i], price_eur_kwh=0.01)
        selected = select_charging_windows(slots, required_minutes=60)
        cheap_starts = {slots[i].start for i in [4, 5, 6, 7]}
        self.assertTrue(all(s.start in cheap_starts for s in selected))

    def test_continuous_only_returns_one_block(self):
        slots = self._slots(24)
        selected = select_charging_windows(slots, required_minutes=60,
                                           continuous_only=True)
        groups = _group_continuous(sorted(selected, key=lambda s: s.start))
        self.assertEqual(len(groups), 1)

    def test_min_slot_minutes_enforced(self):
        slots = self._slots(24)
        selected = select_charging_windows(slots, required_minutes=120,
                                           min_slot_minutes=30)
        groups = _group_continuous(sorted(selected, key=lambda s: s.start))
        for group in groups:
            duration = sum(s.duration_minutes for s in group)
            self.assertGreaterEqual(duration, 30)

    def test_max_price_ceiling_respected(self):
        slots = self._slots(24, price_cents=5.0)
        # Only 4 slots are cheap enough
        for i in range(4):
            slots[i] = replace(slots[i], price_eur_kwh=0.01)
        selected = select_charging_windows(slots, required_minutes=60,
                                           max_price=0.02)
        self.assertLessEqual(len(selected), 4)
        for s in selected:
            self.assertLessEqual(s.price_eur_kwh, 0.02)

    def test_empty_prices_returns_empty(self):
        self.assertEqual(select_charging_windows([], required_minutes=60), [])

    def test_latest_slot_preferred_on_equal_price(self):
        # All slots same price — should prefer the latest ones
        slots = self._slots(8)
        selected = select_charging_windows(slots, required_minutes=15)
        self.assertEqual(selected[0].start, slots[-1].start)


class TestBestContinuousWindow(unittest.TestCase):

    def _slots(self, count=8):
        base = datetime(2026, 3, 14, 22, 0, tzinfo=UTC)
        return slots_from(base, count)

    def test_returns_cheapest_continuous_window(self):
        slots = self._slots(8)
        # Make slots 2–5 cheaper
        for i in [2, 3, 4, 5]:
            slots[i] = replace(slots[i], price_eur_kwh=0.01)
        result = _best_continuous_window(slots, slots, n_slots=4)
        self.assertEqual(len(result), 4)
        self.assertEqual(result[0].start, slots[2].start)

    def test_fallback_stays_within_candidates(self):
        slots = self._slots(8)
        # Only slots 0–1 and 6–7 are candidates — no 4-slot window fits
        candidates = [s for i, s in enumerate(slots) if i in (0, 1, 6, 7)]
        result = _best_continuous_window(candidates, slots, n_slots=4)
        # Returns longest contiguous block within candidates, not outside
        candidate_starts = {s.start for s in candidates}
        for s in result:
            self.assertIn(s.start, candidate_starts)

    def test_respects_temporal_continuity(self):
        # Build slots with a time gap in the middle
        base = datetime(2026, 3, 14, 22, 0, tzinfo=UTC)
        evening = slots_from(base, 4)
        morning = slots_from(base + timedelta(hours=6), 4)  # gap of 5h
        all_slots = evening + morning
        result = _best_continuous_window(all_slots, all_slots, n_slots=4)
        # Result must be temporally contiguous — no gap
        for i in range(len(result) - 1):
            self.assertEqual(result[i].end, result[i + 1].start)


# ===========================================================================
# 5. Spillover
# ===========================================================================

class TestSelectSpillover(unittest.TestCase):

    def _window_utc(self, start_hhmm, end_hhmm):
        return _resolve_window_utc(start_hhmm, end_hhmm, FI_TZ,
                                    _anchor_date=REF_DATE)

    def test_no_spill_when_satisfied(self):
        base = datetime(2026, 3, 14, 22, 0, tzinfo=UTC)
        selected = slots_from(base, 8)  # 2h
        ws, we = self._window_utc("00:00", "06:00")
        result = _select_spillover(
            outside=[], selected=selected,
            continuous_only=False, win_end_utc=we, win_end_local="06:00",
            required_minutes=120, remaining=0,
            max_price_eur=None, min_slot_minutes=30, all_prices=selected,
        )
        self.assertEqual(result, [])

    def test_noncontinuous_spill_stays_before_window_end(self):
        ws, we = self._window_utc("00:00", "04:00")
        # inside: 2h; need 4h total → 2h spill from outside (before window)
        before_base = datetime(2026, 3, 14, 20, 0, tzinfo=UTC)
        outside = slots_from(before_base, 8, price_cents=2.0)
        inside  = slots_from(datetime(2026, 3, 14, 22, 0, tzinfo=UTC), 8)
        result = _select_spillover(
            outside=outside, selected=inside,
            continuous_only=False, win_end_utc=we, win_end_local="04:00",
            required_minutes=240, remaining=120,
            max_price_eur=None, min_slot_minutes=30, all_prices=outside + inside,
        )
        for s in result:
            self.assertLessEqual(s.end, we)

    def test_continuous_spill_extends_leftward(self):
        ws, we = self._window_utc("02:00", "05:00")
        # 3h window, need 5h — must extend 2h leftward
        inside_base  = datetime(2026, 3, 15, 0, 0, tzinfo=UTC)  # 02:00 Helsinki
        outside_base = datetime(2026, 3, 14, 22, 0, tzinfo=UTC)  # before window
        inside  = slots_from(inside_base,  12)
        outside = slots_from(outside_base, 8)
        result = _select_spillover(
            outside=outside, selected=inside,
            continuous_only=True, win_end_utc=we, win_end_local="05:00",
            required_minutes=300, remaining=120,
            max_price_eur=None, min_slot_minutes=30, all_prices=outside + inside,
        )
        # Spill slots must be adjacent to the selected block (extend leftward)
        all_selected = sorted(inside + result, key=lambda s: s.start)
        for i in range(len(all_selected) - 1):
            self.assertEqual(all_selected[i].end, all_selected[i + 1].start)

    def test_spill_remaining_less_than_min_slot(self):
        # Regression: remaining=15 with min_slot_minutes=30 previously returned
        # nothing because _select_with_min_block couldn't form a valid 30-min block
        # from a single 15-min spillover slot. Spillover should ignore min_slot_minutes.
        ws, we = self._window_utc("00:00", "04:00")
        before_base = datetime(2026, 3, 14, 20, 0, tzinfo=UTC)
        outside = slots_from(before_base, 4, price_cents=2.0)  # 4 × 15-min slots before window
        inside  = slots_from(datetime(2026, 3, 14, 22, 0, tzinfo=UTC), 7)  # 7 slots = 105 min
        result = _select_spillover(
            outside=outside, selected=inside,
            continuous_only=False, win_end_utc=we, win_end_local="04:00",
            required_minutes=120, remaining=15,
            max_price_eur=None, min_slot_minutes=30, all_prices=outside + inside,
        )
        self.assertEqual(len(result), 1, "Should fill the 15-min deficit with one spillover slot")
        total = sum(s.duration_minutes for s in result)
        self.assertEqual(total, 15)

    def test_no_spill_after_window_end(self):
        ws, we = self._window_utc("00:00", "04:00")
        after_base = datetime(2026, 3, 15, 2, 30, tzinfo=UTC)  # 04:30 Helsinki — past window end
        outside = slots_from(after_base, 8)
        inside  = slots_from(datetime(2026, 3, 14, 22, 0, tzinfo=UTC), 4)
        result = _select_spillover(
            outside=outside, selected=inside,
            continuous_only=False, win_end_utc=we, win_end_local="04:00",
            required_minutes=120, remaining=60,
            max_price_eur=None, min_slot_minutes=30, all_prices=outside + inside,
        )
        for s in result:
            self.assertLessEqual(s.end, we)


# ===========================================================================
# 7. Plan building
# ===========================================================================

class TestBuildPlan(unittest.TestCase):

    def _make(self, n_slots=8, price_cents=3.0, **overrides):
        base     = datetime(2026, 3, 14, 22, 0, tzinfo=UTC)
        slots    = slots_from(base, n_slots, price_cents=price_cents)
        selected = slots[:4]
        windows  = merge_continuous_slots(selected)
        p        = make_plan_params(slots, selected, windows, **overrides)
        return build_plan(p)

    def test_plan_has_required_keys(self):
        plan = self._make()
        for key in ("version", "date", "area", "windows",
                    "window_starts_utc", "window_ends_utc",
                    "required_minutes", "total_minutes",
                    "ocpp_charging_profile"):
            self.assertIn(key, plan)

    def test_total_minutes_correct(self):
        plan = self._make(n_slots=8)
        self.assertEqual(plan["total_minutes"], 60)  # 4 × 15min

    def test_window_utc_times_are_iso_strings(self):
        plan = self._make()
        for ts in plan["window_starts_utc"] + plan["window_ends_utc"]:
            datetime.fromisoformat(ts)  # should not raise

    def test_price_stats_present(self):
        plan = self._make()
        ps = plan["price_stats"]
        self.assertIn("min_cents_kwh", ps)
        self.assertIn("avg_cents_kwh", ps)
        self.assertIn("max_cents_kwh", ps)


# ===========================================================================
# 8. OCPP profile
# ===========================================================================

SCHEMA_16_PATH  = "test/ocpp16/OCPP_1.6_documentation/schemas/json/SetChargingProfile.json"
SCHEMA_201_PATH = "test/ocpp201/OCPP-2.0.1_all_files/OCPP-2.0.1_part3_JSON_schemas.zip"
SCHEMA_21_PATH  = "test/ocpp21/OCPP-2.1_all_files/OCPP-2.1_part3_JSON_schemas.zip"


def _load_schema_201():
    zf = zipfile.ZipFile(SCHEMA_201_PATH)
    return json.loads(zf.read(
        "OCPP-2.0.1_part3_JSON_schemas/SetChargingProfileRequest.json"))


def _load_schema_21():
    zf = zipfile.ZipFile(SCHEMA_21_PATH)
    return json.loads(zf.read(
        "OCPP-2.1_part3_JSON_schemas/SetChargingProfileRequest.json"))


def _load_schema_16():
    return json.load(open(SCHEMA_16_PATH))


class TestOcppChargingProfile(unittest.TestCase):

    PLAN_SINGLE = {
        "window_starts_utc": ["2026-03-14T22:00:00+00:00"],
        "window_ends_utc":   ["2026-03-15T04:00:00+00:00"],
    }
    PLAN_TWO_WINDOWS = {
        "window_starts_utc": [
            "2026-03-14T22:00:00+00:00",
            "2026-03-15T02:00:00+00:00",
        ],
        "window_ends_utc": [
            "2026-03-15T00:00:00+00:00",
            "2026-03-15T04:00:00+00:00",
        ],
    }

    def _validate_against(self, profile, schema_props, required_fields,
                          allowed_fields):
        missing = [f for f in required_fields if f not in profile]
        extra   = [f for f in profile if f not in allowed_fields]
        self.assertEqual(missing, [], f"Missing fields: {missing}")
        self.assertEqual(extra,   [], f"Extra fields not in schema: {extra}")

    def test_empty_plan_returns_empty_dict(self):
        self.assertEqual(build_ocpp_charging_profile({}), {})

    def test_single_window_schedule(self):
        profile = build_ocpp_charging_profile(self.PLAN_SINGLE)
        periods = profile["chargingSchedule"]["chargingSchedulePeriod"]
        self.assertEqual(len(periods), 1)
        self.assertEqual(periods[0]["startPeriod"], 0)
        self.assertEqual(periods[0]["limit"], 11000.0)

    def test_two_windows_gap_is_zero(self):
        profile = build_ocpp_charging_profile(self.PLAN_TWO_WINDOWS)
        periods = profile["chargingSchedule"]["chargingSchedulePeriod"]
        # Should be: charge, gap=0, charge
        limits = [p["limit"] for p in periods]
        self.assertEqual(limits[0], 11000.0)
        self.assertEqual(limits[1], 0.0)
        self.assertEqual(limits[2], 11000.0)

    def test_periods_ordered_by_start_period(self):
        profile = build_ocpp_charging_profile(self.PLAN_TWO_WINDOWS)
        periods = profile["chargingSchedule"]["chargingSchedulePeriod"]
        starts = [p["startPeriod"] for p in periods]
        self.assertEqual(starts, sorted(starts))

    def test_duration_matches_window_span(self):
        profile = build_ocpp_charging_profile(self.PLAN_SINGLE)
        # 22:00 to 04:00 = 6 hours = 21600 seconds
        self.assertEqual(profile["chargingSchedule"]["duration"], 21600)

    def test_valid_from_to_match_window_bounds(self):
        profile = build_ocpp_charging_profile(self.PLAN_SINGLE)
        self.assertEqual(profile["validFrom"],
                         "2026-03-14T22:00:00+00:00")
        self.assertEqual(profile["validTo"],
                         "2026-03-15T04:00:00+00:00")

    def test_custom_max_rate(self):
        profile = build_ocpp_charging_profile(self.PLAN_SINGLE,
                                               max_charging_rate=7400.0)
        periods = profile["chargingSchedule"]["chargingSchedulePeriod"]
        self.assertEqual(periods[0]["limit"], 7400.0)

    # ── Schema validation against real OCPP specs ────────────────────────────

    def test_ocpp16_schema_valid(self):
        try:
            schema = _load_schema_16()
        except FileNotFoundError:
            self.skipTest("OCPP 1.6 schema not available")
        profile  = build_ocpp_charging_profile(self.PLAN_SINGLE,
                                                ocpp_version="1.6")
        cp_props = schema["properties"]["csChargingProfiles"]
        required = cp_props["required"]
        allowed  = set(cp_props["properties"].keys())
        self._validate_against(profile, cp_props, required, allowed)

    def test_ocpp201_schema_valid(self):
        try:
            schema = _load_schema_201()
        except FileNotFoundError:
            self.skipTest("OCPP 2.0.1 schema not available")
        profile  = build_ocpp_charging_profile(self.PLAN_SINGLE,
                                                ocpp_version="2.0.1")
        cp_props = schema["definitions"]["ChargingProfileType"]
        required = cp_props.get("required", [])
        allowed  = set(cp_props["properties"].keys())
        self._validate_against(profile, cp_props, required, allowed)

    def test_ocpp21_schema_valid(self):
        try:
            schema = _load_schema_21()
        except FileNotFoundError:
            self.skipTest("OCPP 2.1 schema not available")
        profile  = build_ocpp_charging_profile(self.PLAN_SINGLE,
                                                ocpp_version="2.1")
        cp_props = schema["definitions"]["ChargingProfileType"]
        required = cp_props.get("required", [])
        allowed  = set(cp_props["properties"].keys())
        self._validate_against(profile, cp_props, required, allowed)

    def test_16_uses_charging_profile_id(self):
        profile = build_ocpp_charging_profile(self.PLAN_SINGLE,
                                               ocpp_version="1.6")
        self.assertIn("chargingProfileId", profile)
        self.assertNotIn("id", profile)

    def test_201_uses_id(self):
        profile = build_ocpp_charging_profile(self.PLAN_SINGLE,
                                               ocpp_version="2.0.1")
        self.assertIn("id", profile)
        self.assertNotIn("chargingProfileId", profile)

    def test_21_uses_id(self):
        profile = build_ocpp_charging_profile(self.PLAN_SINGLE,
                                               ocpp_version="2.1")
        self.assertIn("id", profile)
        self.assertNotIn("chargingProfileId", profile)


# ===========================================================================
# 9. XML parsing
# ===========================================================================

MINIMAL_XML = """\
<?xml version="1.0" encoding="UTF-8"?>
<Publication_MarketDocument xmlns="urn:iec62325.351:tc57wg16:451-3:publicationdocument:7:3">
  <TimeSeries>
    <Period>
      <timeInterval>
        <start>2026-03-14T23:00Z</start>
        <end>2026-03-15T23:00Z</end>
      </timeInterval>
      <resolution>PT15M</resolution>
      <Point><position>1</position><price.amount>3.00</price.amount></Point>
      <Point><position>5</position><price.amount>1.50</price.amount></Point>
      <Point><position>9</position><price.amount>3.50</price.amount></Point>
    </Period>
  </TimeSeries>
</Publication_MarketDocument>
"""

DUAL_SERIES_XML = """\
<?xml version="1.0" encoding="UTF-8"?>
<Publication_MarketDocument xmlns="urn:iec62325.351:tc57wg16:451-3:publicationdocument:7:3">
  <TimeSeries>
    <Period>
      <timeInterval>
        <start>2026-03-13T23:00Z</start>
        <end>2026-03-14T23:00Z</end>
      </timeInterval>
      <resolution>PT15M</resolution>
      <Point><position>1</position><price.amount>2.00</price.amount></Point>
    </Period>
  </TimeSeries>
  <TimeSeries>
    <Period>
      <timeInterval>
        <start>2026-03-14T23:00Z</start>
        <end>2026-03-15T23:00Z</end>
      </timeInterval>
      <resolution>PT15M</resolution>
      <Point><position>1</position><price.amount>4.00</price.amount></Point>
    </Period>
  </TimeSeries>
</Publication_MarketDocument>
"""

ERROR_XML = """\
<Acknowledgement_MarketDocument xmlns="urn:iec62325.351:tc57wg16:451-3:acknowledgementdocument:7:1">
  <Reason><code>999</code><text>Invalid security token</text></Reason>
</Acknowledgement_MarketDocument>
"""


class TestXmlParsing(unittest.TestCase):

    def test_parses_slots_correctly(self):
        slots = _parse_entsoe_xml(MINIMAL_XML, date(2026, 3, 15), "FI")
        self.assertGreater(len(slots), 0)

    def test_slots_are_sorted_by_start(self):
        slots = _parse_entsoe_xml(MINIMAL_XML, date(2026, 3, 15), "FI")
        starts = [s.start for s in slots]
        self.assertEqual(starts, sorted(starts))

    def test_forward_fill_between_explicit_points(self):
        slots = _parse_entsoe_xml(MINIMAL_XML, date(2026, 3, 15), "FI")
        # Position 1 = 3.00, position 2–4 carry forward 3.00
        first_four = slots[:4]
        for s in first_four:
            self.assertAlmostEqual(s.price_eur_kwh, 0.003)  # 3.00 EUR/MWh = 0.003 €/kWh

    def test_price_at_position_5_updated(self):
        slots = _parse_entsoe_xml(MINIMAL_XML, date(2026, 3, 15), "FI")
        # Position 5 = 1.50 EUR/MWh = 0.0015 €/kWh
        self.assertAlmostEqual(slots[4].price_eur_kwh, 0.0015)

    def test_slots_are_15min_duration(self):
        slots = _parse_entsoe_xml(MINIMAL_XML, date(2026, 3, 15), "FI")
        for s in slots:
            self.assertEqual(s.duration_minutes, 15)
            self.assertEqual(s.end - s.start, timedelta(minutes=15))

    def test_no_duplicate_starts(self):
        slots = _parse_entsoe_xml(DUAL_SERIES_XML, date(2026, 3, 15), "FI")
        starts = [s.start for s in slots]
        self.assertEqual(len(starts), len(set(starts)))

    def test_deduplication_keeps_both_days(self):
        slots = _parse_entsoe_xml(DUAL_SERIES_XML, date(2026, 3, 15), "FI")
        # Both TimeSeries cover different days — should have slots from both
        first_start = slots[0].start
        last_start  = slots[-1].start
        self.assertGreater((last_start - first_start).total_seconds(), 23 * 3600)

    def test_api_error_raises_value_error(self):
        with self.assertRaises(ValueError) as ctx:
            _parse_entsoe_xml(ERROR_XML, date(2026, 3, 15), "FI")
        self.assertIn("error", str(ctx.exception).lower())

    def test_invalid_xml_raises_value_error(self):
        with self.assertRaises(ValueError):
            _parse_entsoe_xml("not xml at all", date(2026, 3, 15), "FI")

    def test_ordinals_sequential(self):
        slots = _parse_entsoe_xml(MINIMAL_XML, date(2026, 3, 15), "FI")
        for i, s in enumerate(slots):
            self.assertEqual(s.slot, i)



class TestSelectWithMinBlock(unittest.TestCase):
    """Direct tests for _select_with_min_block and its pick_next helper."""

    def _slots(self, count=16, price_cents=3.0):
        base = datetime(2026, 3, 14, 22, 0, tzinfo=UTC)
        return slots_from(base, count, price_cents=price_cents)

    def test_no_blocks_shorter_than_min(self):
        slots = self._slots(16)
        selected = select_charging_windows(slots, required_minutes=120,
                                           min_slot_minutes=30)
        groups = _group_continuous(sorted(selected, key=lambda s: s.start))
        for group in groups:
            self.assertGreaterEqual(len(group) * 15, 30)

    def test_cheap_isolated_slot_replaced(self):
        # Make slot 4 very cheap but isolated — the slot before and after are expensive.
        # With min_slot_minutes=30 (2 slots), a single isolated cheap slot should be
        # disqualified and replaced with an adjacent pair.
        base = datetime(2026, 3, 14, 22, 0, tzinfo=UTC)
        slots = slots_from(base, 16, price_cents=5.0)
        slots[4] = replace(slots[4], price_eur_kwh=0.001)  # very cheap, isolated
        # Require 2 slots (30 min) with min_slot_minutes=30
        selected = select_charging_windows(slots, required_minutes=30,
                                           min_slot_minutes=30)
        groups = _group_continuous(sorted(selected, key=lambda s: s.start))
        self.assertEqual(len(groups), 1)
        self.assertGreaterEqual(len(groups[0]), 2)

    def test_total_minutes_correct_despite_disqualification(self):
        base = datetime(2026, 3, 14, 22, 0, tzinfo=UTC)
        slots = slots_from(base, 16, price_cents=5.0)
        # Make slots 0 and 8 cheap but each isolated
        slots[0] = replace(slots[0], price_eur_kwh=0.001)
        slots[8] = replace(slots[8], price_eur_kwh=0.001)
        selected = select_charging_windows(slots, required_minutes=60,
                                           min_slot_minutes=30)
        total = sum(s.duration_minutes for s in selected)
        self.assertEqual(total, 60)

    def test_all_same_price_latest_preferred(self):
        # All slots same price — latest slots should be selected (tiebreaker)
        slots = self._slots(16)
        selected = select_charging_windows(slots, required_minutes=30,
                                           min_slot_minutes=30)
        # Should pick the last 2 slots
        srt = sorted(selected, key=lambda s: s.start)
        self.assertEqual(srt[0].start, slots[-2].start)

    def test_min_slot_larger_than_required_still_works(self):
        # min_slot_minutes=60 but required=60 — should still find a 4-slot block
        slots = self._slots(16)
        selected = select_charging_windows(slots, required_minutes=60,
                                           min_slot_minutes=60)
        total = sum(s.duration_minutes for s in selected)
        self.assertEqual(total, 60)
        groups = _group_continuous(sorted(selected, key=lambda s: s.start))
        self.assertEqual(len(groups), 1)

    def test_real_prices_min_slot_respected(self):
        # Use real ENTSO-E data to exercise the path with realistic price variation
        slots = _parse_entsoe_xml(REAL_ENTSOE_XML, date(2026, 3, 14), "FI")
        anchor = date(2026, 3, 14)
        ws, we = _resolve_window_utc("00:00", "06:30", FI_TZ, _anchor_date=anchor)
        inside, _ = filter_preferred_window(slots, ws, we, "00:00", "06:30")
        selected = select_charging_windows(inside, required_minutes=120,
                                           min_slot_minutes=30)
        groups = _group_continuous(sorted(selected, key=lambda s: s.start))
        for group in groups:
            duration = sum(s.duration_minutes for s in group)
            self.assertGreaterEqual(duration, 30,
                f"Block of {duration} min is shorter than min_slot_minutes=30")

    def test_gap_between_blocks_respects_min_slot(self):
        # Two cheap clusters separated by a 15-min gap — with min_slot_minutes=30
        # the algorithm must not select both clusters since the gap would be < 30 min.
        # It should instead pick the cheaper cluster only (or extend one of them).
        base = datetime(2026, 3, 14, 22, 0, tzinfo=UTC)
        # cheap block A: 30 min
        block_a = slots_from(base, 2, price_cents=1.0)
        # 15-min gap (expensive)
        gap     = slots_from(base + timedelta(minutes=30), 1, price_cents=9.0)
        # cheap block B: 30 min
        block_b = slots_from(base + timedelta(minutes=45), 2, price_cents=1.0)
        # padding
        rest    = slots_from(base + timedelta(minutes=75), 8, price_cents=5.0)
        all_slots = block_a + gap + block_b + rest

        selected = select_charging_windows(
            all_slots, required_minutes=60, min_slot_minutes=30, min_gap_minutes=30
        )
        groups = _group_continuous(sorted(selected, key=lambda s: s.start))
        # Check every gap between groups is >= 30 min (explicit min_gap_minutes=30)
        for i in range(len(groups) - 1):
            gap_min = int(
                (groups[i+1][0].start - groups[i][-1].end).total_seconds() / 60
            )
            self.assertGreaterEqual(
                gap_min, 30,
                f"Gap of {gap_min} min between blocks violates min_gap_minutes=30"
            )

    def test_isolated_cheap_slot_with_price_ceiling(self):
        # Regression: when a price ceiling excludes slots on both sides of a cheap
        # slot, the candidate array has an index-adjacent entry that is NOT
        # time-adjacent.  The DP must not form a block across this time gap,
        # producing a 1-slot (15 min) block that violates min_slot_minutes=30.
        #
        # Reproduces the 2026-04-13 production bug:
        #   20:45 UTC (5.4 c/kWh) — isolated, neighbors above ceiling
        #   21:00 UTC (10.5 c/kWh) — ABOVE ceiling, excluded
        #   21:15 UTC (10.3 c/kWh) — ABOVE ceiling, excluded
        #   21:30 UTC (8.5 c/kWh) — below ceiling
        #   21:45 UTC (6.1 c/kWh) — below ceiling
        base = datetime(2026, 4, 13, 20, 45, tzinfo=UTC)
        cheap_isolated = slots_from(base,                         1, price_cents=5.4)
        above_ceiling  = slots_from(base + timedelta(minutes=15), 2, price_cents=10.5)
        after_gap      = slots_from(base + timedelta(minutes=45), 6, price_cents=7.0)
        all_slots = cheap_isolated + above_ceiling + after_gap

        ceiling = 9.8  # c/kWh — excludes the two above-ceiling slots
        selected = select_charging_windows(
            all_slots, required_minutes=30, min_slot_minutes=30,
            max_price=ceiling / 100,
        )
        groups = _group_continuous(sorted(selected, key=lambda s: s.start))
        for group in groups:
            duration = sum(s.duration_minutes for s in group)
            self.assertGreaterEqual(
                duration, 30,
                f"Block of {duration} min violates min_slot_minutes=30 "
                f"(isolated cheap slot leaked through price-ceiling gap)"
            )
        # The isolated 20:45 slot must not appear — it cannot form a valid block
        selected_starts = {s.start for s in selected}
        self.assertNotIn(
            base, selected_starts,
            "Isolated cheap slot at 20:45 must not be selected when it cannot form a 30-min block"
        )



    """_check_window_coverage exits cleanly when prices are not yet published."""

    def _window(self):
        return _resolve_window_utc("00:00", "06:30", FI_TZ, _anchor_date=REF_DATE)

    def test_full_coverage_does_not_exit(self):
        ws, we = self._window()
        # Build slots covering the full window
        slots = slots_from(ws, int((we - ws).total_seconds() // 900))
        from charging_planner import _check_window_coverage
        # Should not raise SystemExit
        _check_window_coverage(slots, ws, we, "test")

    def test_empty_slots_returns_false(self):
        ws, we = self._window()
        from charging_planner import _check_window_coverage
        self.assertFalse(_check_window_coverage([], ws, we, "test"))

    def test_partial_coverage_below_threshold_returns_false(self):
        ws, we = self._window()
        # Only cover 50% of the window
        window_min = int((we - ws).total_seconds() // 60)
        slots = slots_from(ws, window_min // 30)  # half the slots
        from charging_planner import _check_window_coverage
        self.assertFalse(_check_window_coverage(slots, ws, we, "test"))

    def test_coverage_above_threshold_does_not_exit(self):
        ws, we = self._window()
        # Cover 95% of window
        window_min = int((we - ws).total_seconds() // 60)
        slots = slots_from(ws, int(window_min * 0.95 // 15))
        from charging_planner import _check_window_coverage
        _check_window_coverage(slots, ws, we, "test")  # must not raise

    def test_cmd_plan_exits_when_prices_missing(self):
        """cmd_plan exits cleanly if fetched prices don't cover any profile's window."""
        from charging_planner import cmd_plan
        import unittest.mock as mock

        # Only 1h of prices — far below 90% of any window
        one_hour = slots_from(datetime(2026, 3, 14, 22, 0, tzinfo=UTC), 4)
        with mock.patch("charging_planner.fetch_entsoe_prices", return_value=one_hour):
            with self.assertRaises(SystemExit) as ctx:
                cmd_plan({
                    "entsoe": {"api_key": "x", "area": "FI", "timezone": "Europe/Helsinki"},
                    "charging": [{
                        "name": "topup",
                        "required_hours": 2,
                        "preferred_window_start": "00:00",
                        "preferred_window_end": "06:30",
                    }],
                }, output_dir="/tmp")
        self.assertEqual(ctx.exception.code, 1)

# ===========================================================================
# 10. End-to-end pipeline
# ===========================================================================

class TestEndToEnd(unittest.TestCase):
    """Smoke tests for cmd_plan with a mocked ENTSO-E fetch.

    The synthetic prices are anchored to 2026-03-14. datetime.now is pinned to
    2026-03-14 14:30 UTC so window resolution always targets that same night,
    regardless of when the tests are run.
    """

    # Pin the clock to 14:30 UTC on the day the synthetic prices are built around.
    # This is before any overnight window starts (22:00 Helsinki = 20:00 UTC).
    _FROZEN_NOW = datetime(2026, 3, 14, 14, 30, tzinfo=UTC)

    def _run_cmd_plan(self, prices):
        """Run cmd_plan with frozen clock and mocked price fetch."""
        import charging_planner as cp
        import tempfile

        class _FrozenDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                return TestEndToEnd._FROZEN_NOW if tz is None \
                    else TestEndToEnd._FROZEN_NOW.astimezone(tz)

        with tempfile.TemporaryDirectory() as tmpdir, \
             mock.patch("charging_planner.datetime", _FrozenDatetime), \
             mock.patch("charging_planner.fetch_entsoe_prices", return_value=prices):
            return cp.cmd_plan(self.RAW_CONFIG, output_dir=tmpdir)

    RAW_CONFIG = {
        "entsoe": {"api_key": "test", "area": "FI", "timezone": "Europe/Helsinki"},
        "charging": [
            {
                "name": "topup",
                "required_hours": 2,
                "continuous_only": False,
                "min_slot_minutes": 30,
                "preferred_window_start": "00:00",
                "preferred_window_end": "06:30",
            },
            {
                "name": "overnight",
                "required_hours": 6,
                "continuous_only": True,
                "min_slot_minutes": 30,
                "preferred_window_start": "22:00",
                "preferred_window_end": "06:30",
            },
        ],
    }

    def _make_prices(self):
        """192 slots covering 48h, cheap 22:00–07:00 Helsinki.

        Wide enough to cover both same-day windows (00:00–06:30 tomorrow)
        and overnight windows (22:00 tonight – 06:30 tomorrow morning).
        """
        base = datetime(2026, 3, 14, 20, 0, tzinfo=UTC)  # 22:00 Helsinki
        slots = []
        for i in range(192):
            t = base + timedelta(minutes=15 * i)
            local_h = t.astimezone(FI_TZ).hour
            price = 1.5 if (local_h < 7 or local_h >= 22) else 8.0
            slots.append(Slot(
                start=t, end=t + timedelta(minutes=15),
                duration_minutes=15, price_eur_kwh=price / 100, slot=i,
            ))
        return slots

    def test_produces_one_plan_per_profile(self):
        plans = self._run_cmd_plan(self._make_prices())
        self.assertEqual(len(plans), 2)
        self.assertEqual(plans[0]["profile"], "topup")
        self.assertEqual(plans[1]["profile"], "overnight")

    def test_topup_schedules_required_minutes(self):
        plans = self._run_cmd_plan(self._make_prices())
        self.assertGreaterEqual(plans[0]["total_minutes"], 120)

    def test_overnight_schedules_required_minutes(self):
        plans = self._run_cmd_plan(self._make_prices())
        self.assertGreaterEqual(plans[1]["total_minutes"], 360)

    def test_overnight_windows_within_preferred_window(self):
        plans = self._run_cmd_plan(self._make_prices())
        win_end_utc = datetime.fromisoformat(plans[1]["window_ends_utc"][-1])
        # 06:30 Helsinki EET = 04:30 UTC
        self.assertLessEqual(win_end_utc, datetime(2026, 3, 16, 4, 30, tzinfo=UTC))

    def test_plans_contain_ocpp_profile(self):
        plans = self._run_cmd_plan(self._make_prices())
        for plan in plans:
            self.assertIn("ocpp_charging_profile", plan)
            self.assertIn("chargingSchedule", plan["ocpp_charging_profile"])

    def test_plan_json_written_to_output_dir(self):
        import tempfile, os
        prices = self._make_prices()
        with tempfile.TemporaryDirectory() as tmpdir:
            import charging_planner as cp

            class _FrozenDatetime(datetime):
                @classmethod
                def now(cls, tz=None):
                    return TestEndToEnd._FROZEN_NOW if tz is None \
                        else TestEndToEnd._FROZEN_NOW.astimezone(tz)

            with mock.patch("charging_planner.datetime", _FrozenDatetime), \
                 mock.patch("charging_planner.fetch_entsoe_prices", return_value=prices):
                cp.cmd_plan(self.RAW_CONFIG, output_dir=tmpdir)
            files = os.listdir(tmpdir)
        self.assertIn("plan-topup.json", files)
        self.assertIn("plan-overnight.json", files)



# ===========================================================================
# 11. Real ENTSO-E data
# ===========================================================================

# Real ENTSO-E API response captured on 2026-03-14 at 15:10 UTC.
# Two TimeSeries:
#   TS1: 2026-03-12T23:00Z – 2026-03-13T23:00Z  (2026-03-13 Helsinki)
#   TS2: 2026-03-13T23:00Z – 2026-03-14T23:00Z  (2026-03-14 Helsinki)
# 15-minute resolution, sparse (forward-fill encoding).
# Known: TS2 morning ~4.99 c€/kWh, peak 22–35 c€/kWh, night 26–30 c€/kWh
REAL_ENTSOE_XML = """<?xml version="1.0" encoding="UTF-8"?>
<Publication_MarketDocument xmlns="urn:iec62325.351:tc57wg16:451-3:publicationdocument:7:3">
<mRID>67ae7c1eb55f4a02b089a2fa84863e19</mRID>
<revisionNumber>1</revisionNumber>
<type>A44</type>
<createdDateTime>2026-03-14T15:10:13Z</createdDateTime>
<period.timeInterval>
  <start>2026-03-12T23:00Z</start>
  <end>2026-03-14T23:00Z</end>
</period.timeInterval>
<TimeSeries>
  <mRID>1</mRID>
  <businessType>A62</businessType>
  <in_Domain.mRID codingScheme="A01">10YFI-1--------U</in_Domain.mRID>
  <out_Domain.mRID codingScheme="A01">10YFI-1--------U</out_Domain.mRID>
  <currency_Unit.name>EUR</currency_Unit.name>
  <price_Measure_Unit.name>MWH</price_Measure_Unit.name>
  <curveType>A03</curveType>
  <Period>
    <timeInterval>
      <start>2026-03-12T23:00Z</start>
      <end>2026-03-13T23:00Z</end>
    </timeInterval>
    <resolution>PT15M</resolution>
    <Point><position>1</position><price.amount>2.07</price.amount></Point>
    <Point><position>2</position><price.amount>2</price.amount></Point>
    <Point><position>5</position><price.amount>1.99</price.amount></Point>
    <Point><position>7</position><price.amount>1.98</price.amount></Point>
    <Point><position>8</position><price.amount>1.97</price.amount></Point>
    <Point><position>9</position><price.amount>1.5</price.amount></Point>
    <Point><position>10</position><price.amount>1.48</price.amount></Point>
    <Point><position>11</position><price.amount>1.32</price.amount></Point>
    <Point><position>13</position><price.amount>1.95</price.amount></Point>
    <Point><position>17</position><price.amount>1.82</price.amount></Point>
    <Point><position>18</position><price.amount>1.96</price.amount></Point>
    <Point><position>19</position><price.amount>1.99</price.amount></Point>
    <Point><position>21</position><price.amount>2.04</price.amount></Point>
    <Point><position>22</position><price.amount>2.41</price.amount></Point>
    <Point><position>23</position><price.amount>2.63</price.amount></Point>
    <Point><position>24</position><price.amount>2.62</price.amount></Point>
    <Point><position>25</position><price.amount>3</price.amount></Point>
    <Point><position>26</position><price.amount>3.99</price.amount></Point>
    <Point><position>27</position><price.amount>4</price.amount></Point>
    <Point><position>28</position><price.amount>4.19</price.amount></Point>
    <Point><position>29</position><price.amount>5</price.amount></Point>
    <Point><position>30</position><price.amount>4.96</price.amount></Point>
    <Point><position>31</position><price.amount>4.99</price.amount></Point>
    <Point><position>32</position><price.amount>4.98</price.amount></Point>
    <Point><position>33</position><price.amount>4.99</price.amount></Point>
    <Point><position>34</position><price.amount>4.79</price.amount></Point>
    <Point><position>35</position><price.amount>3.38</price.amount></Point>
    <Point><position>36</position><price.amount>2.69</price.amount></Point>
    <Point><position>37</position><price.amount>2.68</price.amount></Point>
    <Point><position>38</position><price.amount>2.63</price.amount></Point>
    <Point><position>39</position><price.amount>2.56</price.amount></Point>
    <Point><position>40</position><price.amount>2.3</price.amount></Point>
    <Point><position>41</position><price.amount>2.54</price.amount></Point>
    <Point><position>42</position><price.amount>2.12</price.amount></Point>
    <Point><position>43</position><price.amount>2.1</price.amount></Point>
    <Point><position>44</position><price.amount>2</price.amount></Point>
    <Point><position>46</position><price.amount>2.02</price.amount></Point>
    <Point><position>47</position><price.amount>1.99</price.amount></Point>
    <Point><position>49</position><price.amount>2</price.amount></Point>
    <Point><position>53</position><price.amount>1.99</price.amount></Point>
    <Point><position>54</position><price.amount>2</price.amount></Point>
    <Point><position>55</position><price.amount>2.1</price.amount></Point>
    <Point><position>56</position><price.amount>2.15</price.amount></Point>
    <Point><position>57</position><price.amount>1.97</price.amount></Point>
    <Point><position>58</position><price.amount>2.07</price.amount></Point>
    <Point><position>59</position><price.amount>2.14</price.amount></Point>
    <Point><position>60</position><price.amount>2.85</price.amount></Point>
    <Point><position>61</position><price.amount>2.33</price.amount></Point>
    <Point><position>62</position><price.amount>3.06</price.amount></Point>
    <Point><position>63</position><price.amount>3.39</price.amount></Point>
    <Point><position>64</position><price.amount>4.99</price.amount></Point>
    <Point><position>65</position><price.amount>5.29</price.amount></Point>
    <Point><position>66</position><price.amount>6.3</price.amount></Point>
    <Point><position>67</position><price.amount>6.83</price.amount></Point>
    <Point><position>68</position><price.amount>8.25</price.amount></Point>
    <Point><position>69</position><price.amount>7.06</price.amount></Point>
    <Point><position>70</position><price.amount>7.94</price.amount></Point>
    <Point><position>71</position><price.amount>8</price.amount></Point>
    <Point><position>72</position><price.amount>8.56</price.amount></Point>
    <Point><position>73</position><price.amount>8.17</price.amount></Point>
    <Point><position>74</position><price.amount>8.2</price.amount></Point>
    <Point><position>75</position><price.amount>8.35</price.amount></Point>
    <Point><position>76</position><price.amount>8.5</price.amount></Point>
    <Point><position>77</position><price.amount>8.46</price.amount></Point>
    <Point><position>78</position><price.amount>8.12</price.amount></Point>
    <Point><position>79</position><price.amount>7.92</price.amount></Point>
    <Point><position>80</position><price.amount>7.56</price.amount></Point>
    <Point><position>82</position><price.amount>7.47</price.amount></Point>
    <Point><position>83</position><price.amount>7.22</price.amount></Point>
    <Point><position>84</position><price.amount>6.47</price.amount></Point>
    <Point><position>85</position><price.amount>7.02</price.amount></Point>
    <Point><position>86</position><price.amount>6.98</price.amount></Point>
    <Point><position>87</position><price.amount>6.88</price.amount></Point>
    <Point><position>88</position><price.amount>6.37</price.amount></Point>
    <Point><position>89</position><price.amount>6.46</price.amount></Point>
    <Point><position>90</position><price.amount>6.18</price.amount></Point>
    <Point><position>91</position><price.amount>6.04</price.amount></Point>
    <Point><position>92</position><price.amount>5.37</price.amount></Point>
    <Point><position>93</position><price.amount>5.57</price.amount></Point>
    <Point><position>94</position><price.amount>5.47</price.amount></Point>
    <Point><position>95</position><price.amount>5.28</price.amount></Point>
    <Point><position>96</position><price.amount>5.08</price.amount></Point>
  </Period>
</TimeSeries>
<TimeSeries>
  <mRID>2</mRID>
  <businessType>A62</businessType>
  <in_Domain.mRID codingScheme="A01">10YFI-1--------U</in_Domain.mRID>
  <out_Domain.mRID codingScheme="A01">10YFI-1--------U</out_Domain.mRID>
  <currency_Unit.name>EUR</currency_Unit.name>
  <price_Measure_Unit.name>MWH</price_Measure_Unit.name>
  <curveType>A03</curveType>
  <Period>
    <timeInterval>
      <start>2026-03-13T23:00Z</start>
      <end>2026-03-14T23:00Z</end>
    </timeInterval>
    <resolution>PT15M</resolution>
    <Point><position>1</position><price.amount>4.99</price.amount></Point>
    <Point><position>5</position><price.amount>4.84</price.amount></Point>
    <Point><position>6</position><price.amount>4.95</price.amount></Point>
    <Point><position>7</position><price.amount>4.99</price.amount></Point>
    <Point><position>9</position><price.amount>4.93</price.amount></Point>
    <Point><position>10</position><price.amount>4.96</price.amount></Point>
    <Point><position>11</position><price.amount>4.99</price.amount></Point>
    <Point><position>12</position><price.amount>5</price.amount></Point>
    <Point><position>14</position><price.amount>5.08</price.amount></Point>
    <Point><position>15</position><price.amount>5.48</price.amount></Point>
    <Point><position>16</position><price.amount>5.89</price.amount></Point>
    <Point><position>17</position><price.amount>7.78</price.amount></Point>
    <Point><position>18</position><price.amount>8.03</price.amount></Point>
    <Point><position>19</position><price.amount>8.36</price.amount></Point>
    <Point><position>20</position><price.amount>9.05</price.amount></Point>
    <Point><position>21</position><price.amount>8.52</price.amount></Point>
    <Point><position>22</position><price.amount>9.35</price.amount></Point>
    <Point><position>23</position><price.amount>10.47</price.amount></Point>
    <Point><position>24</position><price.amount>11.48</price.amount></Point>
    <Point><position>25</position><price.amount>9.48</price.amount></Point>
    <Point><position>26</position><price.amount>10.57</price.amount></Point>
    <Point><position>27</position><price.amount>11.36</price.amount></Point>
    <Point><position>28</position><price.amount>11.52</price.amount></Point>
    <Point><position>29</position><price.amount>12.45</price.amount></Point>
    <Point><position>30</position><price.amount>12.51</price.amount></Point>
    <Point><position>31</position><price.amount>12.79</price.amount></Point>
    <Point><position>32</position><price.amount>13.2</price.amount></Point>
    <Point><position>33</position><price.amount>13.24</price.amount></Point>
    <Point><position>34</position><price.amount>13.64</price.amount></Point>
    <Point><position>35</position><price.amount>14.24</price.amount></Point>
    <Point><position>36</position><price.amount>17.88</price.amount></Point>
    <Point><position>37</position><price.amount>13.37</price.amount></Point>
    <Point><position>38</position><price.amount>14.16</price.amount></Point>
    <Point><position>39</position><price.amount>18.36</price.amount></Point>
    <Point><position>40</position><price.amount>22.1</price.amount></Point>
    <Point><position>41</position><price.amount>14.99</price.amount></Point>
    <Point><position>42</position><price.amount>16.7</price.amount></Point>
    <Point><position>43</position><price.amount>19.94</price.amount></Point>
    <Point><position>44</position><price.amount>22.19</price.amount></Point>
    <Point><position>45</position><price.amount>17.94</price.amount></Point>
    <Point><position>46</position><price.amount>19.99</price.amount></Point>
    <Point><position>47</position><price.amount>19.92</price.amount></Point>
    <Point><position>48</position><price.amount>21.47</price.amount></Point>
    <Point><position>49</position><price.amount>17.89</price.amount></Point>
    <Point><position>50</position><price.amount>19.41</price.amount></Point>
    <Point><position>51</position><price.amount>20.84</price.amount></Point>
    <Point><position>52</position><price.amount>21.87</price.amount></Point>
    <Point><position>53</position><price.amount>15.86</price.amount></Point>
    <Point><position>54</position><price.amount>18.3</price.amount></Point>
    <Point><position>55</position><price.amount>21.97</price.amount></Point>
    <Point><position>56</position><price.amount>25.07</price.amount></Point>
    <Point><position>57</position><price.amount>18.17</price.amount></Point>
    <Point><position>58</position><price.amount>21.73</price.amount></Point>
    <Point><position>59</position><price.amount>24.53</price.amount></Point>
    <Point><position>60</position><price.amount>27.99</price.amount></Point>
    <Point><position>61</position><price.amount>22.92</price.amount></Point>
    <Point><position>62</position><price.amount>28.33</price.amount></Point>
    <Point><position>63</position><price.amount>30</price.amount></Point>
    <Point><position>64</position><price.amount>32.79</price.amount></Point>
    <Point><position>65</position><price.amount>27.51</price.amount></Point>
    <Point><position>66</position><price.amount>29.99</price.amount></Point>
    <Point><position>67</position><price.amount>32.8</price.amount></Point>
    <Point><position>68</position><price.amount>35.31</price.amount></Point>
    <Point><position>69</position><price.amount>30.31</price.amount></Point>
    <Point><position>70</position><price.amount>30.82</price.amount></Point>
    <Point><position>71</position><price.amount>31.96</price.amount></Point>
    <Point><position>72</position><price.amount>31.32</price.amount></Point>
    <Point><position>73</position><price.amount>31.92</price.amount></Point>
    <Point><position>74</position><price.amount>30.62</price.amount></Point>
    <Point><position>75</position><price.amount>35</price.amount></Point>
    <Point><position>76</position><price.amount>32</price.amount></Point>
    <Point><position>77</position><price.amount>31.99</price.amount></Point>
    <Point><position>78</position><price.amount>30</price.amount></Point>
    <Point><position>79</position><price.amount>28</price.amount></Point>
    <Point><position>80</position><price.amount>26.08</price.amount></Point>
    <Point><position>81</position><price.amount>31.11</price.amount></Point>
    <Point><position>82</position><price.amount>29.64</price.amount></Point>
    <Point><position>83</position><price.amount>27.94</price.amount></Point>
    <Point><position>84</position><price.amount>26.99</price.amount></Point>
    <Point><position>85</position><price.amount>29.81</price.amount></Point>
    <Point><position>86</position><price.amount>30</price.amount></Point>
    <Point><position>89</position><price.amount>34.97</price.amount></Point>
    <Point><position>90</position><price.amount>32</price.amount></Point>
    <Point><position>91</position><price.amount>30.26</price.amount></Point>
    <Point><position>92</position><price.amount>30</price.amount></Point>
    <Point><position>93</position><price.amount>30.88</price.amount></Point>
    <Point><position>94</position><price.amount>29.99</price.amount></Point>
    <Point><position>95</position><price.amount>27.93</price.amount></Point>
    <Point><position>96</position><price.amount>26.26</price.amount></Point>
  </Period>
</TimeSeries>
</Publication_MarketDocument>
"""


class TestPriceSourceRules(unittest.TestCase):
    """Tests for the four price source rules:

    1. Real prices available and reach tomorrow: display forecast always appended,
       price_source stays as real source, display forecast not used for planning.
    2. Real prices available but don't reach tomorrow noon: forecast supplements
       real prices for planning, price_source marked as forecast.
    3. No real prices available: forecast used for everything, price_source=forecast.
    4. Planning horizon caps slot selection but not display forecast slots.
    """

    _FROZEN_NOW = datetime(2026, 3, 14, 14, 30, tzinfo=UTC)

    RAW_CONFIG = {
        "entsoe": {"api_key": "test", "area": "FI", "timezone": "Europe/Helsinki"},
        "charging": [{
            "name": "topup",
            "required_hours": 1,
            "preferred_window_start": "22:00",
            "preferred_window_end": "06:30",
        }],
    }

    def _make_real_prices(self, hours=48):
        """Real prices covering `hours` hours from frozen now."""
        base = datetime(2026, 3, 14, 12, 0, tzinfo=UTC)
        slots = []
        for i in range(hours * 4):
            t = base + timedelta(minutes=15 * i)
            slots.append(Slot(
                start=t, end=t + timedelta(minutes=15),
                duration_minutes=15, price_eur_kwh=0.05, slot=i,
            ))
        return slots

    def _make_forecast_prices(self, hours=48):
        """Forecast prices for `hours` hours starting from frozen now."""
        # Start from frozen now so they always cover the planning window
        base = datetime(2026, 3, 14, 12, 0, tzinfo=UTC)
        slots = []
        for i in range(hours * 4):
            t = base + timedelta(minutes=15 * i)
            slots.append(Slot(
                start=t, end=t + timedelta(minutes=15),
                duration_minutes=15, price_eur_kwh=0.03, slot=i,
            ))
        return slots

    def _run(self, real_prices, forecast_prices=None, display_slots=None):
        import charging_planner as cp
        import tempfile

        class _FrozenDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                return TestPriceSourceRules._FROZEN_NOW if tz is None                     else TestPriceSourceRules._FROZEN_NOW.astimezone(tz)

        forecast_prices = forecast_prices or []
        display_slots   = display_slots   or []

        with tempfile.TemporaryDirectory() as tmpdir,              mock.patch("charging_planner.datetime", _FrozenDatetime),              mock.patch("charging_planner.fetch_entsoe_prices",
                        return_value=real_prices),              mock.patch("charging_planner.fetch_forecast_prices",
                        return_value=forecast_prices),              mock.patch("charging_planner.fetch_forecast_display_slots",
                        return_value=display_slots):
            return cp.cmd_plan(self.RAW_CONFIG, output_dir=tmpdir)

    # ── Rule 1: real prices reach tomorrow ───────────────────────────────────

    def test_rule1_price_source_is_real_when_prices_reach_tomorrow(self):
        """When real prices cover tomorrow, price_source stays as the real source."""
        plans = self._run(self._make_real_prices(hours=48))
        self.assertEqual(plans[0]["price_source"], "ENTSO-E")

    def test_rule1_display_forecast_appended_to_json(self):
        """Display forecast slots are always appended to price_slots with forecasted=True."""
        real = self._make_real_prices(hours=48)
        # Display slots start after real prices end
        last_real = max(s.start for s in real)
        base = last_real + timedelta(minutes=15)
        display = [Slot(start=base + timedelta(minutes=15*i),
                        end=base + timedelta(minutes=15*(i+1)),
                        duration_minutes=15, price_eur_kwh=0.03, slot=i)
                   for i in range(96)]
        plans = self._run(real, display_slots=display)
        forecasted = [s for s in plans[0]["price_slots"] if s.get("forecasted")]
        self.assertGreater(len(forecasted), 0)

    def test_rule1_display_forecast_not_used_for_charging(self):
        """Display forecast slots must not be selected as charging slots."""
        real = self._make_real_prices(hours=48)
        last_real = max(s.start for s in real)
        base = last_real + timedelta(minutes=15)
        display = [Slot(start=base + timedelta(minutes=15*i),
                        end=base + timedelta(minutes=15*(i+1)),
                        duration_minutes=15, price_eur_kwh=0.03, slot=i)
                   for i in range(96)]
        plans = self._run(real, display_slots=display)
        forecasted_starts = {s["start_utc"] for s in plans[0]["price_slots"] if s.get("forecasted")}
        charging_starts   = {s["start_utc"] for s in plans[0]["price_slots"] if s.get("charging")}
        self.assertEqual(forecasted_starts & charging_starts, set())

    # ── Rule 2: real prices don't reach tomorrow noon ────────────────────────

    def test_rule2_price_source_marked_forecast_when_supplemented(self):
        """When real prices don't reach tomorrow noon, price_source is marked as forecast."""
        short_real = self._make_real_prices(hours=6)   # only 6h, won't reach tomorrow
        forecast   = self._make_forecast_prices(hours=24)
        plans = self._run(short_real, forecast_prices=forecast)
        self.assertEqual(plans[0]["price_source"], "forecast")

    def test_rule2_forecast_used_for_slot_selection(self):
        """When supplemented, forecast slots can be selected as charging slots."""
        short_real = self._make_real_prices(hours=6)
        forecast   = self._make_forecast_prices(hours=24)
        plans = self._run(short_real, forecast_prices=forecast)
        self.assertGreater(plans[0]["total_minutes"], 0)

    def test_rule2_supplement_slots_tagged_forecasted_in_json(self):
        """Supplement forecast slots appear as forecasted:true in price_slots JSON."""
        short_real = self._make_real_prices(hours=6)
        forecast   = self._make_forecast_prices(hours=48)
        plans = self._run(short_real, forecast_prices=forecast)
        slots = plans[0]["price_slots"]
        # Supplement slots are those beyond the last real slot
        last_real_start = max(s.start for s in short_real)
        supplement_slots = [
            s for s in slots
            if datetime.fromisoformat(s["start_utc"]).replace(tzinfo=UTC) > last_real_start
            and not s.get("forecasted")
        ]
        # All slots beyond last real should be tagged forecasted
        self.assertEqual(supplement_slots, [],
            "Supplement slots beyond real prices should have forecasted:true")

    # ── Rule 3: no real prices ────────────────────────────────────────────────

    def test_rule3_price_source_forecast_when_no_real_prices(self):
        """When no real prices, price_source is forecast.

        For FI area the chain is ENTSO-E → Elering → Sähkötin → forecast,
        so all three real sources must fail before forecast is reached.
        """
        import charging_planner as cp
        import tempfile

        class _FrozenDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                return TestPriceSourceRules._FROZEN_NOW if tz is None \
                    else TestPriceSourceRules._FROZEN_NOW.astimezone(tz)

        forecast = self._make_forecast_prices(hours=48)
        with tempfile.TemporaryDirectory() as tmpdir:
            with mock.patch("charging_planner.datetime", _FrozenDatetime):
                with mock.patch("charging_planner.fetch_entsoe_prices",
                               side_effect=Exception("unavailable")):
                    with mock.patch("charging_planner.fetch_elering_prices",
                                   side_effect=PricesNotYetAvailable("unavailable")):
                        with mock.patch("charging_planner.fetch_sahkotin_prices",
                                       side_effect=PricesNotYetAvailable("unavailable")):
                            with mock.patch("charging_planner.fetch_forecast_prices",
                                           return_value=forecast):
                                with mock.patch("charging_planner.fetch_forecast_display_slots",
                                               return_value=[]):
                                    plans = cp.cmd_plan(self.RAW_CONFIG, output_dir=tmpdir)
        self.assertEqual(plans[0]["price_source"], "forecast")

    # ── Rule 4: horizon caps planning not display ─────────────────────────────

    def test_rule4_display_forecast_extends_beyond_planning_horizon(self):
        """Display forecast slots extend beyond tomorrow 23:00 UTC horizon."""
        # Real prices end at horizon; display slots extend 24h beyond
        display = self._make_forecast_prices(hours=24)
        plans = self._run(self._make_real_prices(hours=48), display_slots=display)
        forecasted = [s for s in plans[0]["price_slots"] if s.get("forecasted")]
        if forecasted:
            last_forecasted = max(s["start_utc"] for s in forecasted)
            tomorrow_23 = "2026-03-15T23:00:00"
            self.assertGreater(last_forecasted, tomorrow_23)

    def test_rule4_charging_slots_within_planning_horizon(self):
        """Charging slots must not be scheduled beyond the planning horizon."""
        display = self._make_forecast_prices(hours=24)
        plans = self._run(self._make_real_prices(hours=48), display_slots=display)
        tomorrow_23_utc = datetime(2026, 3, 15, 23, 0, tzinfo=UTC)
        for s in plans[0]["price_slots"]:
            if s.get("charging"):
                slot_start = datetime.fromisoformat(s["start_utc"])
                self.assertLessEqual(slot_start, tomorrow_23_utc,
                    f"Charging slot {s['start_utc']} exceeds planning horizon")



class TestRealEntsoEData(unittest.TestCase):

    def _slots(self, ref=date(2026, 3, 14)):
        return _parse_entsoe_xml(REAL_ENTSOE_XML, ref, "FI")

    # ── Parsing correctness ──────────────────────────────────────────────────

    def test_parses_two_time_series(self):
        slots = self._slots()
        # Two 24h days × 96 slots/day = 192 slots; some may overlap at boundaries
        self.assertGreaterEqual(len(slots), 96)

    def test_no_duplicate_start_times(self):
        slots = self._slots()
        starts = [s.start for s in slots]
        self.assertEqual(len(starts), len(set(starts)))

    def test_all_slots_15_minutes(self):
        slots = self._slots()
        for s in slots:
            self.assertEqual(s.duration_minutes, 15)

    def test_slots_sorted_ascending(self):
        slots = self._slots()
        starts = [s.start for s in slots]
        self.assertEqual(starts, sorted(starts))

    def test_ordinals_sequential(self):
        slots = self._slots()
        for i, s in enumerate(slots):
            self.assertEqual(s.slot, i)

    def test_prices_are_positive(self):
        slots = self._slots()
        for s in slots:
            self.assertGreater(s.price_eur_kwh, 0)

    def test_prices_in_plausible_range(self):
        # Finnish day-ahead prices on 2026-03-13/14 should be between 0 and 1 €/kWh
        slots = self._slots()
        for s in slots:
            self.assertLess(s.price_eur_kwh, 1.0,
                            f"Price {s.price_eur_kwh:.4f} €/kWh seems implausibly high")

    def test_forward_fill_applied(self):
        # TS2 position 1 = 4.99 EUR/MWh; positions 2–4 not listed → should carry forward
        slots = self._slots()
        # TS2 starts at 2026-03-13T23:00Z
        ts2_start = datetime(2026, 3, 13, 23, 0, tzinfo=UTC)
        ts2_slots = [s for s in slots if s.start >= ts2_start][:4]
        self.assertEqual(len(ts2_slots), 4)
        # All four should have the same price (4.99 EUR/MWh = 0.00499 EUR/kWh)
        for s in ts2_slots:
            self.assertAlmostEqual(s.price_eur_kwh, 0.00499, places=4)

    def test_known_peak_price(self):
        # TS2 position 40 = 22.1 EUR/MWh at 2026-03-13T23:00Z + 39×15min = 2026-03-14T08:45Z
        # = 10:45 Helsinki EET
        slots = self._slots()
        peak_time = datetime(2026, 3, 14, 8, 45, tzinfo=UTC)
        peak_slot = next((s for s in slots if s.start == peak_time), None)
        self.assertIsNotNone(peak_slot, "Expected slot at 08:45 UTC not found")
        self.assertAlmostEqual(peak_slot.price_eur_kwh, 0.0221, places=4)

    # ── Selection with real prices ───────────────────────────────────────────

    def test_topup_selects_cheapest_window(self):
        """2h topup in 00:00–06:30 Helsinki should pick the cheapest morning slots."""
        slots = self._slots()
        # Filter to the 00:00–06:30 Helsinki window on 2026-03-14
        anchor = date(2026, 3, 14)
        ws, we = _resolve_window_utc("00:00", "06:30", FI_TZ, _anchor_date=anchor)
        inside, _ = filter_preferred_window(slots, ws, we, "00:00", "06:30")
        selected = select_charging_windows(inside, required_minutes=120,
                                           min_slot_minutes=30)
        self.assertEqual(sum(s.duration_minutes for s in selected), 120)
        # All selected slots must be within the window
        for s in selected:
            self.assertGreaterEqual(s.start, ws)
            self.assertLessEqual(s.end, we)
        # Avg price should be well below the day's avg (morning is cheap)
        avg = sum(s.price_eur_kwh for s in selected) / len(selected)
        self.assertLess(avg * 100, 6.0)  # below 6 c€/kWh

    def test_continuous_block_stays_within_window(self):
        """6h continuous block in 00:00–06:30 window should fit entirely inside."""
        slots = self._slots()
        anchor = date(2026, 3, 14)
        ws, we = _resolve_window_utc("00:00", "06:30", FI_TZ, _anchor_date=anchor)
        inside, _ = filter_preferred_window(slots, ws, we, "00:00", "06:30")
        selected = select_charging_windows(inside, required_minutes=360,
                                           continuous_only=True,
                                           min_slot_minutes=30)
        self.assertEqual(sum(s.duration_minutes for s in selected), 360)
        for s in selected:
            self.assertGreaterEqual(s.start, ws)
            self.assertLessEqual(s.end, we)
        # Must be one continuous block
        srt = sorted(selected, key=lambda s: s.start)
        for i in range(len(srt) - 1):
            self.assertEqual(srt[i].end, srt[i + 1].start)

    def test_selected_slots_cheaper_than_peak(self):
        """Scheduled slots should be significantly cheaper than the day's peak."""
        slots = self._slots()
        anchor = date(2026, 3, 14)
        ws, we = _resolve_window_utc("00:00", "06:30", FI_TZ, _anchor_date=anchor)
        inside, _ = filter_preferred_window(slots, ws, we, "00:00", "06:30")
        selected = select_charging_windows(inside, required_minutes=120,
                                           min_slot_minutes=30)
        avg_selected = sum(s.price_eur_kwh for s in selected) / len(selected)
        # TS2 peak is ~35 c€/kWh (0.35 EUR/kWh); selected morning should be < 10%
        self.assertLess(avg_selected, 0.10)


# ===========================================================================
# 12. Area-based fallback chain
# ===========================================================================

class TestBuildFallbackChain(unittest.TestCase):
    """Unit tests for _build_fallback_chain.

    Verifies that the correct fetch functions are returned in the correct
    order for each area, and that area-inappropriate sources are never
    included.

    Chain by area:
      FI:        ENTSO-E → Elering → Sähkötin → forecast
      EE/LV/LT:  ENTSO-E → Elering
      SE1–SE4:   ENTSO-E only
      NO1–NO5:   ENTSO-E only
      other:     ENTSO-E only
    """

    def _names(self, area):
        from charging_planner import _build_fallback_chain
        return [name for _fn, name in _build_fallback_chain(area)]

    # ── FI ────────────────────────────────────────────────────────────────────

    def test_fi_chain_length(self):
        from charging_planner import _build_fallback_chain
        self.assertEqual(len(_build_fallback_chain("FI")), 4)

    def test_fi_chain_order(self):
        self.assertEqual(self._names("FI"), [
            "ENTSO-E",
            "Elering",
            "Sähkötin",
            "forecast",
        ])

    def test_fi_eic_code_same_chain(self):
        """Full EIC code for FI produces the same chain as the short code."""
        self.assertEqual(self._names("FI"), self._names("10YFI-1--------U"))

    # ── EE / LV / LT ─────────────────────────────────────────────────────────

    def test_ee_chain_length(self):
        from charging_planner import _build_fallback_chain
        self.assertEqual(len(_build_fallback_chain("EE")), 2)

    def test_ee_chain_order(self):
        self.assertEqual(self._names("EE"), ["ENTSO-E", "Elering"])

    def test_lv_chain_same_as_ee(self):
        self.assertEqual(self._names("LV"), self._names("EE"))

    def test_lt_chain_same_as_ee(self):
        self.assertEqual(self._names("LT"), self._names("EE"))

    def test_ee_no_sahkotin(self):
        self.assertNotIn("Sähkötin", self._names("EE"))

    def test_ee_no_forecast(self):
        self.assertNotIn("forecast", self._names("EE"))

    # ── SE ────────────────────────────────────────────────────────────────────

    def test_se1_chain_order(self):
        self.assertEqual(self._names("SE1"), ["ENTSO-E", "elprisetjustnu.se"])

    def test_se4_chain_order(self):
        self.assertEqual(self._names("SE4"), ["ENTSO-E", "elprisetjustnu.se"])

    def test_se_no_elering(self):
        self.assertNotIn("Elering", self._names("SE2"))

    def test_se_eic_code(self):
        """Full EIC code for SE3 produces the same chain as the short code."""
        self.assertEqual(self._names("SE3"), self._names("10Y1001A1001A46L"))

    # ── NO ────────────────────────────────────────────────────────────────────

    def test_no1_chain_order(self):
        self.assertEqual(self._names("NO1"), ["ENTSO-E", "hvakosterstrommen.no"])

    def test_no5_chain_order(self):
        self.assertEqual(self._names("NO5"), ["ENTSO-E", "hvakosterstrommen.no"])

    def test_no_no_elering(self):
        self.assertNotIn("Elering", self._names("NO3"))

    def test_no_eic_code(self):
        """Full EIC code for NO2 produces the same chain as the short code."""
        self.assertEqual(self._names("NO2"), self._names("10YNO-2--------T"))

    def test_se_and_no_chains_differ(self):
        """SE and NO use different source names — they are separate functions."""
        self.assertNotEqual(self._names("SE1"), self._names("NO1"))

    # ── Other ─────────────────────────────────────────────────────────────────

    def test_de_entsoe_only(self):
        self.assertEqual(self._names("DE"), ["ENTSO-E"])

    def test_unknown_area_entsoe_only(self):
        self.assertEqual(self._names("XX"), ["ENTSO-E"])

    def test_chain_contains_callables(self):
        from charging_planner import _build_fallback_chain
        for fn, name in _build_fallback_chain("FI"):
            self.assertTrue(callable(fn))


class TestAreaFallbackChainIntegration(unittest.TestCase):
    """Integration tests: cmd_plan calls the right sources and skips wrong ones.

    Each test patches all four fetch functions explicitly so no real network
    calls are made and the mock wiring is unambiguous.
    """

    _FROZEN_NOW = datetime(2026, 3, 14, 14, 30, tzinfo=UTC)

    def _make_prices(self, hours=48, price_eur=0.05):
        base = datetime(2026, 3, 14, 12, 0, tzinfo=UTC)
        return [Slot(start=base + timedelta(minutes=15*i),
                     end=base + timedelta(minutes=15*i+15),
                     duration_minutes=15, price_eur_kwh=price_eur, slot=i)
                for i in range(hours * 4)]

    def _config(self, area="FI"):
        return {
            "entsoe": {"api_key": "test", "area": area,
                       "timezone": "Europe/Helsinki"},
            "charging": [{"name": "topup", "required_hours": 1,
                          "preferred_window_start": "22:00",
                          "preferred_window_end": "06:30"}],
        }

    def _run(self, area, entsoe, elering=None, sahkotin=None, forecast=None,
             elprisetjustnu=None, hvakosterstrommen=None):
        """Run cmd_plan with all fetch functions explicitly patched."""
        import charging_planner as cp
        import tempfile

        _unavail = PricesNotYetAvailable("unavailable")

        def _side(v):
            if v is None:
                return mock.Mock(side_effect=_unavail)
            if isinstance(v, list):
                return mock.Mock(return_value=v)
            return mock.Mock(side_effect=v)

        class _FrozenDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                return TestAreaFallbackChainIntegration._FROZEN_NOW if tz is None \
                    else TestAreaFallbackChainIntegration._FROZEN_NOW.astimezone(tz)

        with tempfile.TemporaryDirectory() as tmpdir, \
             mock.patch("charging_planner.datetime", _FrozenDatetime), \
             mock.patch("charging_planner.fetch_entsoe_prices",            _side(entsoe)), \
             mock.patch("charging_planner.fetch_elering_prices",           _side(elering)), \
             mock.patch("charging_planner.fetch_sahkotin_prices",          _side(sahkotin)), \
             mock.patch("charging_planner.fetch_forecast_prices",          _side(forecast)), \
             mock.patch("charging_planner.fetch_elprisetjustnu_prices",    _side(elprisetjustnu)), \
             mock.patch("charging_planner.fetch_hvakosterstrommen_prices", _side(hvakosterstrommen)), \
             mock.patch("charging_planner.fetch_forecast_display_slots",   return_value=[]):
            return cp.cmd_plan(self._config(area), output_dir=tmpdir)

    # ── FI: ENTSO-E succeeds — no fallback called ─────────────────────────────

    def test_fi_entsoe_success_elering_not_called(self):
        prices = self._make_prices()
        mock_el = mock.Mock(side_effect=AssertionError("should not be called"))
        with mock.patch("charging_planner.fetch_elering_prices", mock_el), \
             mock.patch("charging_planner.fetch_forecast_display_slots", return_value=[]):
            self._run("FI", entsoe=prices)
        mock_el.assert_not_called()

    def test_fi_entsoe_success_price_source(self):
        plans = self._run("FI", entsoe=self._make_prices())
        self.assertEqual(plans[0]["price_source"], "ENTSO-E")

    # ── FI: ENTSO-E fails → Elering ───────────────────────────────────────────

    def test_fi_elering_tried_when_entsoe_fails(self):
        prices = self._make_prices()
        plans = self._run("FI",
                          entsoe=Exception("down"),
                          elering=prices)
        self.assertEqual(plans[0]["price_source"], "Elering")

    def test_fi_elering_success_sahkotin_not_called(self):
        prices = self._make_prices()
        mock_sah = mock.Mock(side_effect=AssertionError("should not be called"))
        with mock.patch("charging_planner.fetch_sahkotin_prices", mock_sah):
            self._run("FI", entsoe=Exception("down"), elering=prices)
        mock_sah.assert_not_called()

    # ── FI: ENTSO-E + Elering fail → Sähkötin ────────────────────────────────

    def test_fi_sahkotin_tried_when_entsoe_and_elering_fail(self):
        prices = self._make_prices()
        plans = self._run("FI",
                          entsoe=Exception("down"),
                          elering=PricesNotYetAvailable("down"),
                          sahkotin=prices)
        self.assertEqual(plans[0]["price_source"], "Sähkötin")

    def test_fi_sahkotin_success_forecast_not_called(self):
        prices = self._make_prices()
        mock_fc = mock.Mock(side_effect=AssertionError("should not be called"))
        with mock.patch("charging_planner.fetch_forecast_prices", mock_fc):
            self._run("FI",
                      entsoe=Exception("down"),
                      elering=PricesNotYetAvailable("down"),
                      sahkotin=prices)
        mock_fc.assert_not_called()

    # ── FI: all real sources fail → forecast ──────────────────────────────────

    def test_fi_forecast_tried_when_all_real_fail(self):
        prices = self._make_prices()
        plans = self._run("FI",
                          entsoe=Exception("down"),
                          elering=PricesNotYetAvailable("down"),
                          sahkotin=PricesNotYetAvailable("down"),
                          forecast=prices)
        self.assertEqual(plans[0]["price_source"], "forecast")

    def test_fi_exits_when_all_sources_fail(self):
        with self.assertRaises(SystemExit) as ctx:
            self._run("FI",
                      entsoe=Exception("down"),
                      elering=PricesNotYetAvailable("down"),
                      sahkotin=PricesNotYetAvailable("down"),
                      forecast=PricesNotYetAvailable("down"))
        self.assertEqual(ctx.exception.code, 1)

    # ── EE: ENTSO-E succeeds ──────────────────────────────────────────────────

    def test_ee_entsoe_success_price_source(self):
        plans = self._run("EE", entsoe=self._make_prices())
        self.assertEqual(plans[0]["price_source"], "ENTSO-E")

    # ── EE: ENTSO-E fails → Elering ───────────────────────────────────────────

    def test_ee_elering_tried_when_entsoe_fails(self):
        plans = self._run("EE",
                          entsoe=Exception("down"),
                          elering=self._make_prices())
        self.assertEqual(plans[0]["price_source"], "Elering")

    # ── EE: Sähkötin and forecast never called ────────────────────────────────
    # These patch all fetchers directly so assert_not_called() is unambiguous.

    def _run_all_patched(self, area, entsoe_exc, elering_exc,
                         mock_sah, mock_fc, mock_ep=None, mock_hv=None):
        """Run cmd_plan with every fetcher patched; mocks passed in are used directly."""
        import charging_planner as cp
        import tempfile

        _unavail = PricesNotYetAvailable("unavailable")

        class _FrozenDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                return TestAreaFallbackChainIntegration._FROZEN_NOW if tz is None \
                    else TestAreaFallbackChainIntegration._FROZEN_NOW.astimezone(tz)

        if mock_ep is None:
            mock_ep = mock.Mock(side_effect=_unavail)
        if mock_hv is None:
            mock_hv = mock.Mock(side_effect=_unavail)

        with tempfile.TemporaryDirectory() as tmpdir, \
             mock.patch("charging_planner.datetime", _FrozenDatetime), \
             mock.patch("charging_planner.fetch_entsoe_prices",
                        side_effect=entsoe_exc), \
             mock.patch("charging_planner.fetch_elering_prices",
                        side_effect=elering_exc), \
             mock.patch("charging_planner.fetch_sahkotin_prices",          mock_sah), \
             mock.patch("charging_planner.fetch_forecast_prices",          mock_fc), \
             mock.patch("charging_planner.fetch_elprisetjustnu_prices",    mock_ep), \
             mock.patch("charging_planner.fetch_hvakosterstrommen_prices", mock_hv), \
             mock.patch("charging_planner.fetch_forecast_display_slots",   return_value=[]):
            return cp.cmd_plan(self._config(area), output_dir=tmpdir)

    def test_ee_sahkotin_never_called(self):
        mock_sah = mock.Mock(side_effect=PricesNotYetAvailable("unavailable"))
        mock_fc  = mock.Mock(side_effect=PricesNotYetAvailable("unavailable"))
        with self.assertRaises(SystemExit):
            self._run_all_patched("EE",
                                  entsoe_exc=Exception("down"),
                                  elering_exc=PricesNotYetAvailable("down"),
                                  mock_sah=mock_sah, mock_fc=mock_fc)
        mock_sah.assert_not_called()

    def test_ee_forecast_never_called(self):
        mock_sah = mock.Mock(side_effect=PricesNotYetAvailable("unavailable"))
        mock_fc  = mock.Mock(side_effect=PricesNotYetAvailable("unavailable"))
        with self.assertRaises(SystemExit):
            self._run_all_patched("EE",
                                  entsoe_exc=Exception("down"),
                                  elering_exc=PricesNotYetAvailable("down"),
                                  mock_sah=mock_sah, mock_fc=mock_fc)
        mock_fc.assert_not_called()

    def test_ee_exits_when_entsoe_and_elering_fail(self):
        with self.assertRaises(SystemExit) as ctx:
            self._run("EE",
                      entsoe=Exception("down"),
                      elering=PricesNotYetAvailable("down"))
        self.assertEqual(ctx.exception.code, 1)

    # ── SE1: ENTSO-E → elprisetjustnu.se ─────────────────────────────────────

    def test_se1_entsoe_success_price_source(self):
        plans = self._run("SE1", entsoe=self._make_prices())
        self.assertEqual(plans[0]["price_source"], "ENTSO-E")

    def test_se1_elprisetjustnu_tried_when_entsoe_fails(self):
        plans = self._run("SE1",
                          entsoe=Exception("down"),
                          elprisetjustnu=self._make_prices())
        self.assertEqual(plans[0]["price_source"], "elprisetjustnu.se")

    def test_se1_elering_never_called(self):
        mock_el = mock.Mock(side_effect=PricesNotYetAvailable("unavailable"))
        mock_sah = mock.Mock(side_effect=PricesNotYetAvailable("unavailable"))
        mock_fc  = mock.Mock(side_effect=PricesNotYetAvailable("unavailable"))
        mock_ep  = mock.Mock(side_effect=PricesNotYetAvailable("unavailable"))
        with self.assertRaises(SystemExit):
            self._run_all_patched("SE1",
                                  entsoe_exc=Exception("down"),
                                  elering_exc=PricesNotYetAvailable("unavailable"),
                                  mock_sah=mock_sah, mock_fc=mock_fc, mock_ep=mock_ep)
        mock_el.assert_not_called()

    def test_se1_sahkotin_never_called(self):
        mock_sah = mock.Mock(side_effect=PricesNotYetAvailable("unavailable"))
        mock_fc  = mock.Mock(side_effect=PricesNotYetAvailable("unavailable"))
        mock_ep  = mock.Mock(side_effect=PricesNotYetAvailable("unavailable"))
        with self.assertRaises(SystemExit):
            self._run_all_patched("SE1",
                                  entsoe_exc=Exception("down"),
                                  elering_exc=PricesNotYetAvailable("unavailable"),
                                  mock_sah=mock_sah, mock_fc=mock_fc, mock_ep=mock_ep)
        mock_sah.assert_not_called()

    def test_se1_exits_when_entsoe_and_elprisetjustnu_fail(self):
        with self.assertRaises(SystemExit) as ctx:
            self._run("SE1",
                      entsoe=Exception("down"),
                      elprisetjustnu=PricesNotYetAvailable("down"))
        self.assertEqual(ctx.exception.code, 1)

    # ── NO1: ENTSO-E → hvakosterstrommen.no ──────────────────────────────────

    def test_no1_entsoe_success_price_source(self):
        plans = self._run("NO1", entsoe=self._make_prices())
        self.assertEqual(plans[0]["price_source"], "ENTSO-E")

    def test_no1_hvakosterstrommen_tried_when_entsoe_fails(self):
        plans = self._run("NO1",
                          entsoe=Exception("down"),
                          hvakosterstrommen=self._make_prices())
        self.assertEqual(plans[0]["price_source"], "hvakosterstrommen.no")

    def test_no1_elering_never_called(self):
        mock_el = mock.Mock(side_effect=PricesNotYetAvailable("unavailable"))
        mock_sah = mock.Mock(side_effect=PricesNotYetAvailable("unavailable"))
        mock_fc  = mock.Mock(side_effect=PricesNotYetAvailable("unavailable"))
        mock_hv  = mock.Mock(side_effect=PricesNotYetAvailable("unavailable"))
        with self.assertRaises(SystemExit):
            self._run_all_patched("NO1",
                                  entsoe_exc=Exception("down"),
                                  elering_exc=PricesNotYetAvailable("unavailable"),
                                  mock_sah=mock_sah, mock_fc=mock_fc, mock_hv=mock_hv)
        mock_el.assert_not_called()

    def test_no1_exits_when_entsoe_and_hvakosterstrommen_fail(self):
        with self.assertRaises(SystemExit) as ctx:
            self._run("NO1",
                      entsoe=Exception("down"),
                      hvakosterstrommen=PricesNotYetAvailable("down"))
        self.assertEqual(ctx.exception.code, 1)

    # ── DE: ENTSO-E only — no fallback at all ────────────────────────────────

    def test_de_no_fallback_beyond_entsoe(self):
        mock_el  = mock.Mock(side_effect=PricesNotYetAvailable("unavailable"))
        mock_sah = mock.Mock(side_effect=PricesNotYetAvailable("unavailable"))
        mock_fc  = mock.Mock(side_effect=PricesNotYetAvailable("unavailable"))
        with self.assertRaises(SystemExit):
            self._run_all_patched("DE",
                                  entsoe_exc=Exception("down"),
                                  elering_exc=PricesNotYetAvailable("unavailable"),
                                  mock_sah=mock_sah, mock_fc=mock_fc)
        mock_el.assert_not_called()
        mock_sah.assert_not_called()

    # ── Supplement block: forecast only attempted for FI ─────────────────────

    def test_ee_supplement_not_attempted_when_prices_partial(self):
        """For EE, if real prices don't reach tomorrow noon, we exit rather than
        attempting a forecast supplement (forecast is not in the EE chain)."""
        # 6h of prices won't reach tomorrow noon
        short_prices = self._make_prices(hours=6)
        mock_fc = mock.Mock(side_effect=AssertionError("should not be called"))
        with mock.patch("charging_planner.fetch_forecast_prices", mock_fc):
            with self.assertRaises(SystemExit):
                self._run("EE", entsoe=short_prices)
        mock_fc.assert_not_called()


# ===========================================================================
# 11. Console output — print_plan_summary, _window_bar, GHA summary
# ===========================================================================

def _make_output_plan(*, windows=None, total_minutes=240, avg_price=0.62,
                      retained=0, warning=None, avg_optimal=None) -> dict:
    """Minimal plan dict for print_plan_summary / GHA summary tests."""
    if windows is None:
        windows = [
            {"start": "03:00", "end": "07:00",
             "duration_minutes": 240, "avg_price_cents_kwh": 0.62},
        ]
    return {
        "date": "2026-03-15",
        "area": "FI",
        "price_source": "ENTSO-E",
        "timezone": "Europe/Helsinki",
        "utc_offset_hours": 2,
        "price_stats": {
            "min_cents_kwh": 0.47,
            "max_cents_kwh": 4.27,
            "avg_cents_kwh": 1.64,
        },
        "required_minutes": 240,
        "total_minutes": total_minutes,
        "avg_price_cents_kwh": avg_price,
        "avg_optimal_price_cents_kwh": avg_optimal,
        "windows": windows,
        "retained_minutes": retained,
        "plan_warning": warning,
        "profile": "test",
    }


def _capture_stdout(fn, *args, **kwargs) -> str:
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        fn(*args, **kwargs)
    return buf.getvalue()


class TestPrintPlanSummary(unittest.TestCase):

    def setUp(self):
        patcher = mock.patch("charging_planner._USE_COLOR", False)
        self.addCleanup(patcher.stop)
        patcher.start()

    def _out(self, **kw) -> str:
        return _capture_stdout(print_plan_summary, _make_output_plan(**kw), [])

    # ── Header content ────────────────────────────────────────────────────────

    def test_header_contains_date(self):
        self.assertIn("2026-03-15", self._out())

    def test_header_contains_area(self):
        self.assertIn("FI", self._out())

    def test_header_contains_price_source(self):
        self.assertIn("ENTSO-E", self._out())

    def test_header_contains_timezone(self):
        self.assertIn("Europe/Helsinki", self._out())

    def test_market_prices_line_shows_min_avg_max(self):
        out = self._out()
        self.assertIn("Market prices", out)
        self.assertIn("0.47", out)
        self.assertIn("1.64", out)
        self.assertIn("4.27", out)

    def test_scheduled_line_present(self):
        self.assertIn("Scheduled", self._out())

    # ── Avg price line ────────────────────────────────────────────────────────

    def test_avg_price_line_present_when_slots_scheduled(self):
        self.assertIn("Avg price", self._out())

    def test_avg_price_line_absent_when_no_slots_scheduled(self):
        out = self._out(total_minutes=0, windows=[])
        self.assertNotIn("Avg price", out)

    # ── Charging windows ──────────────────────────────────────────────────────

    def test_window_times_shown(self):
        out = self._out()
        self.assertIn("03:00", out)
        self.assertIn("07:00", out)

    def test_no_windows_message_when_empty(self):
        out = self._out(windows=[], total_minutes=0)
        self.assertIn("No windows selected", out)

    def test_window_count_in_header(self):
        self.assertIn("Charging windows (1)", self._out())

    def test_multiple_windows_count(self):
        plan = _make_output_plan(windows=[
            {"start": "01:00", "end": "03:00", "duration_minutes": 120, "avg_price_cents_kwh": 0.50},
            {"start": "05:00", "end": "06:00", "duration_minutes": 60,  "avg_price_cents_kwh": 0.60},
        ], total_minutes=180)
        out = _capture_stdout(print_plan_summary, plan, [])
        self.assertIn("Charging windows (2)", out)

    # ── Savings vs market ─────────────────────────────────────────────────────

    def test_savings_below_market_avg_shown(self):
        # avg (0.62) < market avg (1.64) → savings = -1.02
        out = self._out()
        self.assertIn("vs market", out)
        self.assertIn("-1.02", out)

    def test_savings_above_market_avg_shown(self):
        # avg (2.00) > market avg (1.64) → "+0.36"
        plan = _make_output_plan(avg_price=2.00)
        out = _capture_stdout(print_plan_summary, plan, [])
        self.assertIn("vs market +", out)

    def test_near_market_avg_shown(self):
        plan = _make_output_plan(avg_price=1.64)
        out = _capture_stdout(print_plan_summary, plan, [])
        self.assertIn("near market avg", out)

    # ── Optional fields ───────────────────────────────────────────────────────

    def test_retained_minutes_shown_when_nonzero(self):
        self.assertIn("carried over", self._out(retained=60))

    def test_retained_minutes_absent_when_zero(self):
        self.assertNotIn("carried over", self._out(retained=0))

    def test_plan_warning_shown(self):
        out = self._out(warning="partial plan — grid limited")
        self.assertIn("grid limited", out)

    def test_plan_warning_absent_when_none(self):
        self.assertNotIn("partial plan", self._out())

    def test_vs_optimal_shown_when_more_expensive(self):
        # avg (0.62) - optimal (0.40) = 0.22 > 0.005 → line shown
        out = self._out(avg_price=0.62, avg_optimal=0.40)
        self.assertIn("vs optimal", out)

    def test_vs_optimal_not_shown_when_near_optimal(self):
        self.assertNotIn("vs optimal", self._out(avg_price=0.62, avg_optimal=0.62))

    # ── Color control ─────────────────────────────────────────────────────────

    def test_no_ansi_codes_when_color_disabled(self):
        self.assertNotIn("\033[", self._out())

    def test_ansi_codes_present_when_color_enabled(self):
        with mock.patch("charging_planner._USE_COLOR", True):
            out = self._out()
        self.assertIn("\033[", out)


class TestWindowBar(unittest.TestCase):

    def setUp(self):
        patcher = mock.patch("charging_planner._USE_COLOR", False)
        self.addCleanup(patcher.stop)
        patcher.start()

    def _bar(self, start="03:00", end="07:00", dur=120,
             avg=1.0, mn=0.5, mx=4.0) -> str:
        return _window_bar(start, end, dur, avg, mn, mx)

    def test_returns_string(self):
        self.assertIsInstance(self._bar(), str)

    def test_contains_start_and_end_times(self):
        result = self._bar(start="02:00", end="06:00")
        self.assertIn("02:00", result)
        self.assertIn("06:00", result)

    def test_contains_avg_price(self):
        self.assertIn("1.23", self._bar(avg=1.23))

    def test_bar_length_for_60_minutes(self):
        # bar_len = max(2, 60 // 15) = 4 → four block chars
        self.assertIn("████", self._bar(dur=60))

    def test_bar_length_minimum_two_blocks(self):
        # dur=1 → bar_len = max(2, 0) = 2
        result = self._bar(dur=1)
        self.assertIn("██", result)

    def test_duration_hours_and_minutes(self):
        # 90 min → "1h30m"
        self.assertIn("1h30m", self._bar(dur=90))

    def test_duration_exact_hours(self):
        # 120 min → "2h00m"
        self.assertIn("2h00m", self._bar(dur=120))

    def test_duration_minutes_only(self):
        # 30 min → "30m" (h=0)
        self.assertIn("30m", self._bar(dur=30))


class TestGhaFmtHours(unittest.TestCase):

    def test_exact_hours(self):
        self.assertEqual(_gha_fmt_hours(120), "2h")

    def test_hours_and_minutes(self):
        self.assertEqual(_gha_fmt_hours(90), "1h30min")

    def test_minutes_only(self):
        self.assertEqual(_gha_fmt_hours(45), "0h45min")


class TestGhaSummaryHeader(unittest.TestCase):

    def _header(self, **kw) -> str:
        return "\n".join(_gha_summary_header(_make_output_plan(**kw)))

    def test_returns_list(self):
        self.assertIsInstance(_gha_summary_header(_make_output_plan()), list)

    def test_contains_date(self):
        self.assertIn("2026-03-15", self._header())

    def test_contains_area(self):
        self.assertIn("FI", self._header())

    def test_contains_price_source(self):
        self.assertIn("ENTSO-E", self._header())

    def test_contains_timezone(self):
        self.assertIn("Europe/Helsinki", self._header())

    def test_contains_utc_offset(self):
        self.assertIn("UTC+2", self._header())

    def test_contains_market_price_range(self):
        lines = self._header()
        self.assertIn("0.47", lines)
        self.assertIn("4.27", lines)
        self.assertIn("1.64", lines)


class TestGhaSummaryProfile(unittest.TestCase):

    def _profile(self, **kw) -> str:
        return "\n".join(_gha_summary_profile(_make_output_plan(**kw), market_avg=1.64))

    def test_returns_list(self):
        self.assertIsInstance(_gha_summary_profile(_make_output_plan(), market_avg=1.64), list)

    def test_contains_profile_name(self):
        self.assertIn("test", self._profile())

    def test_contains_required_hours(self):
        # required_minutes=240 → "4h"
        self.assertIn("4h", self._profile())

    def test_window_table_shows_start_and_end(self):
        lines = self._profile()
        self.assertIn("03:00", lines)
        self.assertIn("07:00", lines)

    def test_no_windows_message_when_empty(self):
        self.assertIn("No windows selected", self._profile(windows=[], total_minutes=0))

    def test_incomplete_plan_warning_shown(self):
        # total_minutes (60) < required_minutes (240)
        self.assertIn("charge plan not possible", self._profile(total_minutes=60))

    def test_savings_amount_shown(self):
        # avg 0.62, market 1.64 → |diff| = 1.02
        self.assertIn("1.02", self._profile(avg_price=0.62))


class TestWriteGhaSummary(unittest.TestCase):

    def test_no_op_when_env_var_not_set(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            write_gha_summary([_make_output_plan()])  # must not raise

    def test_writes_markdown_to_file(self):
        with tempfile.NamedTemporaryFile(mode="r", suffix=".md", delete=False) as f:
            path = f.name
        try:
            with mock.patch.dict("os.environ", {"GITHUB_STEP_SUMMARY": path}):
                write_gha_summary([_make_output_plan()])
            with open(path, encoding="utf-8") as f:
                content = f.read()
            self.assertIn("2026-03-15", content)
            self.assertIn("ENTSO-E", content)
        finally:
            os.unlink(path)

    def test_includes_skipped_profiles_section(self):
        with tempfile.NamedTemporaryFile(mode="r", suffix=".md", delete=False) as f:
            path = f.name
        try:
            with mock.patch.dict("os.environ", {"GITHUB_STEP_SUMMARY": path}):
                write_gha_summary([_make_output_plan()], skipped=["night", "peak"])
            with open(path, encoding="utf-8") as f:
                content = f.read()
            self.assertIn("night", content)
            self.assertIn("peak", content)
            self.assertIn("Skipped profiles", content)
        finally:
            os.unlink(path)

    def test_handles_oserror_gracefully(self):
        with mock.patch.dict("os.environ",
                             {"GITHUB_STEP_SUMMARY": "/nonexistent/path/summary.md"}):
            write_gha_summary([_make_output_plan()])  # must not raise


if __name__ == "__main__":
    unittest.main(verbosity=2)
