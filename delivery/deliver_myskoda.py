#!/usr/bin/env python3
"""
MySkoda Charging Profile Delivery
====================================
Delivers a charging plan to a Skoda EV via the official MySkoda Public API
(https://public.api.connect.skoda-auto.cz).

The handler reads the current charging profile from the vehicle, updates
preferred charging time slot 4 with the planned window, disables slots 1-3,
and sets the charge mode to PREFERRED_CHARGING_TIMES.

Only compatible with plans that contain a single continuous charging window
(i.e. profiles configured with continuous_only: true). If the plan contains
multiple windows the delivery is rejected.

Tested against a real Škoda Enyaq on 2026-09-14. First delivery confirmed
correct in the MyŠkoda app. Vehicle was away from home at time of delivery
— at-home charging behaviour to be observed on subsequent runs.

Invoked by delivery/deliver.py when handler: myskoda is set inside a charging
profile's delivery entry in config.yaml.

Authentication:
    API key created in the MySkoda app (go.skoda.eu/api-keys).
    Pass via the environment variable named in api_key_env (default: SKODA_API_KEY).
    Keys expire - check X-API-Key-Expires-At in responses and rotate before
    expiry. Requests with an expired key fail with 401 api-key-expired.

Rate limit:
    20 requests per hour per VIN. Delivery uses GET + PUT profile + PUT mode = 3
    requests. Well within the limit.

config.yaml delivery entry:
    deliveries:
      - handler: myskoda
        enabled: true
        charge_point_id: SKODA_VIN       # env var holding the VIN (required)
        api_key_env: SKODA_API_KEY        # env var holding the API key (default: SKODA_API_KEY)
        profile_name: "Home"              # charging profile name to update (optional if only one profile exists)
        set_charge_mode: true             # set mode to PREFERRED_CHARGING_TIMES (default: true)

Environment variables (names configurable above):
    SKODA_VIN          Vehicle Identification Number (17 characters)
    SKODA_API_KEY      API key from the MySkoda app
"""

from __future__ import annotations

import copy
import json
import logging
import os
import urllib.error
import urllib.request
from datetime import datetime
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)

_BASE_URL = "https://public.api.connect.skoda-auto.cz"

# Index of the preferredChargingTimes slot we write into (0-based).
# All other slots are disabled but their times are preserved unchanged.
_TARGET_SLOT_INDEX = 3  # slot 4 (last of the standard 4)


# ===========================================================================
# HTTP helper
# ===========================================================================

def _request(
    path: str,
    api_key: str,
    method: str = "GET",
    body: dict | None = None,
) -> dict | None:
    """Make a single authenticated request to the MySkoda Public API.

    Returns parsed JSON for 200 responses, None for 202 (accepted, no body).
    Raises urllib.error.HTTPError on all error status codes.
    """
    url = f"{_BASE_URL}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={
            "X-API-Key":    api_key,
            "Content-Type": "application/json",
            "Accept":       "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            expires_at = resp.headers.get("X-API-Key-Expires-At", "")
            if expires_at:
                log.debug("MySkoda API key expires at %s", expires_at)
            log.debug("MySkoda rate limit remaining: %s", resp.headers.get("RateLimit-Remaining", "?"))
            raw = resp.read()
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as e:
        body_text = e.read().decode(errors="replace")
        log.error("MySkoda API %s %s -> HTTP %d: %s", method, path, e.code, body_text[:500])
        raise


# ===========================================================================
# Core helpers
# ===========================================================================

def _get_charging_profiles(vin: str, api_key: str) -> dict:
    """GET vehicle data including chargingProfiles and charging status."""
    path = f"/api/v1/vehicles/{vin}?include=chargingProfiles,charging"
    log.info("MySkoda: fetching charging profiles for VIN %s", vin)
    return _request(path, api_key)


def _find_profile(vehicle_response: dict, profile_name: str | None) -> dict:
    """Find the target charging profile.

    Matches by name (case-insensitive) if profile_name is given.
    If profile_name is omitted and there is exactly one profile, uses it directly.
    If profile_name is omitted and there are multiple profiles, raises ValueError
    asking the user to set profile_name explicitly.
    """
    profiles = (
        vehicle_response.get("vehicle", {})
        .get("chargingProfiles", {})
        .get("profiles", [])
    )
    if not profiles:
        raise ValueError(
            "No charging profiles found on vehicle. "
            "The vehicle may not support charging profiles, or they could not be "
            "retrieved — check the 'errors' list in the vehicle response."
        )

    if profile_name:
        for p in profiles:
            if p.get("name", "").lower() == profile_name.lower():
                log.info(
                    "MySkoda: found profile '%s' (id=%s)", p.get("name"), p.get("id")
                )
                return p
        available = [p.get("name", "?") for p in profiles]
        raise ValueError(
            f"Charging profile '{profile_name}' not found on vehicle. "
            f"Available profiles: {available}"
        )

    # No profile_name configured — use the single profile if there is only one,
    # otherwise require the user to set profile_name explicitly.
    if len(profiles) == 1:
        profile = profiles[0]
        log.info(
            "MySkoda: one profile found, using '%s' (id=%s)",
            profile.get("name"), profile.get("id"),
        )
        return profile

    available = [p.get("name", "?") for p in profiles]
    raise ValueError(
        f"Vehicle has {len(profiles)} charging profiles ({available}). "
        f"Set profile_name in the delivery config to specify which one to update."
    )


def _window_to_local_hhmm(utc_iso: str, tz: ZoneInfo) -> str:
    """Convert a UTC ISO timestamp to local HH:MM for the vehicle's timezone.

    The MySkoda API expects times in the vehicle's local time (ISO 8601 HH:mm).
    """
    dt_utc = datetime.fromisoformat(utc_iso.replace("Z", "+00:00"))
    return dt_utc.astimezone(tz).strftime("%H:%M")


def _build_updated_profile(profile: dict, start_hhmm: str, end_hhmm: str) -> dict:
    """Return a deep copy of the profile with our charging window applied.

    Slot _TARGET_SLOT_INDEX is set to the planned window with enabled=True.
    All other slots are set to enabled=False (times preserved unchanged).

    The full profile is always returned unchanged except for the enabled flags
    and the target slot's times. The API requires the complete profile in the
    PUT body: 'the vehicle applies the submitted profile as a whole'.
    """
    updated = copy.deepcopy(profile)

    slots = updated.get("preferredChargingTimes", [])
    if not slots:
        raise ValueError(
            "Charging profile has no preferredChargingTimes entries. "
            "Add preferred charging time slots in the MySkoda app first."
        )

    if _TARGET_SLOT_INDEX >= len(slots):
        raise ValueError(
            f"Profile has only {len(slots)} preferred charging time slot(s); "
            f"cannot write to slot {_TARGET_SLOT_INDEX + 1} (index {_TARGET_SLOT_INDEX}). "
            f"Add more preferred time slots in the MySkoda app."
        )

    for i, slot in enumerate(slots):
        if i == _TARGET_SLOT_INDEX:
            slot["enabled"]   = True
            slot["startTime"] = start_hhmm
            slot["endTime"]   = end_hhmm
            log.info(
                "MySkoda: slot %d (id=%s) set to %s-%s enabled=True",
                i + 1, slot.get("id"), start_hhmm, end_hhmm,
            )
        else:
            if slot.get("enabled"):
                log.info(
                    "MySkoda: slot %d (id=%s) disabled (was enabled)",
                    i + 1, slot.get("id"),
                )
            slot["enabled"] = False

    return updated


def _put_profile(vin: str, profile: dict, api_key: str) -> None:
    """PUT the complete updated profile to the vehicle.

    The profile id in the URL must match the id in the body — mismatch
    returns 412 Precondition Failed. Response is 202 Accepted (async).
    """
    profile_id = profile["id"]
    path = f"/api/v1/vehicles/{vin}/charging-profiles/{profile_id}"
    log.info("MySkoda: PUT charging profile id=%s to VIN %s", profile_id, vin)
    _request(path, api_key, method="PUT", body=profile)
    log.info("MySkoda: profile update accepted (202) - vehicle will apply asynchronously")


def _put_charge_mode(vin: str, api_key: str) -> None:
    """Set the charge mode to PREFERRED_CHARGING_TIMES.

    Without this the vehicle may remain in MANUAL or TIMER mode and not
    act on the preferred charging times we just set.
    Response is 202 Accepted (async).
    """
    path = f"/api/v1/vehicles/{vin}/charging/mode"
    log.info("MySkoda: setting charge mode to PREFERRED_CHARGING_TIMES")
    _request(path, api_key, method="PUT", body={"chargeMode": "PREFERRED_CHARGING_TIMES"})
    log.info("MySkoda: charge mode update accepted (202)")


# ===========================================================================
# Public entry point - called by delivery/deliver.py
# ===========================================================================

def deliver(plan: dict, vin: str, entry: dict, tz_name: str) -> bool:
    """Deliver a charging plan to a Skoda vehicle via the MySkoda Public API.

    Matches the deliver.py dispatcher convention:
        handler.deliver(plan, charge_point_id, entry, timezone)
    where charge_point_id is the VIN (resolved from the env var named by
    charge_point_id in config.yaml, e.g. SKODA_VIN).

    Args:
        plan:    Charging plan dict as produced by charging_planner.py.
        vin:     Vehicle Identification Number (from charge_point_id env var).
        entry:   Delivery config entry from config.yaml for this handler.
        tz_name: IANA timezone name from the top-level config.

    Returns True on success, False on failure (error is logged).
    """
    try:
        _deliver_inner(plan, vin, entry, tz_name)
        return True
    except Exception as exc:
        log.error(
            "Delivery failed for VIN '%s': %s", vin, exc,
            exc_info=not isinstance(exc, ValueError),
        )
        return False


def _deliver_inner(plan: dict, vin: str, entry: dict, tz_name: str) -> None:
    """Inner delivery — raises on any error."""

    # Resolve API key
    api_key_env = entry.get("api_key_env", "SKODA_API_KEY")
    api_key = os.environ.get(api_key_env, "").strip()
    if not api_key:
        raise ValueError(
            f"MySkoda delivery: API key not found in environment variable '{api_key_env}'. "
            f"Create a key at go.skoda.eu/api-keys and set {api_key_env} in the environment."
        )

    # Validate plan
    total_minutes = plan.get("total_minutes", 0)
    windows_start = plan.get("window_starts_utc", [])
    windows_end   = plan.get("window_ends_utc", [])

    if total_minutes == 0:
        raise ValueError(
            f"MySkoda delivery: plan for profile '{plan.get('profile')}' has no scheduled "
            f"minutes ({plan.get('plan_warning', 'no warning')}). Nothing to deliver."
        )

    if len(windows_start) != 1:
        raise ValueError(
            f"MySkoda delivery requires exactly one continuous charging window "
            f"(set continuous_only: true in the charging profile config). "
            f"This plan has {len(windows_start)} window(s)."
        )

    # Convert window to local HH:MM
    tz = ZoneInfo(tz_name)
    start_hhmm = _window_to_local_hhmm(windows_start[0], tz)
    end_hhmm   = _window_to_local_hhmm(windows_end[0],   tz)

    log.info(
        "MySkoda: delivering profile '%s' -> VIN %s  window %s-%s local (%s)  %d min",
        plan.get("profile"), vin, start_hhmm, end_hhmm, tz_name, total_minutes,
    )

    # Step 1: GET current charging profiles and charging state
    vehicle_response = _get_charging_profiles(vin, api_key)

    is_at_location = (
        vehicle_response.get("vehicle", {})
        .get("charging", {})
        .get("isVehicleInSavedLocation", False)
    )
    log.info("MySkoda: vehicle at saved charging location: %s", is_at_location)
    if not is_at_location:
        log.warning(
            "MySkoda: vehicle is not at a saved charging location - preferred charging "
            "times will be set but location-specific profile settings (target SoC etc.) "
            "may not be active until the vehicle arrives at the saved location."
        )

    # Step 2: Find target profile
    profile_name = entry.get("profile_name")
    profile = _find_profile(vehicle_response, profile_name)

    # Step 3: Build updated profile
    updated = _build_updated_profile(profile, start_hhmm, end_hhmm)

    # Step 4: PUT updated profile
    _put_profile(vin, updated, api_key)

    # Step 5: Set charge mode (optional, default: true)
    if entry.get("set_charge_mode", True):
        _put_charge_mode(vin, api_key)
    else:
        log.info("MySkoda: skipping charge mode update (set_charge_mode: false)")

    log.info(
        "Delivery succeeded: profile='%s'  handler='myskoda'  vin='%s'  window=%s-%s",
        plan.get("profile"), vin, start_hhmm, end_hhmm,
    )
