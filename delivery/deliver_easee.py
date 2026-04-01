#!/usr/bin/env python3
"""
Easee Schedule Delivery
========================
Delivers a charging plan to a single Easee charger via the official
Easee REST API (api.easee.com).

Invoked by delivery/deliver.py once per charger ID when handler: easee
is set inside a charging profile's deliveries in config.yaml.

Environment variables:
    CHARGER_EMAIL       Easee account username (email or phone with country code)
    CHARGER_PASSWORD    Easee account password

config.yaml delivery entry:
    deliveries:
      - handler: easee
        charge_point_id: CHARGER_ID_1       # single ID or a list
        max_charging_rate: 16               # amps — integer, default 16

Single charging window → Basic Charge Plan (specific datetime, no repeat).
Multiple windows (split plan) → Weekly Charge Plan for the target day.

Note: This handler has not been tested against a real Easee charger.
The API endpoints and payload formats are based on official Easee
developer documentation at https://developer.easee.com/docs/scheduling.
"""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone as _tz

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
_EASEE_BASE = "https://api.easee.com/api"

# Module-level token cache — login once per process, reuse across deliver() calls.
_token_cache: dict[str, str] = {}


# ===========================================================================
# HTTP helper
# ===========================================================================

def _http_request_with_retry(
    req: urllib.request.Request,
    timeout: int = 15,
    retries: int = 3,
    backoff: float = 2.0,
    label: str = "",
) -> str:
    last_exc: Exception = RuntimeError("No attempts made")
    for attempt in range(1, retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read().decode()
        except urllib.error.HTTPError as e:
            if 400 <= e.code < 500:
                body = e.read().decode()
                log.error("%s HTTP %s: %s", label or req.full_url, e.code, body[:500])
                raise
            body = e.read().decode()
            last_exc = e
            log.warning("%s HTTP %s (attempt %d/%d): %s",
                        label or req.full_url, e.code, attempt, retries, body[:200])
        except urllib.error.URLError as e:
            last_exc = e
            log.warning("%s network error (attempt %d/%d): %s",
                        label or req.full_url, attempt, retries, e.reason)
        except OSError as e:
            last_exc = e
            log.warning("%s OS error (attempt %d/%d): %s",
                        label or req.full_url, attempt, retries, e)

        if attempt < retries:
            wait = backoff ** (attempt - 1)
            log.info("Retrying in %.0f s…", wait)
            time.sleep(wait)

    raise last_exc


# ===========================================================================
# Easee API
# ===========================================================================

def _easee_request(
    path: str,
    method: str = "GET",
    body: dict | None = None,
    token: str = "",
) -> dict | list:
    url = f"{_EASEE_BASE}{path}"
    data = json.dumps(body).encode() if body is not None else None
    headers = {
        "accept":       "application/json",
        "content-type": "application/json",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"

    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    raw = _http_request_with_retry(req, label=f"Easee {method} {path}")
    return json.loads(raw) if raw.strip() else {}


def _easee_login() -> str:
    """Authenticate and return access token.

    Caches the result so multiple deliver() calls within the same process
    only log in once. Token is valid for 1 hour — sufficient for a single
    daily GHA run.
    """
    if "token" in _token_cache:
        return _token_cache["token"]

    username = os.environ.get("CHARGER_EMAIL", "")
    password = os.environ.get("CHARGER_PASSWORD", "")
    if not username or not password:
        raise RuntimeError(
            "CHARGER_EMAIL and CHARGER_PASSWORD environment variables must be set."
        )

    resp = _easee_request(
        "/accounts/login",
        method="POST",
        body={"userName": username, "password": password},
    )
    token = resp.get("accessToken", "")
    if not token:
        raise RuntimeError(f"Easee login failed — no accessToken in response: {resp}")
    log.info("Easee login OK")

    _token_cache["token"] = token
    return token


def _easee_put_basic_plan(
    charger_id: str,
    start_utc: datetime,
    stop_utc: datetime,
    token: str,
) -> None:
    """Deliver a single charging window using the Basic Charge Plan API.

    Uses repeat=false so the plan fires once for the specific datetime,
    not recurring daily.
    """
    payload = {
        "id":              charger_id,
        "chargeStartTime": start_utc.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        "chargeStopTime":  stop_utc.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        "repeat":          False,
        "isEnabled":       True,
    }
    _easee_request(
        f"/chargers/{charger_id}/basic_charge_plan",
        method="POST",
        body=payload,
        token=token,
    )
    log.info(
        "Delivered basic plan: charger=%s  %s → %s",
        charger_id,
        start_utc.strftime("%Y-%m-%d %H:%M UTC"),
        stop_utc.strftime("%H:%M UTC"),
    )


def _easee_put_weekly_plan(
    charger_id: str,
    windows: list[tuple[datetime, datetime]],
    max_charging_rate: int,
    token: str,
) -> None:
    """Deliver multiple charging windows using the Weekly Charge Plan API.

    Maps each window to the correct day of the week (0=Monday). Clears all
    other days. The weekly plan uses UTC times in "HH:MMZ" format.

    Limitation: the weekly plan is recurring — it will repeat the same
    windows every week until changed. For a daily-changing split plan this
    is less ideal than the basic plan, but it is the only API that supports
    multiple windows. The next day's run will overwrite it.

    Also note: the weekly plan supports at most 2 ranges per day.
    """
    # Build a full 7-day structure with all days empty
    days: list[dict] = [{"dayOfWeek": d, "ranges": []} for d in range(7)]

    for start_utc, stop_utc in windows:
        dow = start_utc.isoweekday() % 7  # Monday=0 … Sunday=6
        start_str = start_utc.strftime("%H:%MZ")
        stop_str  = stop_utc.strftime("%H:%MZ")
        days[dow]["ranges"].append({
            "startTime":           start_str,
            "stopTime":            stop_str,
            "chargingCurrentLimit": max_charging_rate,
        })
        log.debug("Weekly plan day %d: %s → %s", dow, start_str, stop_str)

    payload = {"isEnabled": True, "days": days}
    _easee_request(
        f"/chargers/{charger_id}/weekly_charge_plan",
        method="POST",
        body=payload,
        token=token,
    )

    windows_str = ", ".join(
        f"{s.strftime('%H:%M')}–{e.strftime('%H:%M')} UTC"
        for s, e in windows
    )
    log.info("Delivered weekly plan: charger=%s  windows=%s", charger_id, windows_str)


# ===========================================================================
# Public interface — called by delivery/deliver.py
# ===========================================================================

def deliver(plan: dict, charge_point_id: str, entry: dict, timezone: str) -> bool:
    """Deliver a plan to a single Easee charger.

    Single window → Basic Charge Plan (specific datetime, no repeat).
    Multiple windows → Weekly Charge Plan (recurring, overwritten daily).

    Args:
        plan:             Plan dict as produced by charging_planner.py.
        charge_point_id:  Resolved charger ID (already read from env by dispatcher).
        entry:            The delivery config entry from config.yaml.
        timezone:         IANA timezone name (unused — Easee API uses UTC).

    Returns True on success, False on failure.
    """
    max_charging_rate = int(entry.get("max_charging_rate", 16))

    window_starts = plan.get("window_starts_utc", [])
    window_ends   = plan.get("window_ends_utc", [])

    if not window_starts:
        log.warning("Easee: no charging windows in plan for charger '%s' — skipping.", charge_point_id)
        return True  # Empty plan is not a delivery failure

    windows = [
        (
            datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(_tz.utc),
            datetime.fromisoformat(e.replace("Z", "+00:00")).astimezone(_tz.utc),
        )
        for s, e in zip(window_starts, window_ends)
    ]

    try:
        token = _easee_login()
    except Exception as exc:
        log.error("Easee login failed: %s", exc)
        return False

    try:
        if len(windows) == 1:
            _easee_put_basic_plan(charge_point_id, windows[0][0], windows[0][1], token)
        else:
            _easee_put_weekly_plan(charge_point_id, windows, max_charging_rate, token)
    except Exception as exc:
        log.error("Easee delivery failed for charger '%s': %s", charge_point_id, exc)
        return False

    return True
