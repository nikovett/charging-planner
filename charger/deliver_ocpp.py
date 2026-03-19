#!/usr/bin/env python3
"""
OCPP WebSocket Schedule Delivery
==================================
Delivers a charging plan to a single OCPP charge point via WebSocket
(SetChargingProfile.req).

Invoked by charger/deliver.py once per charger ID when handler: ocpp is set
inside a charging profile's deliveries in config.yaml.

Dependencies:
    pip install websockets        # async WebSocket client (stdlib has none)

Environment variables:
    OCPP_ENDPOINT_URL   WebSocket base URL of your CSMS, e.g.
                        ws://192.168.1.10:9000/ocpp or
                        wss://csms.example.com/ocpp
                        Named via endpoint_url_env in config.yaml.

config.yaml delivery entry:
    deliveries:
      - handler: "ocpp"
        charge_point_id_env: "OCPP_CHARGE_POINT_ID"   # or a list
        endpoint_url_env: "OCPP_ENDPOINT_URL"
        ocpp_version: "1.6"      # "1.6", "2.0.1", or "2.1" — default 1.6
        connector_id: 1          # OCPP 1.6 connectorId / 2.x evseId
        transaction_id: null     # optional; null = TxDefaultProfile
        timeout: 30              # seconds to wait for response
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import string
import sys

try:
    import websockets
    import websockets.exceptions
except ImportError:
    print(
        "ERROR: 'websockets' package is required.\n"
        "Install it with:  pip install websockets",
        file=sys.stderr,
    )
    sys.exit(1)

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# OCPP message type codes (OCPP 1.6 §4.2 / 2.0.1 §4.2)
# ---------------------------------------------------------------------------
CALL       = 2   # client → server
CALLRESULT = 3   # server → client (success)
CALLERROR  = 4   # server → client (error)


# ===========================================================================
# Unique message ID (OCPP §4.1)
# ===========================================================================

def _unique_id(length: int = 8) -> str:
    return "".join(random.choices(string.ascii_letters + string.digits, k=length))


# ===========================================================================
# OCPP payload builder
# ===========================================================================

def _build_profile(
    plan: dict,
    ocpp_version: str,
    transaction_id: int | None,
) -> dict:
    """Build an OCPP ChargingProfile from plan window timestamps.

    Charging windows run at 11 kW; gaps between windows are set to 0 so the
    vehicle does not charge outside the planned slots.
    """
    from datetime import datetime

    starts = [datetime.fromisoformat(s) for s in plan.get("window_starts_utc", [])]
    ends   = [datetime.fromisoformat(s) for s in plan.get("window_ends_utc",   [])]

    if not starts:
        raise ValueError("Plan has no window_starts_utc — cannot build OCPP profile.")

    schedule_start = starts[0]
    schedule_end   = ends[-1]
    duration_sec   = int((schedule_end - schedule_start).total_seconds())
    max_rate       = 11000.0  # W

    periods = []
    cursor  = schedule_start
    for win_start, win_end in zip(starts, ends):
        if win_start > cursor:
            periods.append({
                "startPeriod": int((cursor - schedule_start).total_seconds()),
                "limit": 0.0,
            })
        periods.append({
            "startPeriod": int((win_start - schedule_start).total_seconds()),
            "limit": max_rate,
        })
        cursor = win_end

    if cursor < schedule_end:
        periods.append({
            "startPeriod": int((cursor - schedule_start).total_seconds()),
            "limit": 0.0,
        })

    is_v2          = not ocpp_version.startswith("1.6")
    profile_id_key = "id" if is_v2 else "chargingProfileId"

    schedule: dict = {
        "startSchedule":          schedule_start.isoformat(),
        "duration":               duration_sec,
        "chargingRateUnit":       "W",
        "chargingSchedulePeriod": periods,
    }
    if is_v2:
        schedule["id"] = 1

    return {
        profile_id_key:           1,
        "stackLevel":             0,
        "chargingProfilePurpose": "TxDefaultProfile",
        "chargingProfileKind":    "Absolute",
        "validFrom":              schedule_start.isoformat(),
        "validTo":                schedule_end.isoformat(),
        "chargingSchedule":       schedule,
    }


def _set_charging_profile_payload(
    plan: dict,
    ocpp_version: str,
    connector_id: int,
    transaction_id: int | None,
) -> dict:
    """Wrap the ChargingProfile in the correct SetChargingProfile.req envelope.

    OCPP 1.6  →  { connectorId, csChargingProfiles }
    OCPP 2.x  →  { evseId, chargingProfile }
    """
    profile = _build_profile(plan, ocpp_version, transaction_id)
    is_v2   = not ocpp_version.startswith("1.6")

    if is_v2:
        return {
            "evseId":          connector_id,
            "chargingProfile": profile,
        }
    else:
        if transaction_id is not None:
            profile["transactionId"] = transaction_id
        return {
            "connectorId":        connector_id,
            "csChargingProfiles": profile,
        }


# ===========================================================================
# WebSocket OCPP call
# ===========================================================================

async def _send_set_charging_profile(
    endpoint_url:    str,
    charge_point_id: str,
    payload:         dict,
    ocpp_version:    str,
    timeout:         int = 30,
) -> bool:
    """Open a WebSocket connection, send SetChargingProfile.req, verify response.

    Connects to <endpoint_url>/<charge_point_id> — the standard OCPP URL
    convention. Returns True on Accepted, False otherwise.
    """
    subprotocol_map = {
        "1.6":   "ocpp1.6",
        "2.0.1": "ocpp2.0.1",
        "2.1":   "ocpp2.1",
    }
    subprotocol = subprotocol_map.get(ocpp_version, "ocpp1.6")
    ws_url      = endpoint_url.rstrip("/") + "/" + charge_point_id
    message_id  = _unique_id()
    call_frame  = json.dumps([CALL, message_id, "SetChargingProfile", payload])

    log.info("Connecting to %s (subprotocol: %s)", ws_url, subprotocol)
    log.debug("CALL → %s", call_frame)

    try:
        async with websockets.connect(
            ws_url,
            subprotocols=[subprotocol],
            open_timeout=timeout,
            close_timeout=10,
        ) as ws:
            await ws.send(call_frame)
            log.info("SetChargingProfile.req sent (msgId=%s)", message_id)

            deadline = asyncio.get_event_loop().time() + timeout
            while True:
                remaining = deadline - asyncio.get_event_loop().time()
                if remaining <= 0:
                    log.error("Timeout waiting for SetChargingProfile response.")
                    return False

                raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
                log.debug("← %s", raw)

                try:
                    frame = json.loads(raw)
                except json.JSONDecodeError:
                    log.warning("Received non-JSON frame — ignoring: %s", raw[:200])
                    continue

                if not isinstance(frame, list) or len(frame) < 3:
                    log.warning("Unexpected frame shape — ignoring: %s", frame)
                    continue

                msg_type    = frame[0]
                resp_msg_id = frame[1]

                if resp_msg_id != message_id:
                    log.debug("Frame for different msgId %s — ignoring.", resp_msg_id)
                    continue

                if msg_type == CALLRESULT:
                    status = (frame[2] if len(frame) > 2 else {}).get("status", "")
                    if status == "Accepted":
                        log.info("SetChargingProfile accepted by %s.", charge_point_id)
                        return True
                    else:
                        log.error(
                            "SetChargingProfile returned status=%r for %s.",
                            status, charge_point_id,
                        )
                        return False

                elif msg_type == CALLERROR:
                    error_code = frame[2] if len(frame) > 2 else "?"
                    error_desc = frame[3] if len(frame) > 3 else ""
                    log.error(
                        "CALLERROR from %s: %s — %s",
                        charge_point_id, error_code, error_desc,
                    )
                    return False

                else:
                    log.warning("Unexpected MessageTypeId %s — ignoring.", msg_type)

    except websockets.exceptions.InvalidURI:
        log.error("Invalid WebSocket URL: %s", ws_url)
        return False
    except websockets.exceptions.InvalidHandshake as exc:
        log.error("WebSocket handshake failed for %s: %s", ws_url, exc)
        return False
    except (OSError, ConnectionRefusedError) as exc:
        log.error("Could not connect to %s: %s", ws_url, exc)
        return False
    except asyncio.TimeoutError:
        log.error("Connection timed out to %s", ws_url)
        return False


# ===========================================================================
# Public interface — called by charger/deliver.py
# ===========================================================================

def deliver(plan: dict, charge_point_id: str, entry: dict, timezone: str) -> bool:
    """Deliver a plan to a single OCPP charge point via WebSocket.

    Args:
        plan:             Plan dict as produced by charging_planner.py.
        charge_point_id:  Resolved charger ID (already read from env by dispatcher).
        entry:            The delivery config entry from config.yaml, containing
                          endpoint_url_env, ocpp_version, connector_id, etc.
        timezone:         IANA timezone name inherited from the charging profile.
                          Not used by the OCPP handler but accepted for a
                          consistent interface across all handlers.

    Returns True on success, False on failure.
    """
    endpoint_env   = entry.get("endpoint_url_env", "OCPP_ENDPOINT_URL")
    ocpp_version   = str(entry.get("ocpp_version", "1.6"))
    connector_id   = int(entry.get("connector_id", 1))
    transaction_id = entry.get("transaction_id")
    timeout        = int(entry.get("timeout", 30))

    endpoint_url = os.environ.get(endpoint_env, "")
    if not endpoint_url:
        log.error(
            "Env var %s (OCPP endpoint URL) is not set — skipping charger '%s'.",
            endpoint_env, charge_point_id,
        )
        return False

    try:
        payload = _set_charging_profile_payload(
            plan, ocpp_version, connector_id, transaction_id,
        )
    except Exception as exc:
        log.error(
            "Failed to build OCPP payload for charger '%s': %s",
            charge_point_id, exc,
        )
        return False

    log.debug("SetChargingProfile payload: %s", json.dumps(payload, indent=2))

    ok = asyncio.run(
        _send_set_charging_profile(
            endpoint_url, charge_point_id, payload, ocpp_version, timeout,
        )
    )

    if ok:
        windows_str = ", ".join(
            f"{s}–{e}"
            for s, e in zip(plan["window_starts_utc"], plan["window_ends_utc"])
        )
        log.info("Delivered: charger=%s  windows=%s", charge_point_id, windows_str)

    return ok
