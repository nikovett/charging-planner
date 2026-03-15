#!/usr/bin/env python3
"""
Charge Amps Schedule Delivery
==============================
Reads one or more plan JSON files produced by charging_planner.py and delivers
the charging schedule to Charge Amps via the my.charge.space internal API.

Usage:
    python deliver_chargeamps.py plan-topup.json plan-overnight.json
    python deliver_chargeamps.py plan-*.json
    python deliver_chargeamps.py --config chargeamps.yaml plan-*.json

Each delivery entry in chargeamps.yaml maps a profile name to a charger.
Credentials and charger IDs are injected via environment variables so no
secrets are ever committed to the repository.

Environment variables:
    CHARGEAMPS_EMAIL       my.charge.space login email
    CHARGEAMPS_PASSWORD    my.charge.space login password
    <charge_point_id_env>  per-delivery env var named in chargeamps.yaml
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import random
import string
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

try:
    import yaml
except ImportError:
    yaml = None

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
_CA_BASE = "https://my.charge.space/api"

DEFAULT_CONFIG = {
    "deliveries": [],
}


# ===========================================================================
# Config
# ===========================================================================

def load_config(path: str) -> dict:
    """Load chargeamps.yaml."""
    if yaml is None:
        log.error("PyYAML is not installed. Run: pip install pyyaml")
        sys.exit(1)
    if not os.path.exists(path):
        log.error("Config file not found: %s", path)
        sys.exit(1)
    with open(path) as f:
        cfg = yaml.safe_load(f) or {}
    if "deliveries" not in cfg:
        log.error("chargeamps.yaml must contain a 'deliveries' list.")
        sys.exit(1)
    return cfg


# ===========================================================================
# HTTP helper (shared with charging_planner style)
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
    """Make a single authenticated request to the Charge Amps internal API."""
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


def ca_login(email: str, password: str) -> tuple[str, str]:
    """Authenticate and return (token, entitlements_token).

    Two-step flow:
      1. POST /api/auth/login -> bearer token + organizationId from user.claims
      2. POST /api/entitlements/context -> entitlements token
    """
    # Step 1 — login
    resp = _ca_request(
        "/auth/login",
        method="POST",
        body={"email": email, "password": password, "hostName": "my.charge.space"},
    )
    token = resp.get("token", "")
    if not token:
        raise RuntimeError(f"Charge Amps login failed — no token in response: {resp}")
    log.info("Charge Amps login OK")

    # Step 2 — extract organizationId from user.claims (format: "<uuid>:Role")
    claims = (resp.get("user") or {}).get("claims", [])
    org_id = ""
    for claim in claims:
        if ":" in claim:
            org_id = claim.split(":")[0]
            break
    if not org_id:
        raise RuntimeError(f"Could not extract organizationId from claims: {claims}")

    # Step 3 — fetch entitlements token
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

    return token, entitlements_token


_NANOID_CHARS = string.ascii_letters + string.digits


def _nanoid(size: int = 21) -> str:
    """Generate a random nanoid-style ID matching the format used by the web app."""
    return "".join(random.choices(_NANOID_CHARS, k=size))


def ca_build_periods(
    plan: dict,
    tz_name: str,
    max_current: float,
) -> tuple[list[dict], str]:
    """Convert plan windows -> Charge Amps schedulePeriods.

    The Charge Amps schedule is anchored to Monday 00:00 local time (expressed
    as UTC), matching the weekly calendar the web app displays and sends.
    Returns (periods, startOfSchedule_iso).
    """
    window_starts = plan.get("window_starts_utc", [])
    window_ends   = plan.get("window_ends_utc", [])

    if not window_starts:
        return [], ""

    # Anchor = most recent Monday 00:00 local time, expressed as UTC
    zone = ZoneInfo(tz_name)
    first_start = datetime.fromisoformat(window_starts[0].replace("Z", "+00:00"))
    local_start = first_start.astimezone(zone)
    # isoweekday(): Mon=1 ... Sun=7; days since Monday = isoweekday() - 1
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
        from_sec  = int((start_utc - anchor_utc).total_seconds())
        to_sec    = int((end_utc   - anchor_utc).total_seconds())
        periods.append({
            "id":         _nanoid(),
            "from":       from_sec,
            "to":         to_sec,
            "maxCurrent": max_current,
        })

    log.debug("Anchor: %s  periods: %s", anchor_iso, periods)
    return periods, anchor_iso



def ca_put_schedule(
    plan: dict,
    charge_point_id: str,
    connector_id: int,
    tz_name: str,
    max_current: float,
    token: str,
    entitlements_token: str,
) -> None:
    """Build and PUT the updated schedule.

    The server assigns a new scheduleId on every PUT regardless of the value
    sent, so we use 0. No prior GET is needed.
    """
    periods, anchor_iso = ca_build_periods(plan, tz_name, max_current)
    if not periods:
        raise ValueError("No valid periods generated from plan windows.")

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
        f"{s}-{e}"
        for s, e in zip(plan["window_starts_utc"], plan["window_ends_utc"])
    )
    log.info(
        "Delivered: charger=%s connector=%s  windows=%s",
        charge_point_id, connector_id, windows_str,
    )



# ===========================================================================
# Delivery orchestration
# ===========================================================================

def deliver(plans_by_profile: dict[str, dict], deliveries: list[dict]) -> bool:
    """Run all deliveries. Returns True if all succeeded, False if any failed.

    Logs in once and reuses the token for all deliveries.
    """
    email    = os.environ.get("CHARGEAMPS_EMAIL", "")
    password = os.environ.get("CHARGEAMPS_PASSWORD", "")
    if not email or not password:
        log.error(
            "CHARGEAMPS_EMAIL and CHARGEAMPS_PASSWORD environment variables must be set."
        )
        return False

    # Login once
    try:
        token, ent_token = ca_login(email, password)
    except Exception as exc:
        log.error("Login failed: %s", exc)
        return False

    all_ok = True

    for entry in deliveries:
        profile      = entry.get("profile", "")
        cp_id_env    = entry.get("charge_point_id_env", "")
        connector_id = int(entry.get("connector_id", 1))
        max_current  = float(entry.get("max_current", 16.0))
        tz_name      = entry.get("timezone", "UTC")

        # Resolve charge point ID from env var
        if not cp_id_env:
            log.error(
                "Delivery for profile '%s' is missing charge_point_id_env — skipping.",
                profile,
            )
            all_ok = False
            continue

        cp_id = os.environ.get(cp_id_env, "")
        if not cp_id:
            log.error(
                "Env var %s (charge_point_id for profile '%s') is not set — skipping.",
                cp_id_env, profile,
            )
            all_ok = False
            continue

        # Find matching plan
        plan = plans_by_profile.get(profile)
        if plan is None:
            log.error(
                "No plan found for profile '%s' — skipping delivery to %s.",
                profile, cp_id,
            )
            all_ok = False
            continue

        if not plan.get("window_starts_utc"):
            log.warning(
                "Plan for profile '%s' has no windows — skipping delivery to %s.",
                profile, cp_id,
            )
            all_ok = False
            continue


        log.info(
            "Delivering profile '%s' -> charger %s connector %s",
            profile, cp_id, connector_id,
        )

        try:
            ca_put_schedule(
                plan, cp_id, connector_id,
                tz_name, max_current, token, ent_token,
            )
        except Exception as exc:
            log.error(
                "Delivery failed for profile '%s' → %s: %s",
                profile, cp_id, exc,
            )
            all_ok = False

    return all_ok


# ===========================================================================
# CLI
# ===========================================================================

def main():
    parser = argparse.ArgumentParser(
        description="Deliver charging plan(s) to Charge Amps via my.charge.space",
    )
    parser.add_argument(
        "plan_files",
        nargs="+",
        metavar="PLAN_JSON",
        help="One or more plan JSON files produced by charging_planner.py",
    )
    parser.add_argument(
        "--config", "-c",
        default="chargeamps/chargeamps.yaml",
        help="Path to chargeamps.yaml (default: chargeamps.yaml)",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable debug logging",
    )
    args = parser.parse_args()

    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)

    # Load config
    cfg = load_config(args.config)
    deliveries = cfg.get("deliveries", [])
    if not deliveries:
        log.error("No deliveries configured in %s.", args.config)
        sys.exit(1)

    # Load plan files
    plans_by_profile: dict[str, dict] = {}
    for path in args.plan_files:
        try:
            with open(path) as f:
                plan = json.load(f)
            profile = plan.get("profile", "")
            if not profile:
                log.warning("Plan file %s has no 'profile' field — skipping.", path)
                continue
            plans_by_profile[profile] = plan
            log.info("Loaded plan: profile='%s'  date=%s  file=%s",
                     profile, plan.get("date"), path)
        except (OSError, json.JSONDecodeError) as exc:
            log.error("Failed to load %s: %s", path, exc)
            sys.exit(1)

    if not plans_by_profile:
        log.error("No valid plan files loaded.")
        sys.exit(1)

    ok = deliver(plans_by_profile, deliveries)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
