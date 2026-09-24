#!/usr/bin/env python3
"""
MySkoda Charging Profile Delivery
====================================
Delivers a charging plan to a Skoda EV via the official MySkoda Public API
(https://public.api.connect.skoda-auto.cz).

The handler reads the current charging profile from the vehicle, writes each
plan window into a preferred charging time slot (window 1 -> slot 1, window
2 -> slot 2, and so on), disables any unused slots, and sets the charge mode
to PREFERRED_CHARGING_TIMES.

If the vehicle is actively charging in PREFERRED_CHARGING_TIMES mode, the
handler detects which slot is driving the current session (by checking which
enabled slot's time window contains the current local time — the MySkoda API
does not report this directly) and routes the plan's windows around it,
leaving that one slot completely untouched. If the active slot can't be
identified with confidence (no unique match), or there isn't enough room to
route around it (the plan needs all 4 slots), delivery is skipped entirely
for that run rather than risk interrupting the session.

Compatible with plans that contain 1 to 4 charging windows — the vehicle has
exactly 4 preferredChargingTimes slots, so plans with more than 4 windows are
rejected. Configure the charging profile's max_windows to 4 or fewer (never
null/unlimited) to guarantee this holds; the handler also checks the actual
window count of each plan as a defensive runtime check.

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
        charge_point_id: SKODA_VIN       # env var holding the VIN (required)
        api_key_env: SKODA_API_KEY        # env var holding the API key (default: SKODA_API_KEY)
        profile_name: "Home"              # charging profile name to update (optional if only one profile exists)
        set_charge_mode: PREFERRED_CHARGING_TIMES  # charge mode to set after profile update, or false to skip

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

# The vehicle has exactly 4 preferredChargingTimes slots (indices 0-3).
# Plan windows are written starting from slot 1 (index 0) onwards; any slots
# beyond the number of plan windows are disabled.
_MAX_VEHICLE_SLOTS = 4


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


def _hhmm_to_minutes(hhmm: str) -> int:
    h, m = map(int, hhmm.split(":"))
    return h * 60 + m


def _time_in_window(now_hhmm: str, start_hhmm: str, end_hhmm: str) -> bool:
    """True if now_hhmm falls within [start_hhmm, end_hhmm), local time-of-day.

    Handles overnight windows (start > end, e.g. 22:00-06:00) by wrapping
    around midnight. A zero-length window (start == end) never matches.
    Half-open interval: matches at exactly start_hhmm, not at exactly
    end_hhmm — consistent with charging stopping right at the window end.
    """
    now, start, end = (_hhmm_to_minutes(x) for x in (now_hhmm, start_hhmm, end_hhmm))
    if start == end:
        return False
    if start < end:
        return start <= now < end
    return now >= start or now < end


def _find_active_slot_index(slots: list[dict], now_hhmm: str) -> int | None:
    """Return the index of the single enabled slot whose time window contains
    now_hhmm, or None if zero or more than one slot matches.

    The MySkoda API does not report which preferredChargingTimes slot is
    actually driving an active PREFERRED_CHARGING_TIMES session — this is
    inferred from time overlap instead. A non-unique result (no match, or
    more than one enabled slot's window contains now) is genuinely ambiguous
    and callers should treat it as "unknown" rather than guess.
    """
    matches = [
        i for i, s in enumerate(slots)
        if s.get("enabled") and _time_in_window(now_hhmm, s.get("startTime", ""), s.get("endTime", ""))
    ]
    return matches[0] if len(matches) == 1 else None


def _window_to_local_hhmm(utc_iso: str, tz: ZoneInfo) -> str:
    """Convert a UTC ISO timestamp to local HH:MM for the vehicle's timezone.

    The MySkoda API expects times in the vehicle's local time (ISO 8601 HH:mm).
    """
    dt_utc = datetime.fromisoformat(utc_iso.replace("Z", "+00:00"))
    return dt_utc.astimezone(tz).strftime("%H:%M")


def _build_updated_profile(
    profile: dict,
    windows_hhmm: list[tuple[str, str]],
    target_slot_indices: list[int] | None = None,
    protected_slot_index: int | None = None,
) -> dict:
    """Return a deep copy of the profile with our charging window(s) applied.

    windows_hhmm is an ordered list of (start_hhmm, end_hhmm) tuples, one per
    plan window. target_slot_indices gives the slot index each window is
    written to, in the same order (default: positional, 0..len(windows_hhmm)-1
    — window 1 -> slot 1, window 2 -> slot 2, ...).

    protected_slot_index, if given, is left completely untouched — no enable/
    disable, no time change — regardless of whether it appears in
    target_slot_indices (it never should; callers route around it). This is
    used when the vehicle is actively charging in PREFERRED_CHARGING_TIMES
    mode via that specific slot: we know precisely which one it is, so we can
    manage every other slot normally instead of blanket-preserving all of
    them "just in case."

    Every slot that is neither a target nor protected is disabled (its time
    is irrelevant once disabled, so it's left as-is).

    The full profile is always returned — the API requires the complete profile
    in the PUT body: 'the vehicle applies the submitted profile as a whole'.
    """
    updated = copy.deepcopy(profile)

    slots = updated.get("preferredChargingTimes", [])
    if not slots:
        raise ValueError(
            "Charging profile has no preferredChargingTimes entries. "
            "Add preferred charging time slots in the MySkoda app first."
        )

    if target_slot_indices is None:
        target_slot_indices = list(range(len(windows_hhmm)))

    if len(target_slot_indices) != len(windows_hhmm):
        raise ValueError(
            f"target_slot_indices has {len(target_slot_indices)} entries but "
            f"windows_hhmm has {len(windows_hhmm)} — must match one-to-one."
        )

    max_target = max(target_slot_indices, default=-1)
    if max_target >= len(slots):
        raise ValueError(
            f"Plan requires slot {max_target + 1} but the profile has only "
            f"{len(slots)} preferred charging time slot(s). "
            f"Add more preferred time slots in the MySkoda app, or reduce "
            f"max_windows in the charging profile config."
        )

    window_by_slot = dict(zip(target_slot_indices, windows_hhmm))

    for i, slot in enumerate(slots):
        if i == protected_slot_index:
            log.info(
                "MySkoda: slot %d (id=%s) protected — actively driving the "
                "current charging session, left untouched",
                i + 1, slot.get("id"),
            )
        elif i in window_by_slot:
            start_hhmm, end_hhmm = window_by_slot[i]
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


def _put_charge_mode(vin: str, api_key: str, mode: str) -> None:
    """Set the vehicle charge mode.

    mode is any valid ChargeMode value from the MySkoda Public API:
      MANUAL, TIMER, TIMER_CHARGING_WITH_CLIMATISATION, PREFERRED_CHARGING_TIMES,
      ONLY_OWN_CURRENT, IMMEDIATE_DISCHARGING, HOME_STORAGE_CHARGING.
    New values may be added by Skoda over time and are passed through as-is.
    Response is 202 Accepted (async).
    """
    path = f"/api/v1/vehicles/{vin}/charging/mode"
    log.info("MySkoda: setting charge mode to %s", mode)
    _request(path, api_key, method="PUT", body={"chargeMode": mode})
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

    plan is the charging plan dict as produced by charging_planner.py. vin is
    the Vehicle Identification Number (from the charge_point_id env var).
    entry is the delivery config entry from config.yaml for this handler.
    tz_name is the IANA timezone name from the top-level config.

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

    if len(windows_start) == 0:
        raise ValueError(
            f"MySkoda delivery: plan for profile '{plan.get('profile')}' has no "
            f"charging windows to deliver."
        )

    # Config-level check: the charging profile's max_windows must be set to a
    # bounded value that fits the vehicle's 4 preferredChargingTimes slots.
    # max_windows is written into the plan JSON by charging_planner.py.
    # None (unbounded) is rejected even if today's plan happens to produce
    # <= 4 windows — an unbounded profile can produce more than 4 windows on
    # a different day and fail unpredictably at delivery time.
    max_windows = plan.get("max_windows")
    if max_windows is None or max_windows > _MAX_VEHICLE_SLOTS:
        raise ValueError(
            f"MySkoda delivery requires the charging profile's max_windows to be "
            f"set to a value between 1 and {_MAX_VEHICLE_SLOTS} "
            f"(got {max_windows!r}) — the vehicle has {_MAX_VEHICLE_SLOTS} "
            f"preferred charging time slots. Set max_windows in the charging "
            f"profile config."
        )

    # Defensive runtime check: the plan's actual window count must also fit,
    # independent of the configured max_windows (belt-and-braces in case the
    # two ever drift, e.g. an older plan.json generated before this check).
    if len(windows_start) > _MAX_VEHICLE_SLOTS:
        raise ValueError(
            f"MySkoda delivery requires at most {_MAX_VEHICLE_SLOTS} charging "
            f"windows (the vehicle has {_MAX_VEHICLE_SLOTS} preferred charging "
            f"time slots). This plan has {len(windows_start)} window(s)."
        )

    # Convert windows to local HH:MM, in plan order (window 1 -> slot 1, etc.)
    tz = ZoneInfo(tz_name)
    windows_hhmm = [
        (_window_to_local_hhmm(s, tz), _window_to_local_hhmm(e, tz))
        for s, e in zip(windows_start, windows_end)
    ]

    log.info(
        "MySkoda: delivering profile '%s' -> VIN %s  %d window(s): %s  (%s)  %d min",
        plan.get("profile"), vin, len(windows_hhmm),
        ", ".join(f"{s}-{e}" for s, e in windows_hhmm), tz_name, total_minutes,
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
            "times and charge mode may not take effect until the vehicle arrives at the "
            "saved location. The API accepts the update (202) but the vehicle may ignore "
            "or revert it when away from home."
        )

    # Step 2: Find target profile
    profile_name = entry.get("profile_name")
    profile = _find_profile(vehicle_response, profile_name)

    # Step 3: Determine delivery behaviour based on charging state and mode.
    #
    # charging.status.state == CHARGING means the car is actively charging.
    # The safe action depends on the active charge mode (charging.settings.preferredChargeMode):
    #
    #   MANUAL / TIMER / TIMER_CHARGING_WITH_CLIMATISATION:
    #     These modes drive the current session directly — not via a
    #     preferredChargingTimes slot. Safe to update slots 1..N with the
    #     plan windows and disable any unused slots.
    #     Do NOT change the charge mode — don't interrupt the current session logic.
    #
    #   PREFERRED_CHARGING_TIMES:
    #     One specific slot is driving this session, but the API doesn't say
    #     which one — inferred by checking which enabled slot's time window
    #     contains the current local time (charging stops at window end, so
    #     this is a reliable signal, not a guess). If exactly one slot
    #     matches, plan windows are routed around it (that one slot is left
    #     completely untouched; every other slot — including ones outside
    #     the plan's usual 1..N range — is managed normally). If the active
    #     slot can't be identified, or the plan needs all 4 slots so there's
    #     no room to route around it, delivery is skipped entirely rather
    #     than risk touching the active slot.
    #     Do NOT change the charge mode.
    #
    #   Unknown mode while charging:
    #     Skip delivery entirely — safer than guessing.
    #
    # If not charging: full delivery — update slots 1..N, disable unused
    # slots, set mode.

    _SAFE_CHARGING_MODES = {"MANUAL", "TIMER", "TIMER_CHARGING_WITH_CLIMATISATION",
                            "PREFERRED_CHARGING_TIMES"}

    charging_obj      = vehicle_response.get("vehicle", {}).get("charging", {})
    charging_state    = charging_obj.get("status", {}).get("state", "")
    active_mode       = charging_obj.get("settings", {}).get("preferredChargeMode", "")
    is_charging       = charging_state == "CHARGING"
    target_indices    = None  # None = positional default (0..N-1)
    protected_index   = None
    skip_mode_change  = False

    if is_charging:
        if active_mode not in _SAFE_CHARGING_MODES:
            log.warning(
                "MySkoda: vehicle is charging in unknown mode '%s' — skipping delivery "
                "to avoid interrupting the active session.", active_mode,
            )
            return
        skip_mode_change = True
        if active_mode == "PREFERRED_CHARGING_TIMES":
            profile_slots = profile.get("preferredChargingTimes", [])
            now_hhmm = datetime.now(tz).strftime("%H:%M")
            active_idx = _find_active_slot_index(profile_slots, now_hhmm)

            if active_idx is None:
                log.warning(
                    "MySkoda: vehicle is charging in PREFERRED_CHARGING_TIMES mode but "
                    "the active slot could not be identified (no unique time-window "
                    "match at %s local) — skipping delivery to avoid interrupting the "
                    "active session.", now_hhmm,
                )
                return

            available = [i for i in range(len(profile_slots)) if i != active_idx]
            if len(windows_hhmm) > len(available):
                log.warning(
                    "MySkoda: vehicle is charging via slot %d (active at %s local) — "
                    "plan needs %d window(s) but only %d slot(s) remain available "
                    "with slot %d protected. Skipping delivery to avoid interrupting "
                    "the active session.",
                    active_idx + 1, now_hhmm, len(windows_hhmm), len(available),
                    active_idx + 1,
                )
                return

            target_indices  = available[:len(windows_hhmm)]
            protected_index = active_idx
            log.info(
                "MySkoda: vehicle is charging via slot %d (active at %s local) — "
                "routing plan window(s) to slot(s) %s, leaving slot %d untouched.",
                active_idx + 1, now_hhmm,
                ", ".join(str(i + 1) for i in target_indices), active_idx + 1,
            )
        else:
            log.info(
                "MySkoda: vehicle is charging in %s mode — updating slots 1-%d and "
                "disabling unused slots, but not changing charge mode.",
                active_mode, len(windows_hhmm),
            )

    # Step 4: Build updated profile
    updated = _build_updated_profile(profile, windows_hhmm,
                                     target_slot_indices=target_indices,
                                     protected_slot_index=protected_index)

    # Step 5: PUT updated profile
    _put_profile(vin, updated, api_key)

    # Step 6: Set charge mode (optional).
    # set_charge_mode accepts a charge mode string (e.g. PREFERRED_CHARGING_TIMES)
    # or false/omitted to skip. Default: PREFERRED_CHARGING_TIMES.
    # Valid modes per MySkoda Public API v1.0.0: MANUAL, TIMER,
    # TIMER_CHARGING_WITH_CLIMATISATION, PREFERRED_CHARGING_TIMES,
    # ONLY_OWN_CURRENT, IMMEDIATE_DISCHARGING, HOME_STORAGE_CHARGING.
    # New values added by Skoda are passed through as-is.
    # Skipped when vehicle is actively charging to avoid interrupting the session.
    charge_mode_cfg = entry.get("set_charge_mode", "PREFERRED_CHARGING_TIMES")
    if skip_mode_change:
        log.info("MySkoda: skipping charge mode update — vehicle is actively charging.")
    elif charge_mode_cfg and charge_mode_cfg is not False:
        mode = charge_mode_cfg if isinstance(charge_mode_cfg, str) else "PREFERRED_CHARGING_TIMES"
        _put_charge_mode(vin, api_key, mode)
    else:
        log.info("MySkoda: skipping charge mode update (set_charge_mode: false)")

    log.info(
        "Delivery succeeded: profile='%s'  handler='myskoda'  vin='%s'  windows=%s",
        plan.get("profile"), vin,
        ", ".join(f"{s}-{e}" for s, e in windows_hhmm),
    )
