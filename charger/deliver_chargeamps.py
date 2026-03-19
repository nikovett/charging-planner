#!/usr/bin/env python3
"""
Charge Amps Schedule Delivery
==============================
Delivers a charging plan to a single Charge Amps charger via the
my.charge.space internal API.

Invoked by charger/deliver.py once per charger ID when handler: chargeamps
is set inside a charging profile's deliveries in config.yaml.

Environment variables:
    CHARGER_EMAIL       my.charge.space login email
    CHARGER_PASSWORD    my.charge.space login password

config.yaml delivery entry:
    deliveries:
      - handler: "chargeamps"
        charge_point_id_env: "CHARGER_ID_1"   # single ID or a list
        connector_id: 1                        # default 1
        max_charging_rate: 16.0               # amps — default 16.0 A
"""

from __future__ import annotations

import json
import logging
import os
import random
import string
import time
import urllib.request
import urllib.error
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
_CA_BASE = "https://my.charge.space/api"

# Module-level token cache — login once per process, reuse across deliver() calls
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
# Charge Amps API
# ===========================================================================

def _ca_request(
    path: str,
    method: str = "GET",
    body: dict | None = None,
    token: str = "",
    entitlements_token: str = "",
) -> dict | list:
    url = f"{_CA_BASE}{path}"
    data = json.dumps(body).encode() if body is not None else None
    headers = {
        "accept":       "*/*",
        "content-type": "application/json",
    }
    if token:
        headers["authorization"] = f"Bearer {token}"
    if entitlements_token:
        headers["x-entitlements-token"] = f"Bearer {entitlements_token}"

    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    raw = _http_request_with_retry(req, label=f"CA {method} {path}")
    return json.loads(raw)


def _ca_login() -> tuple[str, str]:
    """Authenticate and return (token, entitlements_token).

    Caches the result in _token_cache so multiple deliver() calls within the
    same process only log in once.
    """
    if "token" in _token_cache and "ent_token" in _token_cache:
        return _token_cache["token"], _token_cache["ent_token"]

    email    = os.environ.get("CHARGER_EMAIL", "")
    password = os.environ.get("CHARGER_PASSWORD", "")
    if not email or not password:
        raise RuntimeError(
            "CHARGER_EMAIL and CHARGER_PASSWORD environment variables must be set."
        )

    resp = _ca_request(
        "/auth/login",
        method="POST",
        body={"email": email, "password": password, "hostName": "my.charge.space"},
    )
    token = resp.get("token", "")
    if not token:
        raise RuntimeError(f"Charge Amps login failed — no token in response: {resp}")
    log.info("Charge Amps login OK")

    claims = (resp.get("user") or {}).get("claims", [])
    org_id = ""
    for claim in claims:
        if ":" in claim:
            org_id = claim.split(":")[0]
            break
    if not org_id:
        raise RuntimeError(f"Could not extract organizationId from claims: {claims}")

    ent_resp = _ca_request(
        "/entitlements/context",
        method="POST",
        body={"organizationId": org_id},
        token=token,
    )
    entitlements_token = ent_resp.get("token", "")
    if not entitlements_token:
        raise RuntimeError(f"No entitlements token in response: {ent_resp}")
    log.info("Charge Amps entitlements token OK (org: %s)", org_id)

    _token_cache["token"]     = token
    _token_cache["ent_token"] = entitlements_token
    return token, entitlements_token


_NANOID_CHARS = string.ascii_letters + string.digits


def _nanoid(size: int = 21) -> str:
    return "".join(random.choices(_NANOID_CHARS, k=size))


def _ca_build_periods(
    plan: dict,
    tz_name: str,
    max_charging_rate: float,
) -> tuple[list[dict], str]:
    """Convert plan windows → Charge Amps schedulePeriods.

    max_charging_rate is in amps — the Charge Amps API uses current (A),
    not power (W).
    """
    window_starts = plan.get("window_starts_utc", [])
    window_ends   = plan.get("window_ends_utc", [])

    if not window_starts:
        return [], ""

    zone        = ZoneInfo(tz_name)
    first_start = datetime.fromisoformat(window_starts[0].replace("Z", "+00:00"))
    local_start = first_start.astimezone(zone)

    days_since_monday = local_start.isoweekday() - 1
    monday_local = (local_start - timedelta(days=days_since_monday)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    anchor_utc = monday_local.astimezone(timezone.utc)
    anchor_iso = anchor_utc.strftime("%Y-%m-%dT%H:%M:%SZ")

    periods = []
    for start_iso, end_iso in zip(window_starts, window_ends):
        start_utc = datetime.fromisoformat(start_iso.replace("Z", "+00:00"))
        end_utc   = datetime.fromisoformat(end_iso.replace("Z", "+00:00"))
        periods.append({
            "id":         _nanoid(),
            "from":       int((start_utc - anchor_utc).total_seconds()),
            "to":         int((end_utc   - anchor_utc).total_seconds()),
            "maxCurrent": max_charging_rate,
        })

    log.debug("Anchor: %s  periods: %s", anchor_iso, periods)
    return periods, anchor_iso


def _ca_get_chargepoint(
    charge_point_id: str,
    token: str,
    entitlements_token: str,
) -> dict:
    """Fetch the full chargepoint object including connector state."""
    return _ca_request(
        f"/chargepoints/{charge_point_id}"
        "?expand=additionalInfo,scheduleInfo,wifisignal,topChargingLimitation",
        method="GET",
        token=token,
        entitlements_token=entitlements_token,
    )


def _ca_get_connector_mode(chargepoint: dict, connector_id: int) -> str | None:
    """Extract the current mode for a connector from a chargepoint dict.

    Returns the mode string ("On", "Off", "Schedule") or None if not found.
    """
    connectors = chargepoint.get("connectors", [])
    for c in connectors:
        if c.get("connectorId") == connector_id:
            return c.get("mode")
    return None


def _ca_set_connector_mode(
    chargepoint: dict,
    connector_id: int,
    mode: str,
    token: str,
    entitlements_token: str,
) -> None:
    """Set a connector's mode by PUTting the full chargepoint object back with
    the mode field updated on the target connector.

    The API requires the full chargepoint object — only the connector mode
    field is changed, everything else is sent back as-is.
    """
    import copy
    payload = copy.deepcopy(chargepoint)
    for c in payload.get("connectors", []):
        if c.get("connectorId") == connector_id:
            c["mode"] = mode
            break

    _ca_request(
        "/chargepoints",
        method="PUT",
        body=payload,
        token=token,
        entitlements_token=entitlements_token,
    )
    log.info(
        "Connector mode restored: charger=%s connector=%s  mode=%s",
        chargepoint.get("id", "?"), connector_id, mode,
    )


def _ca_put_schedule(
    plan: dict,
    charge_point_id: str,
    connector_id: int,
    tz_name: str,
    max_charging_rate: float,
    token: str,
    entitlements_token: str,
) -> None:
    periods, anchor_iso = _ca_build_periods(plan, tz_name, max_charging_rate)
    if not periods:
        raise ValueError("No valid periods generated from plan windows.")

    # A blind PUT replaces the entire weekly schedule with only the new windows —
    # any periods added manually via the web interface will be cleared.
    # The web app avoids this by GET-ing the existing schedule first and merging.
    # For automated daily delivery this is intentional: we own the full schedule
    # state and avoid conflicts with stale or overlapping manual entries.

    payload = {
        "scheduleId":      0,
        "chargePointId":   charge_point_id,
        "connectorId":     connector_id,
        "validFrom":       None,
        "validTo":         None,
        "defaultCurrent":  0.0,
        "schedulePeriods": periods,
        "isActive":        True,
        "isSynced":        False,
        "timeZone":        tz_name,
        "startOfSchedule": anchor_iso,
    }

    _ca_request(
        "/smartChargingSchedules",
        method="PUT",
        body=payload,
        token=token,
        entitlements_token=entitlements_token,
    )

    windows_str = ", ".join(
        f"{s}–{e}"
        for s, e in zip(plan["window_starts_utc"], plan["window_ends_utc"])
    )
    log.info(
        "Delivered: charger=%s connector=%s  rate=%gA  windows=%s",
        charge_point_id, connector_id, max_charging_rate, windows_str,
    )


# ===========================================================================
# Public interface — called by charger/deliver.py
# ===========================================================================

def deliver(plan: dict, charge_point_id: str, entry: dict, timezone: str) -> bool:
    """Deliver a plan to a single Charge Amps charger.

    Reads the connector mode before delivering the schedule and restores it
    afterwards if it was not already "Schedule". This prevents the delivery
    from silently switching the charger into schedule mode when the user had
    it set to "On" or "Off".

    Args:
        plan:             Plan dict as produced by charging_planner.py.
        charge_point_id:  Resolved charger ID (already read from env by dispatcher).
        entry:            The delivery config entry from config.yaml, containing
                          connector_id, max_charging_rate, etc.
        timezone:         IANA timezone name inherited from the charging profile.

    Returns True on success, False on failure.
    """
    connector_id      = int(entry.get("connector_id", 1))
    max_charging_rate = float(entry.get("max_charging_rate", 16.0))

    try:
        token, ent_token = _ca_login()
    except Exception as exc:
        log.error("Charge Amps login failed: %s", exc)
        return False

    # Read current connector mode before touching the schedule
    try:
        chargepoint   = _ca_get_chargepoint(charge_point_id, token, ent_token)
        original_mode = _ca_get_connector_mode(chargepoint, connector_id)
        log.info(
            "Connector mode before delivery: charger=%s connector=%s  mode=%s",
            charge_point_id, connector_id, original_mode,
        )
    except Exception as exc:
        log.error("Failed to read charger state for '%s': %s", charge_point_id, exc)
        return False

    # Deliver the schedule
    try:
        _ca_put_schedule(
            plan, charge_point_id, connector_id,
            timezone, max_charging_rate, token, ent_token,
        )
    except Exception as exc:
        log.error("Delivery failed for charger '%s': %s", charge_point_id, exc)
        return False

    # Restore original mode if it was not already "Schedule"
    if original_mode and original_mode != "Schedule":
        try:
            _ca_set_connector_mode(
                chargepoint, connector_id, original_mode, token, ent_token,
            )
        except Exception as exc:
            log.warning(
                "Schedule delivered but mode restore failed for charger '%s': %s",
                charge_point_id, exc,
            )
            # Schedule was delivered successfully — still return True
    else:
        log.info(
            "Mode already 'Schedule' for charger=%s connector=%s — no restore needed.",
            charge_point_id, connector_id,
        )

    return True
