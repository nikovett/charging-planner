#!/usr/bin/env python3
"""
Charging Plan Delivery Dispatcher
===================================
Reads plan JSON files produced by charging_planner.py and dispatches each
delivery entry to the correct handler script based on the 'handler' field.

Usage:
    python delivery/deliver.py plan-*.json
    python delivery/deliver.py plan-*.json --config config.yaml

Deliveries are configured inside each charging profile in config.yaml:

    profiles:
      - name: topup
        schedule:
          mon-sun: { window: 21:00-06:30, required: 2 }
        delivery:
          - chargeamps: { charger: CHARGER_ID_1, connector: 1, max_amps: 16.0 }   # → delivery/deliver_chargeamps.py


Timezone is set once at the top level of config.yaml and passed to handlers
directly — it does not need to be repeated inside delivery entries.

Each handler's charge-point-ID key (e.g. 'charger', 'vin') accepts either a
single string or a list of strings. Each resolved ID is delivered
independently; all are attempted even if one fails — the exit code reflects
whether all succeeded.

Exit code is 0 only if every delivery succeeded. Failures are surfaced via
non-zero exit so the GitHub Actions job is marked as failed and the operator
receives an email notification.

A second run for the same profile close behind the first (e.g. an unreliable
scheduled trigger plus a manual backup) will skip re-delivering when doing so
would be redundant or risk interrupting a session the first delivery already
started — see should_skip_redundant_delivery() for the exact rules. Persisted
records live under --data-dir (default: data/), committed to the repo so the
check survives across runs.

Handlers expose:  deliver(plan, charge_point_id, entry, timezone) -> bool
Exit code is 0 only if every delivery succeeded.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import logging
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

# charging_planner.py lives one directory up from this script (the repo
# root); sys.path[0] is this script's own directory when run directly
# (`python delivery/deliver.py ...`), not the repo root, so a bare
# `import charging_planner` would fail regardless of the caller's CWD.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import charging_planner as cp

# ===========================================================================
# Logging
# ===========================================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# Directory containing this script — used to locate deliver_<handler>.py files
CHARGER_DIR = Path(__file__).parent.resolve()


# ===========================================================================
# Config
# ===========================================================================


def _extract_deliveries(config: dict) -> list[tuple[str, str, dict, list[str]]]:
    """Walk charging profiles and extract (profile_name, timezone, entry, charge_point_ids).

    timezone is taken from the entsoe: block — a single value shared across all profiles.
    charge_point_id may be a string or a list — both are normalised to a
    list of resolved environment variable values here so handlers never need
    to deal with the distinction.

    Returns a flat list of (profile_name, timezone, entry, [cp_id, ...]) tuples,
    one tuple per delivery entry. Each tuple may carry multiple charger IDs.
    """
    profiles = config.get("charging", [])
    if isinstance(profiles, dict):
        profiles = [profiles]

    timezone = config.get("entsoe", {}).get("timezone", "UTC")

    results = []
    for profile in profiles:
        profile_name = profile.get("name", "default")
        deliveries   = profile.get("deliveries", [])
        if not deliveries:
            continue

        for entry in deliveries:
            handler = entry.get("handler", "").strip()
            if not handler:
                log.error(
                    "Delivery entry in profile '%s' is missing a 'handler' field — skipping.\n"
                    "  Add  handler: <n>  matching a deliver_<n>.py script.",
                    profile_name,
                )
                continue

            # Normalise charge_point_id to a list
            cp_id_env_raw = entry.get("charge_point_id")
            if cp_id_env_raw is None:
                log.error(
                    "Delivery entry (handler: %s) in profile '%s' has no "
                    "charge_point_id — skipping.",
                    handler, profile_name,
                )
                continue

            cp_id_envs = (
                cp_id_env_raw if isinstance(cp_id_env_raw, list)
                else [cp_id_env_raw]
            )

            # Resolve each env var to its value
            charge_point_ids = []
            for env_var in cp_id_envs:
                value = os.environ.get(env_var, "")
                if not value:
                    log.error(
                        "Env var %s (charge_point_id for profile '%s', handler '%s') "
                        "is not set — skipping this charger.",
                        env_var, profile_name, handler,
                    )
                else:
                    charge_point_ids.append(value)

            if not charge_point_ids:
                log.error(
                    "No valid charge point IDs resolved for profile '%s', "
                    "handler '%s' — skipping.",
                    profile_name, handler,
                )
                continue

            results.append((profile_name, timezone, entry, charge_point_ids))

    return results


# ===========================================================================
# Handler loading
# ===========================================================================

def _load_handler(handler_name: str):
    """Dynamically import deliver_<handler>.py from the charger directory.

    Returns the module object. Exits on import failure so the error is
    surfaced clearly rather than as an AttributeError later.
    """
    script_name = f"deliver_{handler_name}.py"
    script_path = CHARGER_DIR / script_name

    if not script_path.exists():
        log.error(
            "Handler script not found: %s\n"
            "  Expected location: %s\n"
            "  Check that 'handler: %s' in config.yaml matches a deliver_%s.py file.",
            script_name, script_path, handler_name, handler_name,
        )
        sys.exit(1)

    spec   = importlib.util.spec_from_file_location(f"deliver_{handler_name}", script_path)
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception as exc:
        log.error("Failed to load handler %s: %s", script_path, exc)
        sys.exit(1)

    if not hasattr(module, "deliver"):
        log.error(
            "%s does not expose a deliver(plan, charge_point_id, entry, timezone) function.",
            script_name,
        )
        sys.exit(1)

    return module


# ===========================================================================
# Redundant delivery protection
# ===========================================================================
#
# Two triggers close together (e.g. an unreliable GHA schedule plus a manual
# backup trigger) can each build and deliver a plan for the same profile.
# Re-delivering is not free: it can overwrite a manual adjustment made to the
# charger/vehicle between the two runs. A small persisted record per
# (profile, handler, charge_point_id) — the window times, generation time,
# and forecast status of the last plan actually delivered — lets a second,
# redundant run recognise that and skip. See CONTEXT.md "Redundant delivery
# protection" for the full rationale and the Guiding Principles it serves.

def _delivered_record_path(data_dir: str, profile_name: str, handler_name: str,
                           charge_point_id: str) -> Path:
    """Path to the persisted delivered-record file for one delivery target.

    charge_point_id is hashed rather than embedded directly — for MyŠkoda
    it's the vehicle's VIN, for Charge Amps a charger serial, both directly
    identifying. This file lives under --data-dir, which is committed to
    the repo (see the GHA workflow), typically public via GitHub Pages
    alongside the dashboard — the real ID has no reason to be readable
    there. The hash only needs to be stable and distinct per target, not
    cryptographically secure.
    """
    id_hash = hashlib.sha256(charge_point_id.encode()).hexdigest()[:12]
    safe_profile = re.sub(r"[^A-Za-z0-9_-]", "_", profile_name)
    safe_handler = re.sub(r"[^A-Za-z0-9_-]", "_", handler_name)
    return Path(data_dir) / f"delivered-{safe_profile}-{safe_handler}-{id_hash}.json"


def _load_delivered_record(path: Path) -> Optional[dict]:
    if not path.exists():
        return None
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("Could not read delivered record %s: %s", path, exc)
        return None


def _save_delivered_record(path: Path, plan: dict) -> None:
    record = {
        "window_starts_utc":           plan.get("window_starts_utc"),
        "window_ends_utc":             plan.get("window_ends_utc"),
        "generated_at":                plan.get("generated_at"),
        "configured_window_start_utc": plan.get("configured_window_start_utc"),
        "schedule_uses_forecast":      plan.get("schedule_uses_forecast"),
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w") as f:
            json.dump(record, f, indent=2)
    except OSError as exc:
        log.warning("Could not write delivered record %s: %s", path, exc)


def _parse_iso(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def should_skip_redundant_delivery(
    plan: dict,
    prior: Optional[dict],
    profile_name: str,
    handler_name: str = "unknown",
    charge_point_id: str = "unknown",
) -> bool:
    """Decide whether to skip delivery because it would be redundant or unsafe.

    handler_name and charge_point_id identify the specific delivery target
    for log messages only — the decision itself is driven entirely by plan
    and prior (which dispatch() already loaded from the record for this
    exact (profile, handler, charge_point_id) triple). Naming the specific
    target in the log matters once a profile delivers to more than one
    charger: without it, "skipping delivery" for profile 'X' is ambiguous
    about which of X's chargers was actually skipped.

    Checked in order:
      1. No prior record — nothing to compare against, deliver.
      2. The prior delivered schedule relied on forecast data, and this
         plan is either real-price-based or has different windows — always
         yield, regardless of window liveness. A forecast-based commitment
         is only an estimate and must always be correctable. This does NOT
         apply when the new plan is also forecast-based and produced
         byte-identical windows — that's not a correction, just the same
         estimate confirmed again by a redundant trigger, and forcing a
         redelivery there would burn an API call for nothing (falls through
         to rule 4 instead, which will skip it as unchanged).
      3. This run's window is live (already started) and the prior plan
         predates that same window's start — the prior plan is an
         already-committed pre-window schedule; redelivering here risks
         interrupting whatever it already started. Only applies when both
         plans target the *same* window instance (configured_window_start_utc
         matches) — otherwise this would wrongly compare against an
         unrelated, already-elapsed window from a previous cycle.
      4. Otherwise, identical scheduled windows to the prior delivery means
         nothing has changed — skip. Different windows — deliver.
    """
    if prior is None:
        return False

    same_windows = (
        plan.get("window_starts_utc") == prior.get("window_starts_utc")
        and plan.get("window_ends_utc") == prior.get("window_ends_utc")
    )

    if prior.get("schedule_uses_forecast") and not (plan.get("schedule_uses_forecast") and same_windows):
        return False

    new_cfg_start   = _parse_iso(plan.get("configured_window_start_utc"))
    new_gen_at      = _parse_iso(plan.get("generated_at"))
    prior_cfg_start = _parse_iso(prior.get("configured_window_start_utc"))
    prior_gen_at    = _parse_iso(prior.get("generated_at"))

    if (new_cfg_start and new_gen_at and prior_cfg_start and prior_gen_at
            and new_cfg_start == prior_cfg_start
            and new_gen_at >= new_cfg_start
            and prior_gen_at < prior_cfg_start):
        log.info(
            "Skipping delivery: profile='%s'  handler='%s'  charger='%s' — a "
            "plan for this window was already delivered before it opened; "
            "this run is live and redelivering risks interrupting whatever "
            "that plan started.",
            profile_name, handler_name, charge_point_id,
        )
        return True

    if same_windows:
        log.info(
            "Skipping delivery: profile='%s'  handler='%s'  charger='%s' — "
            "unchanged from the already-delivered plan.",
            profile_name, handler_name, charge_point_id,
        )
        return True

    return False


# ===========================================================================
# Dispatch
# ===========================================================================


def dispatch(plans_by_profile: dict[str, dict], config: dict, data_dir: str = "data") -> bool:
    """Resolve deliveries from charging profiles and call each handler.

    For each delivery entry, calls:
        handler.deliver(plan, charge_point_id, entry, timezone)
    once per resolved charger ID. All chargers are attempted; failures are
    accumulated and reported at the end. Before each call, checks
    should_skip_redundant_delivery against the last successfully delivered
    plan for that (profile, handler, charger) — see that function's
    docstring for when a delivery is skipped rather than attempted.

    Returns True only if every attempted delivery succeeded (a skip does not
    count as a failure).
    """
    deliveries = _extract_deliveries(config)

    if not deliveries:
        log.warning("No delivery entries found in any charging profile — nothing to do.")
        return True

    handler_cache: dict[str, object] = {}
    all_ok = True

    for profile_name, timezone, entry, charge_point_ids in deliveries:
        handler_name = entry["handler"]

        plan = plans_by_profile.get(profile_name)
        if plan is None:
            log.error(
                "No plan found for profile '%s' — skipping all deliveries for this profile.",
                profile_name,
            )
            all_ok = False
            continue

        if not plan.get("window_starts_utc"):
            log.warning(
                "Plan for profile '%s' has no windows — skipping delivery.",
                profile_name,
            )
            all_ok = False
            continue

        # Load handler module once per unique handler name
        if handler_name not in handler_cache:
            handler_cache[handler_name] = _load_handler(handler_name)
        module = handler_cache[handler_name]

        for charge_point_id in charge_point_ids:
            record_path = _delivered_record_path(data_dir, profile_name, handler_name, charge_point_id)
            prior = _load_delivered_record(record_path)

            if should_skip_redundant_delivery(plan, prior, profile_name, handler_name, charge_point_id):
                continue

            log.info(
                "Delivering profile '%s' → handler '%s'  charger '%s'  timezone '%s'",
                profile_name, handler_name, charge_point_id, timezone,
            )
            try:
                ok = module.deliver(plan, charge_point_id, entry, timezone)
            except Exception as exc:
                log.error(
                    "Handler '%s' raised an unexpected error for charger '%s': %s",
                    handler_name, charge_point_id, exc,
                )
                ok = False

            if not ok:
                log.error(
                    "Delivery failed: profile='%s'  handler='%s'  charger='%s'",
                    profile_name, handler_name, charge_point_id,
                )
                all_ok = False
            else:
                log.info(
                    "Delivery succeeded: profile='%s'  handler='%s'  charger='%s'",
                    profile_name, handler_name, charge_point_id,
                )
                _save_delivered_record(record_path, plan)

    return all_ok


# ===========================================================================
# CLI
# ===========================================================================

def main():
    parser = argparse.ArgumentParser(
        description=(
            "Dispatch charging plan(s) to the correct charger handler "
            "based on the 'handler' field inside each charging profile in config.yaml."
        ),
    )
    parser.add_argument(
        "plan_files",
        nargs="+",
        metavar="PLAN_JSON",
        help="One or more plan JSON files produced by charging_planner.py",
    )
    parser.add_argument(
        "--config", "-c",
        default="config.yaml",
        help="Path to config.yaml (default: config.yaml)",
    )
    parser.add_argument(
        "--data-dir",
        default="data",
        help="Directory holding persisted delivered-plan records, used to "
             "detect and skip redundant re-delivery (default: data)",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable debug logging",
    )
    args = parser.parse_args()

    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)

    try:
        config = cp.load_config(args.config)
    except FileNotFoundError:
        log.error("Config file not found: %s", args.config)
        sys.exit(1)
    except cp.ConfigError as exc:
        log.error("Config error: %s", exc)
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
            log.info(
                "Loaded plan: profile='%s'  date=%s  file=%s",
                profile, plan.get("date"), path,
            )
        except (OSError, json.JSONDecodeError) as exc:
            log.error("Failed to load %s: %s", path, exc)
            sys.exit(1)

    if not plans_by_profile:
        log.error("No valid plan files loaded.")
        sys.exit(1)

    ok = dispatch(plans_by_profile, config, data_dir=args.data_dir)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
