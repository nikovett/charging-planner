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

    charging:
      - name: "topup"
        ...
        deliveries:
          - handler: "chargeamps"       # → delivery/deliver_chargeamps.py
            charge_point_id_env: "CHARGER_ID_1"
            connector_id: 1
            max_charging_rate: 16.0

          - handler: "ocpp"             # → delivery/deliver_ocpp.py
            charge_point_id_env:        # list — same plan sent to both chargers
              - "CHARGER_ID_2"
              - "CHARGER_ID_3"
            endpoint_url_env: "OCPP_ENDPOINT_URL"
            ocpp_version: "1.6"

Timezone is taken from the charging profile and passed to handlers directly —
it does not need to be repeated inside delivery entries.

'charge_point_id_env' accepts either a single string or a list of strings.
Each resolved ID is delivered independently; all are attempted even if one
fails — the exit code reflects whether all succeeded.

Handlers expose:  deliver(plan, charge_point_id, entry, timezone) -> bool
Exit code is 0 only if every delivery succeeded.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import logging
import os
import sys
from pathlib import Path

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

# Directory containing this script — used to locate deliver_<handler>.py files
CHARGER_DIR = Path(__file__).parent.resolve()


# ===========================================================================
# Config
# ===========================================================================

def load_config(path: str) -> dict:
    if yaml is None:
        log.error("PyYAML is not installed. Run: pip install pyyaml")
        sys.exit(1)
    if not os.path.exists(path):
        log.error("Config file not found: %s", path)
        sys.exit(1)
    with open(path) as f:
        cfg = yaml.safe_load(f) or {}
    return cfg


def _extract_deliveries(config: dict) -> list[tuple[str, str, dict, list[str]]]:
    """Walk charging profiles and extract (profile_name, timezone, entry, charge_point_ids).

    timezone is taken from the entsoe: block — a single value shared across all profiles.
    charge_point_id_env may be a string or a list — both are normalised to a
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
            if not entry.get("enabled", True):
                log.info(
                    "Delivery entry (handler: %s) in profile '%s' is disabled — skipping.",
                    handler or "?", profile_name,
                )
                continue
            if not handler:
                log.error(
                    "Delivery entry in profile '%s' is missing a 'handler' field — skipping.\n"
                    "  Add  handler: <n>  matching a deliver_<n>.py script.",
                    profile_name,
                )
                continue

            # Normalise charge_point_id_env to a list
            cp_id_env_raw = entry.get("charge_point_id_env")
            if cp_id_env_raw is None:
                log.error(
                    "Delivery entry (handler: %s) in profile '%s' has no "
                    "charge_point_id_env — skipping.",
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
# Dispatch
# ===========================================================================

def _send_delivery_ntfy(
    plans_by_profile: dict[str, dict],
    skipped_profiles: list[str],
    delivery_results: list[tuple[str, str, str, bool]],
    config: dict,
) -> None:
    """Send a combined ntfy notification with plan summary and delivery status.

    Each profile section shows its charging windows followed immediately by the
    delivery status for that profile. delivery_results is a list of
    (profile_name, handler_name, charge_point_id, ok) tuples.
    """
    ntfy_cfg = config.get("ntfy", {})
    if not ntfy_cfg.get("enabled", False):
        return

    topic = os.environ.get("NTFY_TOPIC") or ntfy_cfg.get("topic", "")
    if not topic:
        log.warning("ntfy.enabled is true but no topic configured — skipping.")
        return

    plans = sorted(
        [p for p in plans_by_profile.values() if p.get("window_starts_utc")],
        key=lambda p: p.get("required_minutes", 0),
    )

    if not plans:
        return

    # Index delivery results by profile for quick lookup
    results_by_profile: dict[str, list[tuple[str, str, bool]]] = {}
    for profile, handler, cp_id, ok in delivery_results:
        results_by_profile.setdefault(profile, []).append((handler, cp_id, ok))

    def fmt_h(minutes: int) -> str:
        h, m = divmod(minutes, 60)
        return f"{h}h" if m == 0 else f"{h}h{m:02d}m"

    sections = []
    for plan in plans:
        profile = plan.get("profile", "default")
        wins    = plan["windows"]
        req     = plan["required_minutes"]
        tot     = plan["total_minutes"]

        if wins:
            win_lines = "\n".join(
                f"{w['start']}–{w['end']}  {w['avg_price_cents_kwh']:.2f} c€/kWh"
                for w in wins
            )
        else:
            win_lines = "No windows selected"

        summary = (f"{fmt_h(tot)}/{fmt_h(req)}"
                   + (" ⚠️ charge plan not possible" if tot < req else ""))

        block = f"{profile}  {summary}\n{win_lines}"

        # Delivery status for this profile — immediately after windows
        profile_results = results_by_profile.get(profile, [])
        if profile_results:
            delivery_lines = "\n".join(
                f"{'✓' if ok else '✗'} {cp_id} ({handler})"
                for handler, cp_id, ok in profile_results
            )
            block += f"\n\nDeliveries:\n{delivery_lines}"

        sections.append(block)

    if skipped_profiles:
        skipped_lines = "\n".join(f"- {n}: prices not yet published"
                                   for n in skipped_profiles)
        sections.append(f"⏳ Skipped\n{skipped_lines}")

    message = "\n\n".join(sections)
    date    = plans[0].get("date", "")
    url     = f"https://ntfy.sh/{topic}"

    try:
        import urllib.request as _ur
        req = _ur.Request(
            url,
            data=message.encode(),
            method="POST",
            headers={
                "Title":    f"Charging plan for {date}",
                "Priority": "default",
            },
        )
        _ur.urlopen(req, timeout=10)
        log.info("ntfy notification sent to %s", url)
    except Exception as exc:
        log.warning("ntfy notification failed: %s", exc)


def dispatch(plans_by_profile: dict[str, dict], config: dict) -> bool:
    """Resolve deliveries from charging profiles and call each handler.

    For each delivery entry, calls:
        handler.deliver(plan, charge_point_id, entry, timezone)
    once per resolved charger ID. All chargers are attempted; failures are
    accumulated and reported at the end. Sends a combined ntfy notification
    with plan summary and delivery status after all deliveries complete.

    Returns True only if every delivery succeeded.
    """
    deliveries = _extract_deliveries(config)

    if not deliveries:
        log.warning("No delivery entries found in any charging profile — nothing to do.")
        return True

    handler_cache: dict[str, object] = {}
    all_ok = True
    delivery_results: list[tuple[str, str, str, bool]] = []  # (profile, handler, cp_id, ok)

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

            delivery_results.append((profile_name, handler_name, charge_point_id, ok))

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

    # Profiles in config that had no plan (prices skipped) — infer from missing plans
    skipped_profiles = [
        p.get("name", "default")
        for p in config.get("charging", [])
        if isinstance(p, dict)
        and p.get("deliveries")
        and p.get("name") not in plans_by_profile
    ]

    _send_delivery_ntfy(plans_by_profile, skipped_profiles, delivery_results, config)

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
        "--debug",
        action="store_true",
        help="Enable debug logging",
    )
    args = parser.parse_args()

    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)

    config = load_config(args.config)

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

    ok = dispatch(plans_by_profile, config)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
