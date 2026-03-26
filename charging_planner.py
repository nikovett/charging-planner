#!/usr/bin/env python3
"""
Charging Planner
================
Fetches day-ahead electricity prices from the ENTSO-E Transparency Platform,
selects the cheapest charging windows for each configured profile, and writes
a plan to JSON for review and downstream use (e.g. OCPP smart charging).

Usage:
    python charging_planner.py [--config config.yaml] [--output-dir .]

Run daily after ~12:00 UTC when ENTSO-E publishes next-day prices.
"""

import argparse
from dataclasses import dataclass, field
import json
import logging
import os
import sys
import time as time_module
import xml.etree.ElementTree as ET
from datetime import date, datetime, time, timedelta, timezone
from typing import Optional
import urllib.request
import urllib.error
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# ---------------------------------------------------------------------------
# Optional dependencies
# ---------------------------------------------------------------------------
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


# ===========================================================================
# Data model
# ===========================================================================

@dataclass(frozen=True)
class Slot:
    """A single 15/30/60-minute price slot from ENTSO-E.

    frozen=True makes Slot hashable (usable in sets) and its start/end
    datetimes serve as a natural key.  All selection and merge functions
    operate on lists of Slot rather than plain dicts.
    """
    start:             datetime
    end:               datetime
    duration_minutes:  int
    price_eur_kwh:     float
    slot:              int = 0          # ordinal within the fetched batch

    def __lt__(self, other: "Slot") -> bool:
        return self.start < other.start


# ===========================================================================
# Configuration
# ===========================================================================

# Default values applied to every charging profile.
CHARGING_DEFAULTS = {
    "name":                   "default",
    "required_hours":         4,
    "max_price_cents_kwh":    None,
    "continuous_only":        False,
    "min_slot_minutes":       30,
    "preferred_window_start": "00:00",
    "preferred_window_end":   "23:59",
}

DEFAULT_CONFIG = {
    # ENTSO-E settings — required for 'plan'
    "entsoe": {
        "api_key": "",
        "area": "10YFI-1--------U",
    },

    # One or more charging profiles. A bare dict is treated as a single profile.
    "charging": [CHARGING_DEFAULTS.copy()],

    # Push notifications via ntfy.sh (optional — omit or set enabled: false to disable)
    "ntfy": {
        "enabled": False,
        "topic":   "",   # never commit — use environment variable NTFY_TOPIC
    },

}


def load_config(path: str) -> dict:
    """Load config from a YAML file, merging over defaults.

    The charging key may be a single profile dict or a list of profile dicts.
    A bare dict is normalised to a single-element list before returning.
    Each profile is merged over CHARGING_DEFAULTS so omitted keys get defaults.
    """
    config = json.loads(json.dumps(DEFAULT_CONFIG))
    if path:
        if yaml is None:
            log.error("PyYAML is not installed. Run: pip install pyyaml")
            sys.exit(1)
        with open(path) as f:
            user_cfg = yaml.safe_load(f) or {}
        # Deep-merge everything except charging, which we handle separately
        charging_override = user_cfg.pop("charging", None)
        _deep_merge(config, user_cfg)
        if charging_override is not None:
            # Normalise bare dict to list
            if isinstance(charging_override, dict):
                charging_override = [charging_override]
            # Apply CHARGING_DEFAULTS to each profile
            profiles = []
            for profile in charging_override:
                merged = CHARGING_DEFAULTS.copy()
                merged.update(profile)
                profiles.append(merged)
            config["charging"] = profiles
    else:
        # No config file — ensure list form
        if isinstance(config["charging"], dict):
            config["charging"] = [config["charging"]]

    # Environment variable overrides — allow secrets to be injected at runtime
    # without modifying config.yaml (safe for public repos)
    if api_key := os.environ.get("ENTSOE_API_KEY"):
        config["entsoe"]["api_key"] = api_key
    if ntfy_topic := os.environ.get("NTFY_TOPIC"):
        config.setdefault("ntfy", {})["topic"] = ntfy_topic

    return config


def _deep_merge(base: dict, override: dict) -> None:
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _deep_merge(base[k], v)
        else:
            base[k] = v


# ===========================================================================
# HTTP retry helper
# ===========================================================================

def _http_request_with_retry(
    req: urllib.request.Request,
    timeout: int = 20,
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
            time_module.sleep(wait)

    raise last_exc


# ===========================================================================
# Config validation
# ===========================================================================

def _validate_entsoe_config(et: dict, errors: list) -> None:
    """Validate the entsoe: block, appending errors in-place."""
    if not et.get("api_key"):
        errors.append(
            "entsoe.api_key is required. "
            "Register at https://transparency.entsoe.eu and email transparency@entsoe.eu."
        )
    if not et.get("area"):
        errors.append("entsoe.area is required (e.g. 'FI').")


def _validate_hhmm(ch: dict, key: str, errors: list) -> Optional[tuple[int, int]]:
    """Parse and validate a HH:MM config field. Appends to errors and returns None on failure.

    Returns None without error if the value is "any" — callers treat that as no window constraint.
    """
    val = ch.get(key)
    if val is None:
        return None   # missing = treated as "any" by caller
    if str(val).strip().lower() == "any":
        return None   # explicit "any" — caller checks ch[key] == "any"
    try:
        h, m = map(int, str(val).split(":"))
        if not (0 <= h <= 23 and 0 <= m <= 59):
            raise ValueError
        return h, m
    except (ValueError, AttributeError):
        errors.append(f"charging.{key}={val!r} is not valid. Use 'HH:MM' (00:00–23:59) or 'any'.")
        return None


def _validate_charging_profile(ch: dict, errors: list) -> None:
    """Validate a single charging profile dict, appending errors in-place."""
    req_hours = ch.get("required_hours")
    if not isinstance(req_hours, (int, float)) or req_hours <= 0:
        errors.append(f"charging.required_hours must be a positive number, got: {req_hours!r}.")

    min_slot = ch.get("min_slot_minutes", 30)
    if not isinstance(min_slot, (int, float)) or min_slot <= 0:
        errors.append(f"charging.min_slot_minutes must be positive, got: {min_slot!r}.")
    elif int(min_slot) % 15 != 0:
        errors.append(f"charging.min_slot_minutes={min_slot} must be divisible by 15.")

    ceil = ch.get("max_price_cents_kwh")
    if ceil is not None and (not isinstance(ceil, (int, float)) or ceil <= 0):
        errors.append(f"charging.max_price_cents_kwh={ceil!r} must be a positive number or null.")

    schedule = ch.get("schedule") or []
    if not isinstance(schedule, list):
        errors.append("charging.schedule must be a list of day entries.")
    else:
        seen_days: set = set()
        for i, entry in enumerate(schedule):
            if not isinstance(entry, dict):
                errors.append(f"charging.schedule[{i}] must be a mapping.")
                continue
            days = entry.get("days")
            if not isinstance(days, list) or not days:
                errors.append(f"charging.schedule[{i}].days must be a non-empty list of day names.")
            else:
                valid = {"monday","tuesday","wednesday","thursday","friday","saturday","sunday"}
                for d in days:
                    if d not in valid:
                        errors.append(
                            f"charging.schedule[{i}].days: {d!r} is not a valid day name "
                            f"(use monday–sunday)."
                        )
                    elif d in seen_days:
                        errors.append(
                            f"charging.schedule[{i}].days: {d!r} appears in more than one entry."
                        )
                    else:
                        seen_days.add(d)
            # Validate window fields — "any" is accepted by _validate_hhmm directly
            _validate_hhmm(entry, "preferred_window_start", errors)
            _validate_hhmm(entry, "preferred_window_end", errors)
            e_start_any = entry.get("preferred_window_start") is None or str(entry.get("preferred_window_start", "")).lower() == "any"
            e_end_any   = entry.get("preferred_window_end")   is None or str(entry.get("preferred_window_end",   "")).lower() == "any"
            # Mixing any with HH:MM is allowed (see top-level comment above)

    pw_s = _validate_hhmm(ch, "preferred_window_start", errors)
    pw_e = _validate_hhmm(ch, "preferred_window_end", errors)
    # "any" returns None from _validate_hhmm without adding an error
    start_is_any = ch.get("preferred_window_start") is None or str(ch.get("preferred_window_start", "")).lower() == "any"
    end_is_any   = ch.get("preferred_window_end")   is None or str(ch.get("preferred_window_end",   "")).lower() == "any"
    # Mixing any with HH:MM is allowed:
    #   any + HH:MM = all slots from now until HH:MM (departure time)
    #   HH:MM + any = all slots from HH:MM until end of available prices
    if pw_s is not None and pw_e is not None and not start_is_any and not end_is_any:
        s_min = pw_s[0] * 60 + pw_s[1]
        e_min = pw_e[0] * 60 + pw_e[1]
        if s_min == e_min:
            errors.append(
                f"charging.preferred_window_start and preferred_window_end are equal "
                f"(got {ch['preferred_window_start']}–{ch['preferred_window_end']}), "
                f"which gives a zero-length window."
            )
        # s_min > e_min is an overnight window (e.g. 22:00–06:30) — allowed


def _warn_if_continuous_overflows_window(ch: dict) -> None:
    """Log an advisory warning when continuous_only and required_hours exceeds window length."""
    win_start = ch.get("preferred_window_start")
    win_end   = ch.get("preferred_window_end")
    if not (ch.get("continuous_only") and win_start and win_end):
        return
    if win_start is None or win_end is None or str(win_start).lower() == "any" or str(win_end).lower() == "any":
        return
    try:
        sh, sm = map(int, win_start.split(":"))
        eh, em = map(int, win_end.split(":"))
        s_min = sh * 60 + sm
        e_min = eh * 60 + em
        win_minutes = (e_min - s_min) if e_min > s_min else (24 * 60 - s_min + e_min)
        req_minutes = int(ch.get("required_hours", 0) * 60)
        if req_minutes > win_minutes > 0:
            log.warning(
                "continuous_only=true but required_hours=%.1f (%.0f min) exceeds "
                "preferred window %s–%s (%.0f min). "
                "Charging will start before the window.",
                ch["required_hours"], req_minutes, win_start, win_end, win_minutes,
            )
    except (ValueError, TypeError):
        pass


def validate_plan_config(config: dict) -> None:
    """Validate ENTSO-E and charging profile settings, raising ConfigError on failure."""
    errors: list = []
    _validate_entsoe_config(config.get("entsoe", {}), errors)
    _validate_charging_profile(config.get("charging", {}), errors)
    _emit_errors(errors)
    _warn_if_continuous_overflows_window(config.get("charging", {}))


def _emit_errors(errors: list) -> None:
    if errors:
        for e in errors:
            log.error("Config: %s", e)
        raise ConfigError(
            f"{len(errors)} config error(s). Please fix config.yaml and retry."
        )


# ===========================================================================
# Typed config
# ===========================================================================

@dataclass
class Config:
    """Parsed, validated configuration ready for use in cmd_plan.

    All type conversions happen here (hours→minutes, cents→EUR, HH:MM strings
    kept as-is for log messages).  cmd_plan receives a list of Configs and never
    touches the raw dict again.

    timezone_str is the raw IANA name from config (or None for auto-detect).
    It is NOT pre-resolved to a ZoneInfo here because the correct DST offset
    depends on target_date, which is not known until after prices are fetched.
    cmd_plan resolves it once with the actual target_date.
    """
    # ENTSO-E
    api_key:                str
    area:                   str
    timezone_str:           Optional[str]     # raw IANA name from entsoe: block, or None (auto-detect)

    # Profile
    name:                   str               # profile name for filenames and display

    # Charging
    required_minutes:       int
    continuous_only:        bool
    min_slot_minutes:       int
    max_price_eur:          Optional[float]   # None = no ceiling
    preferred_window_start: str               # validated HH:MM or "any"
    preferred_window_end:   str               # validated HH:MM or "any"
    preferred_window_any:   bool              # True = both start and end are "any" (no window constraint)
    window_start_any:       bool              # True = start is "any" (use now as start)
    window_end_any:         bool              # True = end is "any" (use last available price as end)
    schedule:               list              # day-specific window overrides (may be empty)


def _parse_one_profile(et: dict, ch: dict) -> "Config":
    """Build a Config from a single validated charging profile dict."""
    tz_str = et.get("timezone") or None
    if tz_str:
        try:
            ZoneInfo(tz_str)   # validate only; result discarded
        except (ZoneInfoNotFoundError, KeyError):
            raise ConfigError(
                f"entsoe.timezone={tz_str!r} is not a recognised IANA timezone name "
                f"(e.g. 'Europe/Helsinki', 'UTC'). "
                f"Check https://en.wikipedia.org/wiki/List_of_tz_database_time_zones"
            )
    ceil_cents = ch.get("max_price_cents_kwh")
    return Config(
        api_key=et["api_key"],
        area=et["area"],
        timezone_str=tz_str,
        name=str(ch.get("name", "default")),
        required_minutes=int(ch["required_hours"] * 60),
        continuous_only=bool(ch.get("continuous_only", False)),
        min_slot_minutes=int(ch.get("min_slot_minutes", 30)),
        max_price_eur=ceil_cents / 100.0 if ceil_cents is not None else None,
        preferred_window_start=ch.get("preferred_window_start", "00:00"),
        preferred_window_end=ch.get("preferred_window_end", "23:45"),
        preferred_window_any=(
            (ch.get("preferred_window_start") is None or str(ch.get("preferred_window_start", "")).lower() == "any") and
            (ch.get("preferred_window_end")   is None or str(ch.get("preferred_window_end",   "")).lower() == "any")
        ),
        window_start_any=(
            ch.get("preferred_window_start") is None or str(ch.get("preferred_window_start", "")).lower() == "any"
        ),
        window_end_any=(
            ch.get("preferred_window_end") is None or str(ch.get("preferred_window_end", "")).lower() == "any"
        ),
        schedule=ch.get("schedule") or [],
    )


def parse_configs(raw: dict) -> "list[Config]":
    """Validate raw config dict and return a list of typed Configs, one per profile.

    Raises ConfigError on any invalid field.  This is the single entry point
    for config validation — callers should not call validate_plan_config
    directly.
    """
    et = raw["entsoe"]
    profiles = raw["charging"]   # always a list after load_config normalisation

    configs = []
    for ch in profiles:
        # Build a synthetic full config for validate_plan_config
        validate_plan_config({"entsoe": et, "charging": ch})
        configs.append(_parse_one_profile(et, ch))

    return configs




# ===========================================================================
# ENTSO-E Transparency Platform API
# ===========================================================================

ENTSOE_API = "https://web-api.tp.entsoe.eu/api"


class PricesNotYetAvailable(Exception):
    """Raised when ENTSO-E returns no slots for the requested date,
    typically because next-day prices have not been published yet."""


class ConfigError(Exception):
    """Raised when the configuration file contains invalid or missing values."""

ENTSOE_AREAS = {
    "FI":  "10YFI-1--------U",
    "SE1": "10Y1001A1001A44P", "SE2": "10Y1001A1001A45N",
    "SE3": "10Y1001A1001A46L", "SE4": "10Y1001A1001A47J",
    "NO1": "10YNO-1--------2", "NO2": "10YNO-2--------T",
    "NO3": "10YNO-3--------J", "NO4": "10YNO-4--------9",
    "NO5": "10Y1001A1001A48H",
    "DK1": "10YDK-1--------W", "DK2": "10YDK-2--------M",
    "EE":  "10Y1001A1001A39I", "LV":  "10YLV-1--------W",
    "LT":  "10YLT-1--------W",
    "DE":  "10Y1001A1001A83F",
    "FR":  "10YFR-RTE------C", "NL":  "10YNL----------L",
    "BE":  "10YBE----------2", "AT":  "10YAT-APG------L",
    "PL":  "10YPL-AREA-----S",
}


def _resolve_area(area: str) -> str:
    if area in ENTSOE_AREAS:
        return ENTSOE_AREAS[area]
    if len(area) > 5:
        return area
    raise ValueError(
        f"Unknown area '{area}'. Use a short code (e.g. 'FI') or a full EIC code. "
        f"Known: {', '.join(ENTSOE_AREAS)}"
    )


def fetch_entsoe_prices(
    api_key: str,
    area: str,
) -> list[Slot]:
    """Fetch all available day-ahead prices in a single API call.

    Requests a wide window covering yesterday evening through the day after
    tomorrow, so every possible preferred window (same-day or overnight) has
    the slots it needs regardless of what time of day the script runs.

    Returns all non-past slots, renumbered. Callers filter by their window.
    Raises if no slots at all are returned (network/auth error).
    """
    eic   = _resolve_area(area)
    today = datetime.now(tz=timezone.utc).date()

    # from: (today - 1) 20:00 UTC — covers local midnight for UTC+3 (EEST)
    # to:   (today + 1) 23:00 UTC — covers tonight's overnight window morning
    #       (e.g. 22:00–06:30: tonight's evening + tomorrow morning at 04:30 UTC)
    period_start = (today - timedelta(days=1)).strftime("%Y%m%d2000")
    period_end   = (today + timedelta(days=1)).strftime("%Y%m%d2300")
    url = (
        f"{ENTSOE_API}"
        f"?documentType=A44"
        f"&in_Domain={eic}"
        f"&out_Domain={eic}"
        f"&periodStart={period_start}"
        f"&periodEnd={period_end}"
        f"&securityToken={api_key}"
    )
    log.info("Fetching ENTSO-E prices: area=%s", area)
    req = urllib.request.Request(url, headers={"Accept": "application/xml"})
    try:
        raw = _http_request_with_retry(req, timeout=20, retries=3, label="ENTSO-E")
    except Exception as e:
        log.error("ENTSO-E request failed after retries: %s", e)
        raise
    all_slots = _parse_entsoe_xml(raw, today, area)

    if not all_slots:
        raise PricesNotYetAvailable("No price slots returned from ENTSO-E.")

    # Return all non-past slots; each profile's window filter picks what it needs.
    now_utc = datetime.now(tz=timezone.utc)
    usable  = [s for s in all_slots if s.start >= now_utc]
    log.info("Fetched %d total slots, %d usable (not in the past)", len(all_slots), len(usable))

    return [Slot(start=s.start, end=s.end, duration_minutes=s.duration_minutes,
                 price_eur_kwh=s.price_eur_kwh, slot=i)
            for i, s in enumerate(sorted(usable, key=lambda x: x.start))]


def _xml_parse_root(xml_text: str):
    """Parse XML text and return (root, ns_uri). Raises on parse error or API error response."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        raise ValueError(f"ENTSO-E response is not valid XML: {e}\n{xml_text[:300]}")

    ns_uri = root.tag[1:root.tag.index("}")] if root.tag.startswith("{") else ""

    if "Acknowledgement_MarketDocument" in root.tag:
        reason = next(
            (elem.text or "" for elem in root.iter()
             if elem.tag.endswith("}text") or elem.tag == "text"),
            ""
        )
        raise ValueError(f"ENTSO-E returned an error: {reason or xml_text[:300]}")

    return root, ns_uri


def _xml_finders(ns_uri: str):
    """Return (find_all, find_text) helpers bound to the given namespace."""
    ns = {"ns": ns_uri} if ns_uri else {}

    def find_all(parent, tag):
        return parent.findall(f"ns:{tag}", ns) if ns_uri else parent.findall(tag)

    def find_text(parent, tag):
        return parent.findtext(f"ns:{tag}", namespaces=ns) if ns_uri else parent.findtext(tag)

    return find_all, find_text


def _xml_resolution_minutes(resolution_str: str) -> int:
    """Convert ENTSO-E resolution string (e.g. PT15M) to minutes."""
    if "15" in resolution_str:
        return 15
    if "30" in resolution_str:
        return 30
    return 60


def _xml_period_interval(period) -> tuple:
    """Return (period_start_utc, period_end_utc) from a Period element, or (None, None)."""
    interval = next(
        (c for c in period if c.tag.endswith("}timeInterval") or c.tag == "timeInterval"), None
    )
    if interval is None:
        return None, None
    start_str = interval.findtext("{*}start") or interval.findtext("start")
    end_str   = interval.findtext("{*}end")   or interval.findtext("end")
    if not start_str:
        return None, None
    start = datetime.fromisoformat(start_str.replace("Z", "+00:00"))
    end   = datetime.fromisoformat(end_str.replace("Z", "+00:00")) if end_str else None
    return start, end


def _xml_extract_points(period, find_all, find_text) -> dict[int, float]:
    """Return {position: price_eur_mwh} from Point elements in a Period."""
    explicit: dict[int, float] = {}
    for point in find_all(period, "Point"):
        pos_text   = find_text(point, "position")
        price_text = find_text(point, "price.amount")
        if pos_text is None or price_text is None:
            continue
        try:
            explicit[int(pos_text)] = float(price_text)
        except ValueError:
            continue
    return explicit


def _xml_parse_time_series(ts, find_all, find_text) -> list[Slot]:
    """Extract Slots from a single TimeSeries XML element."""
    period = next(
        (c for c in ts if c.tag.endswith("}Period") or c.tag == "Period"), None
    )
    if period is None:
        return []

    period_start_utc, period_end_utc = _xml_period_interval(period)
    if period_start_utc is None:
        return []

    slot_minutes = _xml_resolution_minutes(find_text(period, "resolution") or "PT60M")
    explicit     = _xml_extract_points(period, find_all, find_text)
    if not explicit:
        return []

    return _xml_interpolate_slots(explicit, period_start_utc, period_end_utc, slot_minutes)


def _xml_interpolate_slots(
    explicit: dict[int, float],
    period_start_utc: datetime,
    period_end_utc,
    slot_minutes: int,
) -> list[Slot]:
    """Expand sparse {position: price} dict into a full Slot list via forward-fill."""
    slots      = []
    last_price = explicit[min(explicit.keys())]
    for pos in range(1, max(explicit.keys()) + 1):
        if pos in explicit:
            last_price = explicit[pos]
        slot_start = period_start_utc + timedelta(minutes=slot_minutes * (pos - 1))
        slot_end   = slot_start + timedelta(minutes=slot_minutes)
        if period_end_utc and slot_end > period_end_utc:
            break
        slots.append(Slot(
            slot=0,
            start=slot_start,
            end=slot_end,
            duration_minutes=slot_minutes,
            price_eur_kwh=last_price / 1000.0,
        ))
    return slots


def _xml_deduplicate(slots: list[Slot]) -> list[Slot]:
    """Sort, deduplicate by start time, and assign sequential ordinals."""
    seen: set[datetime] = set()
    unique = []
    for s in sorted(slots, key=lambda x: x.start):
        if s.start not in seen:
            seen.add(s.start)
            unique.append(s)
    return [Slot(start=s.start, end=s.end, duration_minutes=s.duration_minutes,
                 price_eur_kwh=s.price_eur_kwh, slot=i)
            for i, s in enumerate(unique)]


def _parse_entsoe_xml(xml_text: str, target_date: date, area: str) -> list[Slot]:
    """Parse an ENTSO-E Publication_MarketDocument XML response into a Slot list."""
    root, ns_uri   = _xml_parse_root(xml_text)
    find_all, find_text = _xml_finders(ns_uri)

    raw: list[Slot] = []
    for ts in find_all(root, "TimeSeries"):
        raw.extend(_xml_parse_time_series(ts, find_all, find_text))

    unique = _xml_deduplicate(raw)

    if not unique:
        raise PricesNotYetAvailable(
            f"No price slots found for area={area} date={target_date}."
        )

    log.info("Fetched %d price slots for %s (resolution: %d-minute)",
             len(unique), target_date, unique[0].duration_minutes)
    return unique


# ===========================================================================
# Timezone helpers
# ===========================================================================

class TzInfo:
    """Resolved timezone: name (for display) and ZoneInfo object (for conversion)."""
    __slots__ = ("name", "zone")

    def __init__(self, name: str, zone):
        self.name = name
        self.zone = zone

    def __repr__(self) -> str:
        return f"TzInfo({self.name!r})"


def _resolve_tz(config_tz: str | None, ref_date: date) -> "TzInfo":
    """Return a TzInfo for the given timezone config string.

    config_tz may be an explicit IANA name (e.g. "Europe/Helsinki") or None
    to auto-detect from the host system. Falls back to UTC if nothing can be
    determined. An explicit but unrecognised name raises ConfigError.
    """
    # Determine timezone name
    tz_name = config_tz
    if not tz_name:
        try:
            with open("/etc/timezone") as f:
                tz_name = f.read().strip() or None
        except OSError:
            pass
    if not tz_name:
        try:
            import os as _os
            link = _os.path.realpath("/etc/localtime")
            marker = "/zoneinfo/"
            idx = link.find(marker)
            if idx != -1:
                tz_name = link[idx + len(marker):] or None
        except Exception:
            pass
    if not tz_name:
        try:
            off = datetime.now().astimezone().utcoffset()
            if off is not None:
                total_min = int(off.total_seconds() / 60)
                sign = "+" if total_min >= 0 else "-"
                h, m = divmod(abs(total_min), 60)
                tz_name = f"UTC{sign}{h:02d}:{m:02d}"
        except Exception:
            pass
    tz_name = tz_name or "UTC"

    # Resolve to a ZoneInfo object.
    # If the caller explicitly supplied a timezone name (config_tz) and it is
    # not recognised, that is a hard configuration error — silently falling back
    # to UTC would produce quietly wrong window times.
    # Auto-detected names (config_tz is None) are allowed to fall back to UTC.
    try:
        tz = ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, KeyError):
        if config_tz:
            raise ConfigError(
                f"charging.timezone={config_tz!r} is not a recognised IANA timezone name "
                f"(e.g. 'Europe/Helsinki', 'UTC'). Check https://en.wikipedia.org/wiki/List_of_tz_database_time_zones"
            )
        log.warning("Could not resolve system timezone %r — falling back to UTC.", tz_name)
        tz_name = "UTC"
        tz = timezone.utc

    return TzInfo(tz_name, tz)


# ===========================================================================
# Charging window selection
# ===========================================================================

def select_charging_windows(
    prices: list[Slot],
    required_minutes: int,
    continuous_only: bool = False,
    max_price: Optional[float] = None,
    min_slot_minutes: int = 15,
) -> list[Slot]:
    if not prices:
        return []

    slot_dur = prices[0].duration_minutes
    if required_minutes % slot_dur != 0:
        required_minutes = ((required_minutes + slot_dur - 1) // slot_dur) * slot_dur
        log.warning("required_minutes rounded up to %d to align with %d-minute slots.",
                    required_minutes, slot_dur)

    n_slots = required_minutes // slot_dur
    log.info("Selecting %d slots × %d min = %d min of charging", n_slots, slot_dur, required_minutes)

    candidates = prices
    if max_price is not None:
        candidates = [p for p in prices if p.price_eur_kwh <= max_price]
        if len(candidates) < n_slots:
            log.warning("Only %d slots below price ceiling %.2f c/kWh (needed %d).",
                        len(candidates), max_price * 100, n_slots)

    if not candidates:
        log.error("No candidate slots available for charging!")
        return []

    if continuous_only:
        return _best_continuous_window(candidates, prices, n_slots)

    min_slots_per_block = max(1, (min_slot_minutes + slot_dur - 1) // slot_dur)
    effective_min_minutes = min_slots_per_block * slot_dur
    if effective_min_minutes != min_slot_minutes:
        log.info("min_slot_minutes rounded up to %d to align with %d-minute slots.",
                 effective_min_minutes, slot_dur)

    if min_slots_per_block <= 1:
        selected = sorted(candidates, key=lambda x: (x.price_eur_kwh, -x.start.timestamp()))[:n_slots]
        selected.sort(key=lambda x: x.start)
        return selected

    return _select_with_min_block(candidates, n_slots, min_slots_per_block)


def _select_with_min_block(
    candidates: list[Slot],
    n_slots: int,
    min_slots_per_block: int,
) -> list[Slot]:
    """Select n_slots from candidates such that every contiguous block of selected
    slots is at least min_slots_per_block long, minimising total price.

    Strategy: enumerate all valid consecutive blocks (runs of ≥ min_slots_per_block
    consecutive slots), then greedily pick the cheapest individual slots that belong
    to at least one valid block, using a block-aware repair loop to ensure no
    orphaned short blocks remain.
    """
    slot_dur = candidates[0].duration_minutes if candidates else 15

    # Build a time-ordered list and a lookup by start time
    ordered = sorted(candidates, key=lambda x: x.start)
    slot_by_start = {s.start: s for s in ordered}

    # Identify all maximal runs of consecutive slots
    runs: list[list[Slot]] = []
    current_run: list[Slot] = []
    for s in ordered:
        if current_run and s.start != current_run[-1].end:
            runs.append(current_run)
            current_run = []
        current_run.append(s)
    if current_run:
        runs.append(current_run)

    # From each run, enumerate all sub-blocks of size >= min_slots_per_block
    # ranked by average price — these are the valid atomic units we can pick
    valid_blocks: list[list[Slot]] = []
    for run in runs:
        for size in range(min_slots_per_block, len(run) + 1):
            for start_i in range(len(run) - size + 1):
                valid_blocks.append(run[start_i:start_i + size])

    # Sort blocks by average price ascending, then by start time descending
    # (prefer later blocks as tiebreaker — consistent with single-slot behaviour)
    valid_blocks.sort(key=lambda b: (sum(s.price_eur_kwh for s in b) / len(b), -b[0].start.timestamp(), -len(b)))

    # Greedy block selection: pick cheapest blocks until quota met, no overlap
    selected_set: set = set()
    for block in valid_blocks:
        if len(selected_set) >= n_slots:
            break
        # Skip if any slot in this block is already selected (overlap)
        if any(s.start in selected_set for s in block):
            continue
        # Only add as many slots as we still need
        remaining = n_slots - len(selected_set)
        if len(block) <= remaining:
            for s in block:
                selected_set.add(s.start)
        else:
            # Block is larger than remaining quota — take the latest consecutive
            # sub-block of the required size (consistent with latest-preferred tiebreaker)
            sub_size = remaining
            if sub_size >= min_slots_per_block:
                sub = block[-sub_size:]  # take latest slots from the block
                for s in sub:
                    selected_set.add(s.start)

    result = sorted([slot_by_start[k] for k in selected_set], key=lambda x: x.start)
    final_blocks  = _group_continuous(result)
    min_block_min = min_slots_per_block * slot_dur
    short = [b for b in final_blocks if len(b) < min_slots_per_block]
    if short:
        log.warning("%d block(s) still shorter than %d min — not enough eligible slots.",
                    len(short), min_block_min)
    else:
        log.info("All %d block(s) meet the minimum block length of %d min.",
                 len(final_blocks), min_block_min)
    return result


def _best_continuous_window(
    candidates: list[Slot], all_prices: list[Slot], n_slots: int
) -> list[Slot]:
    """Return the cheapest continuous run of n_slots from all_prices where every slot is a candidate.

    "Continuous" means temporally adjacent — each slot ends exactly when the next
    begins. List-index adjacency is not sufficient; there may be time gaps in the
    list (e.g. overnight windows whose inside slots span an evening and a morning
    with a daytime gap in between).

    If no fully-eligible n_slots window exists within candidates (e.g. because
    next-day prices are not published yet and only part of the window is available),
    returns the longest continuous run available within candidates rather than
    falling outside the window. Never returns slots outside the candidate set.
    """
    slots = all_prices
    candidate_starts: set[datetime] = {s.start for s in candidates}

    def is_continuous(window: list[Slot]) -> bool:
        return all(window[j].end == window[j + 1].start for j in range(len(window) - 1))

    # First pass: find cheapest full n_slots continuous window within candidates
    best_avg   = float("inf")
    best_start = None
    for i in range(len(slots) - n_slots, -1, -1):
        window = slots[i:i + n_slots]
        if not is_continuous(window):
            continue
        if all(s.start in candidate_starts for s in window):
            avg = sum(s.price_eur_kwh for s in window) / n_slots
            if avg <= best_avg:
                best_avg   = avg
                best_start = i

    if best_start is not None:
        return slots[best_start:best_start + n_slots]

    # No full window available — return the longest continuous run within candidates.
    # This happens when part of the window has no prices yet (e.g. overnight window
    # where next-day morning prices aren't published). Never go outside candidates.
    log.warning(
        "No continuous %d-slot window found within candidate set — "
        "returning longest available continuous block within window.",
        n_slots,
    )
    candidate_slots = sorted(candidates, key=lambda s: s.start)
    best_block: list[Slot] = []
    current:    list[Slot] = []
    for s in candidate_slots:
        if current and s.start != current[-1].end:
            current = []
        current.append(s)
        if len(current) > len(best_block):
            best_block = list(current)
    return best_block


def _hhmm_to_utc(hhmm: str, ref_date: date, tz) -> datetime:
    """Convert a local HH:MM string on ref_date to UTC using a ZoneInfo object.

    DST-safe: ZoneInfo resolves the correct offset for the exact date/time,
    so windows set to e.g. "03:00" on a DST transition day are handled correctly.
    Returns a UTC datetime with tzinfo=timezone.utc.
    """
    h, m = map(int, hhmm.split(":"))
    local_dt = datetime(ref_date.year, ref_date.month, ref_date.day,
                        h, m, tzinfo=tz)
    return local_dt.astimezone(timezone.utc)


def _is_overnight(start_hhmm: str, end_hhmm: str) -> bool:
    """Return True if start > end, indicating an overnight window (e.g. 22:00–06:30)."""
    sh, sm = map(int, start_hhmm.split(":"))
    eh, em = map(int, end_hhmm.split(":"))
    return (sh * 60 + sm) > (eh * 60 + em)


def _resolve_window_utc(
    start_hhmm: str,
    end_hhmm: str,
    tz,
    _anchor_date: Optional[date] = None,
) -> tuple[datetime, datetime]:
    """Resolve preferred window HH:MM strings to UTC datetimes.

    Determines the anchor date automatically from the clock:
    - Same-day windows (start < end): tomorrow if window start has passed, today otherwise.
    - Overnight windows (start > end, e.g. 22:00–06:30): today if the window
      start is still in the future; tomorrow otherwise.

    _anchor_date overrides the clock-based anchor (used in tests only).
    For overnight windows, win_end_utc is resolved against anchor + 1 day.
    """
    if _anchor_date is not None:
        anchor = _anchor_date
        overnight = _is_overnight(start_hhmm, end_hhmm)
    else:
        now_utc   = datetime.now(tz=timezone.utc)
        local_now = now_utc.astimezone(tz)
        overnight = _is_overnight(start_hhmm, end_hhmm)
        if overnight:
            sh, sm = map(int, start_hhmm.split(":"))
            win_start_today = local_now.replace(hour=sh, minute=sm, second=0, microsecond=0)
            anchor = local_now.date() if local_now < win_start_today                      else (local_now + timedelta(days=1)).date()
            log.info("Overnight window %s–%s: anchor=%s (local now %s)",
                     start_hhmm, end_hhmm, anchor, local_now.strftime("%H:%M"))
        else:
            # Same-day window: use today if the window start is still in the
            # future today, otherwise plan for tomorrow.
            sh, sm = map(int, start_hhmm.split(":"))
            win_start_today = local_now.replace(hour=sh, minute=sm, second=0, microsecond=0)
            anchor = local_now.date() if local_now < win_start_today                      else (local_now + timedelta(days=1)).date()

    win_start_utc = _hhmm_to_utc(start_hhmm, anchor, tz)
    win_end_utc   = _hhmm_to_utc(end_hhmm, anchor + timedelta(days=1), tz) if overnight                     else _hhmm_to_utc(end_hhmm, anchor, tz)
    return win_start_utc, win_end_utc


def filter_preferred_window(
    prices: list[Slot],
    win_start_utc: datetime,
    win_end_utc: datetime,
    window_start_local: str,
    window_end_local: str,
) -> tuple[list[Slot], list[Slot]]:
    """Split prices into slots inside and outside the preferred window.

    win_start_utc / win_end_utc are the pre-resolved UTC bounds.
    window_start_local / window_end_local are passed only for log messages.

    For overnight windows win_end_utc > win_start_utc holds (end is next day),
    so the same start <= slot < end condition works without special-casing.
    """
    inside, outside = [], []
    for slot in prices:
        if slot.start >= win_start_utc and slot.end <= win_end_utc:
            inside.append(slot)
        else:
            outside.append(slot)

    log.info("Preferred window %s–%s local: %d slots inside, %d outside",
             window_start_local, window_end_local, len(inside), len(outside))

    return inside, outside


def _select_spillover(
    outside: list[Slot],
    selected: list[Slot],
    continuous_only: bool,
    win_end_utc: datetime,
    win_end_local: str,
    required_minutes: int,
    remaining: int,
    max_price_eur: Optional[float],
    min_slot_minutes: int,
    all_prices: list[Slot],
) -> list[Slot]:
    """
    Select spillover slots to cover `remaining` minutes when the preferred
    window alone cannot satisfy `required_minutes`.

    Rules:
    - Never spill after win_end_utc.
    - continuous_only: extend the existing block leftward (earlier slots only).
    - non-continuous: pick cheapest slots from eligible outside slots.

    win_end_local is passed only for log messages.
    """
    # Use start datetime as identity key (slot ordinal is only set by fetch_entsoe_prices).
    selected_starts = {s.start for s in selected}
    candidates = [s for s in outside if s.start not in selected_starts]

    # Restrict to slots before the window end.
    candidates = [s for s in candidates if s.end <= win_end_utc]
    log.info(
        "Spillover restricted to slots before window end %s (%d candidates)",
        win_end_local, len(candidates),
    )

    if continuous_only:
        if selected:
            # Extend leftward: take the slots immediately before the block start.
            block_start_utc = min(s.start for s in selected)
            before = sorted(
                [s for s in candidates if s.end <= block_start_utc],
                key=lambda x: x.start,
                reverse=True,
            )
            slot_dur = all_prices[0].duration_minutes if all_prices else 15
            n_extra  = (remaining + slot_dur - 1) // slot_dur
            spillover = list(reversed(before[:n_extra]))
            log.info(
                "continuous_only spill: extending block %d min earlier (%d slots)",
                remaining, len(spillover),
            )
            return spillover
        # No prior selected block — fall back to best continuous block.
        # Apply the same win_end_utc restriction used by the non-empty path so
        # the result never spills after the preferred window end.
        filtered = [
            s for s in all_prices
            if s.end <= win_end_utc
            and (max_price_eur is None or s.price_eur_kwh <= max_price_eur)
        ]
        slot_dur = all_prices[0].duration_minutes if all_prices else 15
        n_slots  = (remaining + slot_dur - 1) // slot_dur
        return _best_continuous_window(filtered, all_prices, n_slots)

    return select_charging_windows(
        candidates,
        required_minutes=remaining,
        continuous_only=False,
        max_price=max_price_eur,
        min_slot_minutes=min_slot_minutes,
    )


def _group_continuous(slots: list[Slot]) -> list[list[Slot]]:
    """Split a sorted list of slots into groups of adjacent (continuous) slots."""
    if not slots:
        return []
    groups: list[list[Slot]] = []
    block = [slots[0]]
    for slot in slots[1:]:
        if slot.start == block[-1].end:
            block.append(slot)
        else:
            groups.append(block)
            block = [slot]
    groups.append(block)
    return groups


def close_gap_merge(
    selected: list[Slot],
    all_prices: list[Slot],
    min_slot_minutes: int,
    required_minutes: int,
) -> tuple[list[Slot], set]:
    """
    If two selected blocks are separated by a gap smaller than min_slot_minutes,
    bridge the gap by including those intervening slots, then trim endpoint slots
    from the merged block until total selected minutes equals required_minutes again.

    Trim rule — on each iteration compare the front (earliest) and back (latest)
    slots of the merged block:
      - Drop the back slot if it is strictly more expensive than the front.
      - Otherwise drop the front slot (cheaper or equal price).
    Tiebreaking by dropping the front pushes the charging window as late as
    possible, closest to the departure time.

    Returns (slots, merged_starts) where merged_starts is a set of UTC start
    datetimes for windows that were produced by a gap merge.
    """
    if not selected:
        return selected, set()

    slots = sorted(selected, key=lambda x: x.start)
    price_map = {s.start: s for s in all_prices}
    merged_starts: set = set()

    changed = True
    while changed:
        changed = False
        groups = _group_continuous(slots)
        for i in range(len(groups) - 1):
            gap_start = groups[i][-1].end
            gap_end   = groups[i + 1][0].start
            gap_min   = int((gap_end - gap_start).total_seconds() / 60)
            if 0 < gap_min < min_slot_minutes:
                # Collect gap slots from all_prices
                gap_slots = [
                    price_map[s.start]
                    for s in all_prices
                    if gap_start <= s.start < gap_end and s.start in price_map
                ]
                if not gap_slots:
                    continue
                # Merge the two blocks + gap into one
                merged_block = sorted(
                    groups[i] + gap_slots + groups[i + 1],
                    key=lambda x: x.start,
                )
                # Trim endpoint slots until total selected equals required_minutes.
                # extra_minutes = how much over required we are after bridging the gap.
                # merged_block already contains all slots including the gap, so summing
                # it gives the exact post-bridge total without double-counting.
                other_total   = sum(
                    s.duration_minutes
                    for g in (groups[:i] + groups[i + 2:])
                    for s in g
                )
                current_total = sum(s.duration_minutes for s in merged_block)
                extra_minutes = (current_total + other_total) - required_minutes
                log.info(
                    "Gap of %d min between %s and %s is below min_slot_minutes=%d — merging blocks.",
                    gap_min,
                    gap_start.isoformat(), gap_end.isoformat(),
                    min_slot_minutes,
                )
                # Trim one slot per iteration from whichever endpoint is more expensive.
                # On a price tie always drop the front (earlier) slot — this pushes
                # the charging window as late as possible, closest to departure time.
                while extra_minutes > 0 and len(merged_block) > 1:
                    front = merged_block[0]
                    back  = merged_block[-1]
                    # Drop back only if it is strictly more expensive than front.
                    if back.price_eur_kwh > front.price_eur_kwh:
                        log.info(
                            "Trimming trailing slot %s (%.4f €/kWh) — more expensive than front (%.4f)",
                            back.start.isoformat(), back.price_eur_kwh, front.price_eur_kwh,
                        )
                        merged_block   = merged_block[:-1]
                        extra_minutes -= back.duration_minutes
                    else:
                        log.info(
                            "Trimming leading slot %s (%.4f €/kWh)%s",
                            front.start.isoformat(), front.price_eur_kwh,
                            " — tiebreak" if back.price_eur_kwh == front.price_eur_kwh else "",
                        )
                        merged_block   = merged_block[1:]
                        extra_minutes -= front.duration_minutes

                merged_starts.add(merged_block[0].start)

                # Rebuild slots list with the merged block in place
                other_groups = groups[:i] + [merged_block] + groups[i + 2:]
                slots = sorted(
                    [s for g in other_groups for s in g],
                    key=lambda x: x.start,
                )
                changed = True
                break  # restart loop with updated groups

    return slots, merged_starts

def merge_continuous_slots(slots: list[Slot]) -> list[tuple[datetime, datetime]]:
    if not slots:
        return []
    groups = _group_continuous(sorted(slots, key=lambda x: x.start))
    return [(g[0].start, g[-1].end) for g in groups]


# ===========================================================================
# Plan file — serialise / deserialise
# ===========================================================================

PLAN_VERSION = 1


@dataclass
class PlanParams:
    """All inputs needed to build a charging plan dict."""
    target_date:            date
    area:                   str
    price_source:           str
    all_prices:             list[Slot]
    selected:               list[Slot]
    windows:                list
    required_minutes:       int
    tz:                     object          # ZoneInfo or timezone.utc
    timezone_name:          str
    preferred_window_start: str
    preferred_window_end:   str
    max_price_eur:          Optional[float] = None
    min_slot_minutes:       int             = 30
    continuous_only:        bool            = False


def build_plan(p: PlanParams) -> dict:
    """
    Build a plan dict that can be serialised to JSON, reviewed, and edited.

    Structure:
    {
      "version": 1,
      "date": "2026-03-14",
      "area": "FI",
      "price_source": "ENTSO-E",
      "timezone": "Europe/Helsinki",
      "utc_offset_hours": 2,
      "price_stats": { "min": …, "max": …, "avg": … },
      "required_minutes": 240,
      "total_minutes": 240,
      "avg_price_cents_kwh": 1.84,
      "windows": [
        { "start": "01:00", "end": "04:00",
          "duration_minutes": 180, "avg_price_cents_kwh": 1.84 }
      ]
    }
    """
    # Use selected prices as fallback if all_prices is somehow empty
    stat_source = p.all_prices or p.selected
    all_prices_eur = [s.price_eur_kwh for s in stat_source]
    if all_prices_eur:
        price_stats = {
            "min_cents_kwh":  round(min(all_prices_eur) * 100, 4),
            "max_cents_kwh":  round(max(all_prices_eur) * 100, 4),
            "avg_cents_kwh":  round(sum(all_prices_eur) / len(all_prices_eur) * 100, 4),
        }
    else:
        price_stats = {"min_cents_kwh": 0.0, "max_cents_kwh": 0.0, "avg_cents_kwh": 0.0}

    win_list = []
    for start_utc, end_utc in sorted(p.windows, key=lambda w: w[0]):
        start_local = start_utc.astimezone(p.tz)
        end_local   = end_utc.astimezone(p.tz)
        dur = int((end_utc - start_utc).total_seconds() / 60)
        block_slots = [s for s in p.selected if start_utc <= s.start < end_utc]
        avg_p = (sum(s.price_eur_kwh for s in block_slots) / len(block_slots) * 100
                 if block_slots else 0.0)
        win_list.append({
            "start": start_local.strftime("%H:%M"),
            "end":   end_local.strftime("%H:%M"),
            "duration_minutes": dur,
            "avg_price_cents_kwh": round(avg_p, 4),
        })

    total_min = sum(s.duration_minutes for s in p.selected)
    sel_prices = [s.price_eur_kwh for s in p.selected]
    overall_avg = (sum(sel_prices) / len(sel_prices) * 100) if sel_prices else 0.0

    # Build price_slots — all available slots with charging and optimal flags
    selected_starts = {s.start for s in p.selected}

    # Compute optimal slots: cheapest possible ignoring window constraints only.
    # Respects continuous_only and min_slot_minutes so comparison is fair.
    optimal_selected = select_charging_windows(
        p.all_prices,
        required_minutes=p.required_minutes,
        continuous_only=p.continuous_only,
        max_price=p.max_price_eur,
        min_slot_minutes=p.min_slot_minutes,
    )
    optimal_starts = {s.start for s in optimal_selected}
    opt_prices = [s.price_eur_kwh for s in optimal_selected]
    avg_optimal = round(sum(opt_prices) / len(opt_prices) * 100, 4) if opt_prices else 0.0

    price_slots = [
        {
            "start_utc":         s.start.isoformat(),
            "price_cents_kwh":   round(s.price_eur_kwh * 100, 4),
            "charging":          s.start in selected_starts,
            "optimal":           s.start in optimal_starts,
        }
        for s in sorted(p.all_prices, key=lambda x: x.start)
    ]

    return {
        "version":                PLAN_VERSION,
        "date":                   str(p.target_date),
        "area":                   p.area,
        "price_source":           p.price_source,
        "timezone":               p.timezone_name,
        "utc_offset_hours":       int(
            datetime(p.target_date.year, p.target_date.month, p.target_date.day,
                     12, 0, tzinfo=p.tz).utcoffset().total_seconds() / 3600
        ),
        "price_stats":            price_stats,
        "required_minutes":       p.required_minutes,
        "total_minutes":          total_min,
        "avg_price_cents_kwh":    round(overall_avg, 4),
        "avg_optimal_price_cents_kwh": avg_optimal,
        "preferred_window_start": p.preferred_window_start,
        "preferred_window_end":   p.preferred_window_end,
        "windows":                win_list,
        "window_starts_utc":      [w[0].isoformat() for w in p.windows],
        "window_ends_utc":        [w[1].isoformat() for w in p.windows],
        "price_slots":            price_slots,
        "ocpp_charging_profile":  build_ocpp_charging_profile(
            {"window_starts_utc": [w[0].isoformat() for w in p.windows],
             "window_ends_utc":   [w[1].isoformat() for w in p.windows]},
        ),
    }


def build_ocpp_charging_profile(
    plan: dict,
    charging_rate_unit: str = "W",
    max_charging_rate: float = 11000.0,
    profile_id: int = 1,
    stack_level: int = 0,
    ocpp_version: str = "1.6",
) -> dict:
    """Build an OCPP-compatible ChargingProfile from a plan.

    Compatible with OCPP 1.6, 2.0.1, and 2.1. The only structural difference
    between versions is that OCPP 2.1 renames `chargingProfileId` to `id` in
    ChargingProfileType, and adds an `id` field to ChargingScheduleType.
    All other fields — startPeriod, limit, startSchedule, chargingRateUnit,
    validFrom/validTo — are identical across all three versions.

    Produces an Absolute TxDefaultProfile. Charging windows run at
    max_charging_rate; gaps between windows are set to 0 so the vehicle does
    not charge outside the planned slots.

    Args:
        plan:               plan dict as returned by build_plan()
        charging_rate_unit: "W" (watts, typical for DC / 3-phase AC) or
                            "A" (amps per phase, typical for single-phase AC)
        max_charging_rate:  limit during charging periods (W or A depending
                            on charging_rate_unit). Default 11000 W = 11 kW.
        profile_id:         profile identifier (must be unique on the charger)
        stack_level:        higher values take precedence; 0 = lowest priority
        ocpp_version:       "1.6", "2.0.1", or "2.1". OCPP 1.6 uses
                            `chargingProfileId`; 2.0.1 and 2.1 use `id`.

    Returns a dict matching the OCPP ChargingProfile structure, ready to be
    sent as csChargingProfiles in a SetChargingProfile.req message.
    """
    starts = [datetime.fromisoformat(s) for s in plan.get("window_starts_utc", [])]
    ends   = [datetime.fromisoformat(s) for s in plan.get("window_ends_utc",   [])]

    if not starts:
        log.warning("build_ocpp_charging_profile: no windows in plan, returning empty profile")
        return {}

    schedule_start = starts[0]
    schedule_end   = ends[-1]
    duration_sec   = int((schedule_end - schedule_start).total_seconds())

    # Build ChargingSchedulePeriod list.
    # startPeriod is seconds from schedule_start; periods alternate 0 ↔ max.
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
            "limit": float(max_charging_rate),
        })
        cursor = win_end

    if cursor < schedule_end:
        periods.append({
            "startPeriod": int((cursor - schedule_start).total_seconds()),
            "limit": 0.0,
        })

    # OCPP 2.0.1 and 2.1 both use "id" instead of "chargingProfileId", and
    # require an "id" field on ChargingScheduleType. OCPP 1.6 uses
    # "chargingProfileId" and has no id on ChargingSchedule.
    is_v2 = not ocpp_version.startswith("1.6")
    profile_id_key = "id" if is_v2 else "chargingProfileId"

    schedule: dict = {
        "startSchedule":          schedule_start.isoformat(),
        "duration":               duration_sec,
        "chargingRateUnit":       charging_rate_unit,
        "chargingSchedulePeriod": periods,
    }
    if is_v2:
        schedule["id"] = profile_id   # ChargingScheduleType.id in 2.0.1 and 2.1

    return {
        profile_id_key:           profile_id,
        "stackLevel":             stack_level,
        "chargingProfilePurpose": "TxDefaultProfile",
        "chargingProfileKind":    "Absolute",
        "validFrom":              schedule_start.isoformat(),
        "validTo":                schedule_end.isoformat(),
        "chargingSchedule":       schedule,
    }


def save_plan(plan: dict, path: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(plan, f, indent=2)
    log.info("Plan saved to %s", path)


# ===========================================================================
# Visualisation
# ===========================================================================

# ANSI colour helpers (auto-disabled when not a TTY or on Windows)
_USE_COLOR = sys.stdout.isatty() and sys.platform != "win32"

def _c(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _USE_COLOR else text

def _green(t):  return _c("32", t)
def _yellow(t): return _c("33", t)
def _red(t):    return _c("31", t)
def _bold(t):   return _c("1",  t)
def _dim(t):    return _c("2",  t)



def _window_bar(start_hm: str, end_hm: str, dur_min: int, avg_c: float,
                min_c: float, max_c: float) -> str:
    """Single-line visual block for a charging window."""
    ratio = (avg_c - min_c) / (max_c - min_c + 1e-9)
    if ratio < 0.33:
        col = "32"
    elif ratio < 0.66:
        col = "33"
    else:
        col = "31"
    h, m = divmod(dur_min, 60)
    dur_str = f"{h}h{m:02d}m" if h else f"{m}m"
    bar_len = max(2, dur_min // 15)
    bar = _c(col + ";1", "█" * bar_len)
    return (f"    {_bold(start_hm)}–{_bold(end_hm)}  {bar}  "
            f"{_c(col, f'{avg_c:.2f} c€/kWh')}  {_dim(dur_str)}")


def print_plan_summary(plan: dict, all_prices: list[Slot]) -> None:
    """Print a rich, coloured plan summary to stdout."""
    W   = 66
    ps  = plan["price_stats"]
    req = plan["required_minutes"]
    tot = plan["total_minutes"]
    avg = plan["avg_price_cents_kwh"]
    wins = plan["windows"]

    min_c = ps["min_cents_kwh"]
    max_c = ps["max_cents_kwh"]

    print()
    print(_bold("  " + "═" * W))
    print(_bold("  Charging Planner"))
    print(_bold("  " + "═" * W))
    print(f"  {_dim('Date')}      {_bold(plan['date'])}   "
          f"{_dim('Area')} {_bold(plan['area'])}   "
          f"{_dim('Source')} {plan['price_source']}")
    tz_offset_str = f"(UTC{plan['utc_offset_hours']:+d})"
    print(f"  {_dim('Timezone')}  {plan['timezone']} {_dim(tz_offset_str)}")
    print()

    # Price summary bar
    avg_mkt_str = f"{ps['avg_cents_kwh']:.2f}"
    print(f"  {_dim('Market prices')}   "
          f"{_green(f'{min_c:.2f}')} {_dim('min')}  "
          f"{_yellow(avg_mkt_str)} {_dim('avg')}  "
          f"{_red(f'{max_c:.2f}')} {_dim('max')}  "
          f"{_dim('c€/kWh')}")
    print()

    # Charging summary
    savings_pct = (1 - avg / ps["avg_cents_kwh"]) * 100 if ps["avg_cents_kwh"] else 0
    sav_str = (_green(f"  ↓ {savings_pct:.0f}% below market avg") if savings_pct > 5
               else _dim(f"  ≈ near market avg"))
    print(f"  {_bold('Scheduled')}  {tot} min of {req} min required{sav_str}")
    print(f"  {_bold('Avg price')}  {_yellow(f'{avg:.2f} c€/kWh')}")
    print()

    if wins:
        print(f"  {_bold(f'Charging windows ({len(wins)}):')} ")
        for w in wins:
            print(_window_bar(w["start"], w["end"],
                              w["duration_minutes"], w["avg_price_cents_kwh"],
                              min_c, max_c))
        print()
    else:
        print(f"  {_red('✗ No windows selected')}")
        print()

    print(_bold("  " + "═" * W))
    print()





# ===========================================================================
# GitHub Actions job summary
# ===========================================================================


def _gha_fmt_hours(minutes: int) -> str:
    """Format minutes as a compact hours string, e.g. 90 → '1h30min', 120 → '2h'."""
    h, m = divmod(minutes, 60)
    return f"{h}h{m:02d}min" if m else f"{h}h"


def _gha_summary_header(plan: dict) -> list[str]:
    """Return markdown lines for the run header table."""
    ps = plan["price_stats"]
    return [
        f"## ✅ Charging Plan — {plan['date']}", "",
        "| | |", "|---|---|",
        f"| **Date** | {plan['date']} |",
        f"| **Area** | {plan['area']} |",
        f"| **Source** | {plan['price_source']} |",
        f"| **Timezone** | {plan['timezone']} (UTC{plan['utc_offset_hours']:+d}) |",
        f"| **Market prices** | {ps['min_cents_kwh']:.2f} – {ps['max_cents_kwh']:.2f} c€/kWh"
        f" (avg {ps['avg_cents_kwh']:.2f}) |",
        "",
    ]


def _gha_summary_profile(plan: dict, market_avg: float) -> list[str]:
    """Return markdown lines for one charging profile section."""
    profile = plan.get("profile", "default")
    req, tot, avg = plan["required_minutes"], plan["total_minutes"], plan["avg_price_cents_kwh"]
    wins = plan["windows"]
    savings_pct = (1 - avg / market_avg) * 100 if market_avg else 0

    incomplete = " ⚠️ **charge plan not possible**" if tot < req else ""
    md = [
        f"### {profile}", "",
        f"| **Required** | {_gha_fmt_hours(req)} |",
        f"| **Scheduled** | {_gha_fmt_hours(tot)}{incomplete} |",
        f"| **Avg price** | **{avg:.2f} c€/kWh**"
        f" ({abs(savings_pct):.0f}% {'below' if savings_pct >= 0 else 'above'} market avg) |",
        "",
    ]
    if wins:
        md += ["| # | Start | End | Duration | Avg price |", "|---|---|---|---|---|"]
        for i, w in enumerate(wins, 1):
            md.append(
                f"| {i} | {w['start']} | {w['end']} | "
                f"{w['duration_minutes']} min | {w['avg_price_cents_kwh']:.2f} c€/kWh |"
            )
        md.append("")
    else:
        md += ["_No windows selected._", ""]
    return md


def write_gha_summary(plans: "list[dict]",
                      skipped: "list[str] | None" = None) -> None:
    """Write a combined GitHub Actions job summary for all profiles."""
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not summary_path:
        return

    first       = plans[0]
    market_avg  = first["price_stats"]["avg_cents_kwh"]
    md          = _gha_summary_header(first)
    for plan in plans:
        md += _gha_summary_profile(plan, market_avg)

    if skipped:
        md += [
            "### ⏳ Skipped profiles", "",
            "Prices not yet published for the following profiles — "
            "they will be planned in tomorrow's run:",
            "",
        ]
        for name in skipped:
            md.append(f"- **{name}**")
        md.append("")

    try:
        with open(summary_path, "a", encoding="utf-8") as f:
            f.write("\n".join(md) + "\n")
    except OSError as exc:
        log.warning("Could not write GitHub Actions summary: %s", exc)


# ---------------------------------------------------------------------------
# cmd_plan helpers — each covers one named phase of the planning pipeline
# ---------------------------------------------------------------------------

def _fetch_prices(cfg: Config) -> tuple[list[Slot], str]:
    """Fetch all available ENTSO-E prices.  Exits the process on failure."""
    try:
        prices = fetch_entsoe_prices(cfg.api_key, cfg.area)
        return prices, "ENTSO-E"
    except PricesNotYetAvailable as exc:
        log.error("No prices available: %s", exc)
        sys.exit(1)
    except Exception as exc:
        log.error("ENTSO-E fetch failed: %s", exc)
        sys.exit(1)


def _check_window_coverage(
    inside: list[Slot],
    win_start_utc: datetime,
    win_end_utc: datetime,
    profile_name: str,
) -> None:
    """Raise PricesNotYetAvailable if fetched prices do not cover the window.

    Compares the duration of slots available inside the window against the
    full window length. If coverage is below 90%, prices for this window have
    not been published yet. The caller handles the exception per-profile so
    other profiles are not affected.
    """
    window_minutes  = int((win_end_utc - win_start_utc).total_seconds() / 60)
    covered_minutes = sum(s.duration_minutes for s in inside)
    coverage        = covered_minutes / window_minutes if window_minutes else 0.0

    if coverage < 0.90:
        raise PricesNotYetAvailable(
            f"Profile '{profile_name}': only {covered_minutes} of {window_minutes} min "
            f"covered in window {win_start_utc.strftime('%H:%M UTC')}–"
            f"{win_end_utc.strftime('%H:%M UTC')} "
            f"({coverage*100:.0f}% — prices not yet published). "
            f"ENTSO-E publishes next-day prices at ~12:00 UTC."
        )

    log.info("Profile '%s': window coverage %d/%d min (%.0f%%).",
             profile_name, covered_minutes, window_minutes, coverage * 100)



def _select_slots(
    cfg:              Config,
    candidate_prices: list[Slot],
    win_start_utc:    datetime,
    win_end_utc:      datetime,
    win_start_str:    str,
    win_end_str:      str,
) -> tuple[list[Slot], set]:
    """Run the full slot-selection pipeline and return selected slots.

    Pipeline:
      filter preferred window → greedy cheapest select → spillover fill
      → gap-merge (if enabled) → coverage warning
    """
    inside, outside = filter_preferred_window(
        candidate_prices,
        win_start_utc=win_start_utc,
        win_end_utc=win_end_utc,
        window_start_local=win_start_str,
        window_end_local=win_end_str,
    )

    _check_window_coverage(inside, win_start_utc, win_end_utc, cfg.name)

    selected = select_charging_windows(
        inside,
        required_minutes=cfg.required_minutes,
        continuous_only=cfg.continuous_only,
        max_price=cfg.max_price_eur,
        min_slot_minutes=cfg.min_slot_minutes,
    )

    remaining = cfg.required_minutes - sum(s.duration_minutes for s in selected)
    if remaining > 0 and outside:
        log.info("Filling %d min from outside preferred window.", remaining)
        spillover = _select_spillover(
            outside=outside,
            selected=selected,
            continuous_only=cfg.continuous_only,
            win_end_utc=win_end_utc,
            win_end_local=win_end_str,
            required_minutes=cfg.required_minutes,
            remaining=remaining,
            max_price_eur=cfg.max_price_eur,
            min_slot_minutes=cfg.min_slot_minutes,
            all_prices=candidate_prices,
        )
        selected = sorted(selected + spillover, key=lambda x: x.start)

    if not cfg.continuous_only:
        selected, _ = close_gap_merge(
            selected, candidate_prices, cfg.min_slot_minutes, cfg.required_minutes
        )

    if not selected:
        log.error("Profile '%s': no slots selected — check preferred window and price ceiling.",
                  cfg.name)
    else:
        scheduled_min = sum(s.duration_minutes for s in selected)
        if scheduled_min < cfg.required_minutes:
            log.warning(
                "⚠ Profile '%s': only %d min scheduled of %d min required. "
                "Window prices may all be above max_price_cents_kwh, or the window "
                "is too short for the required hours.",
                cfg.name, scheduled_min, cfg.required_minutes,
            )

    return selected


# ---------------------------------------------------------------------------
# Schedule window resolution
# ---------------------------------------------------------------------------

_DAY_NAMES = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


def _resolve_schedule_window(cfg: "Config", target_date: "date") -> tuple[str, str]:
    """Return (preferred_window_start, preferred_window_end) for target_date.

    Checks cfg.schedule entries in order, returning the first entry whose
    days list includes the weekday of target_date. Falls back to the
    top-level preferred_window_start / preferred_window_end if no entry matches.
    """
    day_name = _DAY_NAMES[target_date.weekday()]
    for entry in cfg.schedule:
        if day_name in (entry.get("days") or []):
            s = entry.get("preferred_window_start")
            e = entry.get("preferred_window_end")
            s_str = "any" if (s is None or str(s).lower() == "any") else str(s)
            e_str = "any" if (e is None or str(e).lower() == "any") else str(e)
            log.info("Profile '%s': using schedule entry for %s (%s–%s)", cfg.name, day_name, s_str, e_str)
            return s_str, e_str
    if cfg.preferred_window_any:
        return "any", "any"
    s_str = "any" if cfg.window_start_any else cfg.preferred_window_start
    e_str = "any" if cfg.window_end_any   else cfg.preferred_window_end
    return s_str, e_str


# ---------------------------------------------------------------------------
# Main planning command
# ---------------------------------------------------------------------------

def _plan_one_profile(
    cfg:          "Config",
    tz:           "TzInfo",
    all_prices:   "list[Slot]",
    price_source: str,
    output_dir:   str,
) -> dict:
    """Run selection and build a plan dict for a single profile."""
    log.info("=== Profile: %s ===", cfg.name)

    # Resolve window using top-level defaults first to determine plan_date,
    # then re-resolve with day-specific schedule entry if one matches.
    # If top-level window is "any", plan_date = today (tomorrow will be the target).
    def _resolve_any_window(start_str, end_str, anchor_date, tomorrow_date):
        """Resolve window bounds supporting 'any' on either or both sides."""
        s_any = str(start_str).lower() == "any"
        e_any = str(end_str).lower() == "any"
        now_utc = datetime.now(tz=timezone.utc)
        if s_any and e_any:
            return now_utc, max(s.end for s in all_prices) if all_prices else now_utc
        elif s_any:
            # any start = from now, fixed end
            _, end_utc = _resolve_window_utc(
                "00:00", end_str, tz.zone,
                _anchor_date=tomorrow_date if not _is_overnight("00:00", end_str) else anchor_date,
            )
            return now_utc, end_utc
        elif e_any:
            # fixed start, any end = from start until last available price
            start_utc, _ = _resolve_window_utc(
                start_str, "23:45", tz.zone,
                _anchor_date=anchor_date if _is_overnight(start_str, "23:45") else tomorrow_date,
            )
            return start_utc, max(s.end for s in all_prices) if all_prices else start_utc
        else:
            if _is_overnight(start_str, end_str):
                return _resolve_window_utc(start_str, end_str, tz.zone, _anchor_date=anchor_date)
            else:
                return _resolve_window_utc(start_str, end_str, tz.zone, _anchor_date=tomorrow_date)

    if cfg.preferred_window_any:
        plan_date = datetime.now(tz=timezone.utc).astimezone(tz.zone).date()
        win_start_utc = datetime.now(tz=timezone.utc)
        win_end_utc   = max(s.end for s in all_prices) if all_prices else win_start_utc
        win_start_str, win_end_str = "any", "any"
    else:
        win_start_utc, win_end_utc = _resolve_window_utc(
            cfg.preferred_window_start if not cfg.window_start_any else "00:00",
            cfg.preferred_window_end   if not cfg.window_end_any   else "23:45",
            tz.zone
        )
        plan_date = win_start_utc.astimezone(tz.zone).date()

    if cfg.schedule or cfg.preferred_window_any or cfg.window_start_any or cfg.window_end_any:
        tomorrow = plan_date + timedelta(days=1)
        win_start_str, win_end_str = _resolve_schedule_window(cfg, tomorrow)
        s_any = str(win_start_str).lower() == "any"
        e_any = str(win_end_str).lower() == "any"
        if s_any or e_any:
            win_start_utc, win_end_utc = _resolve_any_window(
                win_start_str, win_end_str, plan_date, tomorrow
            )
            log.info("Profile '%s': partial/full any window — %s – %s",
                     cfg.name, win_start_utc.isoformat(), win_end_utc.isoformat())
        elif _is_overnight(win_start_str, win_end_str):
            win_start_utc, win_end_utc = _resolve_window_utc(
                win_start_str, win_end_str, tz.zone, _anchor_date=plan_date,
            )
        else:
            win_start_utc, win_end_utc = _resolve_window_utc(
                win_start_str, win_end_str, tz.zone, _anchor_date=tomorrow,
            )
        plan_date = win_start_utc.astimezone(tz.zone).date()
    else:
        win_start_str, win_end_str = cfg.preferred_window_start, cfg.preferred_window_end

    log.info("Window UTC: %s – %s", win_start_utc.isoformat(), win_end_utc.isoformat())

    # plan_date: the local calendar date the window starts on
    plan_date = win_start_utc.astimezone(tz.zone).date()

    # display_prices: all usable slots from the script run onwards.
    # This is the full market the user could have chosen from at the time of planning.
    display_prices = all_prices

    earliest_useful  = win_start_utc - timedelta(minutes=cfg.required_minutes)
    candidate_prices = [s for s in all_prices if s.start >= earliest_useful]
    if trimmed := len(all_prices) - len(candidate_prices):
        log.info("Trimmed %d unreachable slots (earliest useful: %s UTC)",
                 trimmed, earliest_useful.strftime("%Y-%m-%d %H:%M"))

    selected = _select_slots(cfg, candidate_prices, win_start_utc, win_end_utc, win_start_str, win_end_str)
    windows = merge_continuous_slots(selected)

    plan = build_plan(PlanParams(
        target_date=plan_date,
        area=cfg.area,
        price_source=price_source,
        all_prices=display_prices,
        selected=selected,
        windows=windows,
        required_minutes=cfg.required_minutes,
        tz=tz.zone,
        timezone_name=tz.name,
        preferred_window_start=win_start_str,
        preferred_window_end=win_end_str,
        max_price_eur=cfg.max_price_eur,
        min_slot_minutes=cfg.min_slot_minutes,
        continuous_only=cfg.continuous_only,
    ))
    plan["profile"] = cfg.name

    print_plan_summary(plan, display_prices)
    output_path = os.path.join(output_dir, f"plan-{cfg.name}.json")
    save_plan(plan, output_path)
    print(f"  Plan saved to: {output_path}\n")
    return plan


def _write_run_outputs(plans: "list[dict]") -> None:
    """Write prices_available and plan_date step outputs to GITHUB_OUTPUT."""
    github_output = os.environ.get("GITHUB_OUTPUT")
    if not github_output or not plans:
        return
    try:
        with open(github_output, "a") as f:
            f.write("prices_available=true\n")
            f.write(f"plan_date={plans[0]['date']}\n")
    except OSError as exc:
        log.warning("Could not write to GITHUB_OUTPUT: %s", exc)




def _notify_prices_unavailable(ntfy_cfg: dict) -> None:
    """Send an ntfy notification when ENTSO-E prices are not yet available."""
    if not ntfy_cfg.get("enabled", False):
        return
    topic = os.environ.get("NTFY_TOPIC") or ntfy_cfg.get("topic", "")
    if not topic:
        return
    try:
        import urllib.request as _ur
        req = _ur.Request(
            f"https://ntfy.sh/{topic}",
            data="Prices not yet published — no charging plan scheduled today.".encode(),
            method="POST",
            headers={
                "Title":    "Charging plan unavailable",
                "Priority": "default",
            },
        )
        _ur.urlopen(req, timeout=10)
        log.info("ntfy: prices unavailable notification sent.")
    except Exception as exc:
        log.warning("ntfy prices unavailable notification failed: %s", exc)


def cmd_plan(raw_config: dict, output_dir: str = ".") -> list[dict]:
    """Fetch all available prices once, run selection for each profile."""
    try:
        configs = parse_configs(raw_config)
    except ConfigError as exc:
        log.error("%s", exc)
        sys.exit(1)

    cfg0 = configs[0]
    tz = _resolve_tz(cfg0.timezone_str, datetime.now(tz=timezone.utc).date())
    log.info("Timezone: %s (UTC%+d)", tz.name,
             int(datetime.now(tz=tz.zone).utcoffset().total_seconds() / 3600))

    all_prices, price_source = _fetch_prices(cfg0)

    plans = []
    skipped = []
    for cfg in configs:
        try:
            plan = _plan_one_profile(
                cfg, tz, all_prices, price_source, output_dir,
            )
            plans.append(plan)
        except PricesNotYetAvailable as exc:
            log.warning("%s — skipping profile.", exc)
            skipped.append(cfg.name)

    if not plans:
        log.warning(
            "No profiles could be planned — prices not yet available for any window. "
            "ENTSO-E publishes next-day prices at ~12:00 UTC."
        )
        _notify_prices_unavailable(raw_config.get("ntfy", {}))
        sys.exit(0)

    if skipped:
        log.warning("Skipped profiles (prices not yet available): %s", ", ".join(skipped))

    write_gha_summary(plans, skipped=skipped)
    _write_run_outputs(plans)
    return plans

# ===========================================================================
# CLI entry point
# ===========================================================================

def main():
    parser = argparse.ArgumentParser(
        description="Fetch ENTSO-E day-ahead prices and plan cheapest charging windows",
    )
    parser.add_argument("--config", "-c", default="config.yaml",
                        help="Path to YAML config file (default: config.yaml)")
    parser.add_argument("--output-dir", "-o", default=".",
                        help="Directory for output plan JSON files (default: current dir)")
    parser.add_argument("--debug", action="store_true",
                        help="Enable debug logging")
    args = parser.parse_args()

    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)

    config_path = args.config if os.path.exists(args.config) else None
    if args.config != "config.yaml" and not config_path:
        log.error("Config file not found: %s", args.config)
        sys.exit(1)
    if config_path is None:
        log.warning("No config.yaml found — using built-in defaults.")

    config = load_config(config_path)
    cmd_plan(config, args.output_dir)


if __name__ == "__main__":
    main()
