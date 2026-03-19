"""
Tests for charger/deliver.py
==============================
Covers the ntfy notification logic in _send_delivery_ntfy — message content,
delivery status formatting, disabled/missing topic handling, and HTTP sending.

Run from the repo root:
    python -m unittest discover -s test -v

Or directly:
    python test/test_deliver.py
"""

import sys
import unittest
import unittest.mock as mock

sys.path.insert(0, ".")
sys.path.insert(0, "..")
sys.path.insert(0, "charger")

from deliver import _send_delivery_ntfy


# ===========================================================================
# Helpers
# ===========================================================================

def make_plan(profile: str = "topup", total: int = 120, required: int = 120,
              windows=None) -> dict:
    if windows is None:
        windows = [{"start": "01:00", "end": "03:00",
                    "duration_minutes": 120, "avg_price_cents_kwh": 2.36}]
    return {
        "profile":                profile,
        "date":                   "2026-03-15",
        "required_minutes":       required,
        "total_minutes":          total,
        "preferred_window_start": "00:00",
        "preferred_window_end":   "06:30",
        "windows":                windows,
        "window_starts_utc":      ["2026-03-14T23:00:00+00:00"],
        "window_ends_utc":        ["2026-03-15T01:00:00+00:00"],
    }


def make_config(enabled: bool = True, topic: str = "") -> dict:
    return {"ntfy": {"enabled": enabled, "topic": topic}}


def _capture_message(plans_by_profile, skipped, results, config):
    """Call _send_delivery_ntfy and capture the message sent to ntfy."""
    sent = {}
    def fake_urlopen(req, timeout=None):
        sent["url"]  = req.full_url
        sent["body"] = req.data.decode()
        sent["title"] = req.get_header("Title")
        cm = mock.MagicMock()
        cm.__enter__ = lambda s: s
        cm.__exit__  = mock.Mock(return_value=False)
        return cm

    with mock.patch.dict("os.environ", {"NTFY_TOPIC": "test-topic"}), \
         mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
        _send_delivery_ntfy(plans_by_profile, skipped, results, config)
    return sent


# ===========================================================================
# Message content
# ===========================================================================

class TestNtfyMessageContent(unittest.TestCase):

    CONFIG = make_config(enabled=True)

    def _send(self, plans_by_profile, skipped=None, results=None):
        return _capture_message(
            plans_by_profile,
            skipped or [],
            results or [],
            self.CONFIG,
        )

    def test_message_contains_profile_name(self):
        sent = self._send({"topup": make_plan("topup")})
        self.assertIn("topup", sent["body"])

    def test_message_contains_window_times(self):
        sent = self._send({"topup": make_plan("topup")})
        self.assertIn("01:00", sent["body"])
        self.assertIn("03:00", sent["body"])

    def test_message_contains_price(self):
        sent = self._send({"topup": make_plan("topup")})
        self.assertIn("2.36", sent["body"])

    def test_incomplete_plan_flagged(self):
        sent = self._send({"topup": make_plan("topup", total=60, required=120)})
        self.assertIn("charge plan not possible", sent["body"])

    def test_skipped_profiles_in_message(self):
        sent = self._send({"topup": make_plan("topup")}, skipped=["overnight"])
        self.assertIn("overnight", sent["body"])
        self.assertIn("Skipped", sent["body"])

    def test_delivery_results_in_message(self):
        results = [("topup", "chargeamps", "CHARGER-001", True)]
        sent = self._send({"topup": make_plan("topup")}, results=results)
        self.assertIn("Deliveries:", sent["body"])
        self.assertIn("CHARGER-001", sent["body"])
        self.assertIn("chargeamps", sent["body"])

    def test_success_marked_with_check(self):
        results = [("topup", "chargeamps", "CHARGER-001", True)]
        sent = self._send({"topup": make_plan("topup")}, results=results)
        self.assertIn("✓ CHARGER-001", sent["body"])

    def test_failure_marked_with_cross(self):
        results = [("topup", "chargeamps", "CHARGER-001", False)]
        sent = self._send({"topup": make_plan("topup")}, results=results)
        self.assertIn("✗ CHARGER-001", sent["body"])

    def test_delivery_section_after_windows(self):
        results = [("topup", "chargeamps", "CHARGER-001", True)]
        sent = self._send({"topup": make_plan("topup")}, results=results)
        windows_pos   = sent["body"].index("01:00")
        deliveries_pos = sent["body"].index("Deliveries:")
        self.assertGreater(deliveries_pos, windows_pos)

    def test_multiple_profiles_both_present(self):
        plans = {
            "topup":     make_plan("topup",     required=120),
            "overnight": make_plan("overnight", required=360),
        }
        sent = self._send(plans)
        self.assertIn("topup",     sent["body"])
        self.assertIn("overnight", sent["body"])

    def test_title_contains_date(self):
        sent = self._send({"topup": make_plan("topup")})
        self.assertIn("2026-03-15", sent["title"])


# ===========================================================================
# Sending behaviour
# ===========================================================================

class TestNtfySending(unittest.TestCase):

    PLAN = make_plan("topup")

    def test_disabled_does_not_send(self):
        with mock.patch("urllib.request.urlopen") as mock_open:
            _send_delivery_ntfy(
                {"topup": self.PLAN}, [], [],
                make_config(enabled=False),
            )
            mock_open.assert_not_called()

    def test_no_topic_does_not_send(self):
        with mock.patch.dict("os.environ", {"NTFY_TOPIC": ""}), \
             mock.patch("urllib.request.urlopen") as mock_open:
            _send_delivery_ntfy(
                {"topup": self.PLAN}, [], [],
                make_config(enabled=True, topic=""),
            )
            mock_open.assert_not_called()

    def test_topic_from_env_used(self):
        sent = _capture_message(
            {"topup": self.PLAN}, [], [],
            make_config(enabled=True, topic=""),
        )
        self.assertIn("test-topic", sent["url"])

    def test_topic_from_config_used(self):
        sent = {}
        def fake_urlopen(req, timeout=None):
            sent["url"] = req.full_url
            cm = mock.MagicMock()
            cm.__enter__ = lambda s: s
            cm.__exit__  = mock.Mock(return_value=False)
            return cm

        with mock.patch.dict("os.environ", {"NTFY_TOPIC": ""}), \
             mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            _send_delivery_ntfy(
                {"topup": self.PLAN}, [], [],
                make_config(enabled=True, topic="config-topic"),
            )
        self.assertIn("config-topic", sent["url"])

    def test_no_plans_does_not_send(self):
        with mock.patch.dict("os.environ", {"NTFY_TOPIC": "test-topic"}), \
             mock.patch("urllib.request.urlopen") as mock_open:
            _send_delivery_ntfy({}, [], [], make_config(enabled=True))
            mock_open.assert_not_called()

    def test_send_failure_does_not_raise(self):
        with mock.patch.dict("os.environ", {"NTFY_TOPIC": "test-topic"}), \
             mock.patch("urllib.request.urlopen", side_effect=OSError("network")):
            # Must not raise
            _send_delivery_ntfy(
                {"topup": self.PLAN}, [], [],
                make_config(enabled=True),
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
