#!/usr/bin/env python3
"""
ENTSO-E Day-Ahead Price Planner
================================
Fetches day-ahead electricity prices from the ENTSO-E Transparency Platform,
finds the cheapest charging windows, and saves a plan to a JSON file for
review and optional editing.

Usage:
    python entsoe_prices.py plan [--config config.yaml] [--plan plan.json]

Schedule the 'plan' step to run daily at ~13:30 local time so the next day's
ENTSO-E prices are available.
"""

import argparse
from dataclasses import dataclass, field
import json
import logging
import os
import sys
import time
import xml.etree.ElementTree as ET
from collections import defaultdict
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

DEFAULT_CONFIG = {
    # ENTSO-E settings — required for 'plan'
    "entsoe": {
        "api_key": "",
        "area": "10YFI-1--------U",
    },

    # Charging window selection
    "charging": {
        "required_hours": 4,
        "max_price_cents_kwh": None,
        "contiguous_only": False,
        "merge_gaps": True,
        "min_slot_minutes": 30,
        "preferred_window_start": "00:00",
        "preferred_window_end":   "23:59",
        "timezone": None,
    },
}


def load_config(path: str) -> dict:
    """Load config from a YAML file, merging over defaults."""
    config = json.loads(json.dumps(DEFAULT_CONFIG))
    if path:
        if yaml is None:
            log.error("PyYAML is not installed. Run: pip install pyyaml")
            sys.exit(1)
        with open(path) as f:
            user_cfg = yaml.safe_load(f) or {}
        _deep_merge(config, user_cfg)
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
            time.sleep(wait)

    raise last_exc


# ===========================================================================
# Config validation
# ===========================================================================

def validate_plan_config(config: dict) -> None:
    """Validate settings required for the 'plan' step (ENTSO-E + charging)."""
    errors = []
    et = config.get("entsoe", {})
    ch = config.get("charging", {})

    if not et.get("api_key"):
        errors.append(
            "entsoe.api_key is required. "
            "Register at https://transparency.entsoe.eu and email transparency@entsoe.eu."
        )
    if not et.get("area"):
        errors.append("entsoe.area is required (e.g. 'FI').")

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

    def _parse_hhmm(key: str) -> Optional[tuple[int, int]]:
        val = ch.get(key)
        if val is None:
            errors.append(f"charging.{key} is required. Use 'HH:MM' (e.g. '00:00').")
            return None
        try:
            h, m = map(int, str(val).split(":"))
            if not (0 <= h <= 23 and 0 <= m <= 59):
                raise ValueError
            return h, m
        except (ValueError, AttributeError):
            errors.append(f"charging.{key}={val!r} is not valid. Use 'HH:MM' (00:00–23:59).")
            return None

    pw_s = _parse_hhmm("preferred_window_start")
    pw_e = _parse_hhmm("preferred_window_end")
    if pw_s is not None and pw_e is not None:
        if (pw_s[0] * 60 + pw_s[1]) >= (pw_e[0] * 60 + pw_e[1]):
            errors.append(
                f"charging.preferred_window_start must be before preferred_window_end "
                f"(got {ch['preferred_window_start']}–{ch['preferred_window_end']})."
            )

    _emit_errors(errors)

    # Advisory warnings — only reached when config is valid (no ConfigError raised above)
    win_start = ch.get("preferred_window_start")
    win_end   = ch.get("preferred_window_end")
    if ch.get("contiguous_only") and win_start and win_end:  # both always present after validation
        try:
            sh, sm = map(int, win_start.split(":"))
            eh, em = map(int, win_end.split(":"))
            win_minutes = (eh * 60 + em) - (sh * 60 + sm)
            req_minutes = int(ch.get("required_hours", 0) * 60)
            if req_minutes > win_minutes > 0:
                log.warning(
                    "contiguous_only=true but required_hours=%.1f (%.0f min) exceeds "
                    "preferred window %s–%s (%.0f min). "
                    "Charging will start before the window.",
                    ch["required_hours"], req_minutes, win_start, win_end, win_minutes,
                )
        except (ValueError, TypeError):
            pass


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
    kept as-is for log messages).  cmd_plan receives a Config and never
    touches the raw dict again.

    timezone_str is the raw IANA name from config (or None for auto-detect).
    It is NOT pre-resolved to a ZoneInfo here because the correct DST offset
    depends on target_date, which is not known until after prices are fetched.
    cmd_plan resolves it once with the actual target_date.
    """
    # ENTSO-E
    api_key:                str
    area:                   str

    # Charging
    required_minutes:       int
    contiguous_only:        bool
    merge_gaps:             bool
    min_slot_minutes:       int
    max_price_eur:          Optional[float]   # None = no ceiling
    preferred_window_start: str               # validated HH:MM
    preferred_window_end:   str               # validated HH:MM
    timezone_str:           Optional[str]     # raw IANA name or None (auto-detect)


def parse_config(raw: dict) -> "Config":
    """Validate raw config dict and return a typed Config.

    Raises ConfigError on any invalid field.  This is the single entry point
    for config validation — callers should not call validate_plan_config
    directly.
    """
    validate_plan_config(raw)          # raises ConfigError on any problem

    et = raw["entsoe"]
    ch = raw["charging"]

    # Validate the timezone string now (raises ConfigError for bad explicit names)
    # but do NOT resolve it to a ZoneInfo — the correct DST offset depends on
    # target_date, which is determined later in cmd_plan.
    tz_str = ch.get("timezone") or None
    if tz_str:
        try:
            ZoneInfo(tz_str)   # validate only; result discarded
        except (ZoneInfoNotFoundError, KeyError):
            raise ConfigError(
                f"charging.timezone={tz_str!r} is not a recognised IANA timezone name "
                f"(e.g. 'Europe/Helsinki', 'UTC'). "
                f"Check https://en.wikipedia.org/wiki/List_of_tz_database_time_zones"
            )

    ceil_cents = ch.get("max_price_cents_kwh")
    return Config(
        api_key=et["api_key"],
        area=et["area"],
        required_minutes=int(ch["required_hours"] * 60),
        contiguous_only=bool(ch.get("contiguous_only", False)),
        merge_gaps=bool(ch.get("merge_gaps", True)),
        min_slot_minutes=int(ch.get("min_slot_minutes", 30)),
        max_price_eur=ceil_cents / 100.0 if ceil_cents is not None else None,
        preferred_window_start=ch["preferred_window_start"],
        preferred_window_end=ch["preferred_window_end"],
        timezone_str=tz_str,
    )


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
    target_date: date,
) -> list[Slot]:
    """Fetch day-ahead prices for today and tomorrow in a single API call.

    ENTSO-E always returns two complete 23:00–23:00 UTC TimeSeries periods:
      - (target_date - 1)T23:00Z – target_dateT23:00Z  →  today's prices
      - target_dateT23:00Z – (target_date + 1)T23:00Z  →  tomorrow's prices

    Both periods arrive in one response.  Today's slots are filtered to
    >= now so that leftward spillover can reach back into the current evening.
    If tomorrow's TimeSeries contains fewer than 23 h of slots, prices have
    not been published yet and PricesNotYetAvailable is raised.
    """
    eic = _resolve_area(area)

    # Request window spans both periods in one call:
    #   from: (target_date - 1) 23:00 UTC  (= start of today's period)
    #   to:   (target_date + 1) 23:00 UTC  (= end of tomorrow's period)
    period_start = (target_date - timedelta(days=1)).strftime("%Y%m%d2300")
    period_end   = (target_date + timedelta(days=1)).strftime("%Y%m%d2300")
    url = (
        f"{ENTSOE_API}"
        f"?documentType=A44"
        f"&in_Domain={eic}"
        f"&out_Domain={eic}"
        f"&periodStart={period_start}"
        f"&periodEnd={period_end}"
        f"&securityToken={api_key}"
    )
    log.info("Fetching ENTSO-E prices: area=%s target_date=%s", area, target_date)
    req = urllib.request.Request(url, headers={"Accept": "application/xml"})
    try:
        raw = _http_request_with_retry(req, timeout=20, retries=3, label="ENTSO-E")
    except Exception as e:
        log.error("ENTSO-E request failed after retries: %s", e)
        raise
    all_slots = _parse_entsoe_xml(raw, target_date, area)

    # Check that tomorrow's period is actually published.
    # ENTSO-E TimeSeries boundary: today  = (target_date-1)T23:00Z – target_dateT23:00Z
    #                              tomorrow= (target_date-1)T23:00Z is the START of today,
    # so tomorrow's slots begin at (target_date-1)T23:00Z local-day boundary.
    # target_date=2026-03-14 → today's TS: 2026-03-12T23:00Z–2026-03-13T23:00Z
    #                          tomorrow's TS: 2026-03-13T23:00Z–2026-03-14T23:00Z
    # So tomorrow starts at (target_date - 1)T23:00Z.
    tomorrow_start_utc = datetime.combine(
        target_date - timedelta(days=1), time(23, 0), tzinfo=timezone.utc
    )
    target_slots   = [s for s in all_slots if s.start >= tomorrow_start_utc]
    target_minutes = sum(s.duration_minutes for s in target_slots)
    if target_minutes < 23 * 60:
        raise PricesNotYetAvailable(
            f"Only {target_minutes} min of data for {target_date} "
            f"({len(target_slots)} slots) — tomorrow's prices not published yet."
        )

    # Filter today's slots to >= now so we don't schedule in the past.
    now_utc     = datetime.now(tz=timezone.utc)
    today_slots = [s for s in all_slots if s.start < tomorrow_start_utc
                   and s.start >= now_utc]
    log.info("Today's remaining slots (>= now): %d", len(today_slots))

    combined_raw = sorted(today_slots + target_slots, key=lambda x: x.start)
    combined = [Slot(start=s.start, end=s.end, duration_minutes=s.duration_minutes,
                     price_eur_kwh=s.price_eur_kwh, slot=i)
                for i, s in enumerate(combined_raw)]
    return combined


def _parse_entsoe_xml(xml_text: str, target_date: date, area: str) -> list[Slot]:
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        raise ValueError(f"ENTSO-E response is not valid XML: {e}\n{xml_text[:300]}")

    ns_uri = ""
    if root.tag.startswith("{"):
        ns_uri = root.tag[1:root.tag.index("}")]

    if "Acknowledgement_MarketDocument" in root.tag:
        reason = ""
        for elem in root.iter():
            if elem.tag.endswith("}text") or elem.tag == "text":
                reason = elem.text or ""
                break
        raise ValueError(f"ENTSO-E returned an error: {reason or xml_text[:300]}")

    ns = {"ns": ns_uri} if ns_uri else {}

    def find_all(parent, tag):
        if ns_uri:
            return parent.findall(f"ns:{tag}", ns)
        return parent.findall(tag)

    def find_text(parent, tag):
        if ns_uri:
            return parent.findtext(f"ns:{tag}", namespaces=ns)
        return parent.findtext(tag)

    prices = []

    for ts in find_all(root, "TimeSeries"):
        period = None
        for child in ts:
            if child.tag.endswith("}Period") or child.tag == "Period":
                period = child
                break
        if period is None:
            continue

        interval = None
        for child in period:
            if child.tag.endswith("}timeInterval") or child.tag == "timeInterval":
                interval = child
                break
        if interval is None:
            continue

        start_str = find_text(interval, "start") or interval.findtext("start")
        end_str   = find_text(interval, "end")   or interval.findtext("end")
        if not start_str:
            continue

        period_start_utc = datetime.fromisoformat(start_str.replace("Z", "+00:00"))
        period_end_utc   = datetime.fromisoformat(end_str.replace("Z", "+00:00")) if end_str else None

        # Accept any TimeSeries whose period overlaps the requested date range.
        # The API request already constrains the window so no further date
        # filtering is needed here.

        resolution_str = find_text(period, "resolution") or "PT60M"
        if "15" in resolution_str:
            slot_minutes = 15
        elif "30" in resolution_str:
            slot_minutes = 30
        else:
            slot_minutes = 60

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

        if not explicit:
            continue

        total_slots = max(explicit.keys())
        last_price = explicit[min(explicit.keys())]
        for pos in range(1, total_slots + 1):
            if pos in explicit:
                last_price = explicit[pos]
            slot_start = period_start_utc + timedelta(minutes=slot_minutes * (pos - 1))
            slot_end   = slot_start + timedelta(minutes=slot_minutes)
            if slot_start < period_start_utc:
                continue
            if period_end_utc and slot_end > period_end_utc:
                continue
            prices.append(Slot(
                slot=len(prices),
                start=slot_start,
                end=slot_end,
                duration_minutes=slot_minutes,
                price_eur_kwh=last_price / 1000.0,
            ))

    seen: set[datetime] = set()
    unique: list[Slot] = []
    for p in sorted(prices, key=lambda x: x.start):
        if p.start not in seen:
            seen.add(p.start)
            unique.append(p)

    # Re-assign sequential slot ordinals after deduplication
    unique = [Slot(start=s.start, end=s.end, duration_minutes=s.duration_minutes,
                   price_eur_kwh=s.price_eur_kwh, slot=i)
              for i, s in enumerate(unique)]

    if not unique:
        raise PricesNotYetAvailable(
            f"No price slots found for area={area} date={target_date}."
        )

    slot_dur = unique[0].duration_minutes
    log.info("Fetched %d price slots for %s (resolution: %d-minute)", len(unique), target_date, slot_dur)
    return unique


# ===========================================================================
# Charging window selection
# ===========================================================================

def select_charging_windows(
    prices: list[Slot],
    required_minutes: int,
    contiguous_only: bool = False,
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

    if contiguous_only:
        return _best_contiguous_window(candidates, prices, n_slots)

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
    sorted_candidates = sorted(candidates, key=lambda x: (x.price_eur_kwh, -x.start.timestamp()))
    slot_dur = candidates[0].duration_minutes if candidates else 15

    # Use start datetime as the identity key — guaranteed unique within any
    # non-overlapping price series, and does not depend on the slot ordinal
    # field (which is only assigned inside fetch_entsoe_prices).
    selected_set: set  = set()   # Slot.start datetimes of selected slots
    disqualified: set  = set()   # Slot.start datetimes of disqualified slots

    def pick_next(n: int) -> list[Slot]:
        result = []
        for s in sorted_candidates:
            if len(result) == n:
                break
            if s.start not in selected_set and s.start not in disqualified:
                result.append(s)
        return result

    for s in pick_next(n_slots):
        selected_set.add(s.start)

    slot_by_start = {s.start: s for s in sorted_candidates}

    for iteration in range(len(candidates)):
        current = sorted([slot_by_start[k] for k in selected_set], key=lambda x: x.start)
        blocks = _group_contiguous(current)
        short_blocks = [b for b in blocks if len(b) < min_slots_per_block]
        if not short_blocks:
            break

        for block in short_blocks:
            for s in block:
                selected_set.discard(s.start)
                disqualified.add(s.start)

        deficit = n_slots - len(selected_set)
        if deficit <= 0:
            continue
        backfill = pick_next(deficit)
        if not backfill:
            log.warning("Could not backfill %d slot(s) while respecting min_slot_minutes=%d min.",
                        deficit, min_slots_per_block * slot_dur)
            break
        for s in backfill:
            selected_set.add(s.start)
    else:
        log.warning("min_slot_minutes enforcement loop exhausted without converging.")

    result = sorted([slot_by_start[k] for k in selected_set], key=lambda x: x.start)
    final_blocks  = _group_contiguous(result)
    slot_dur      = candidates[0].duration_minutes if candidates else 15
    min_block_min = min_slots_per_block * slot_dur
    short = [b for b in final_blocks if len(b) < min_slots_per_block]
    if short:
        log.warning("%d block(s) still shorter than %d min — not enough eligible slots.",
                    len(short), min_block_min)
    else:
        log.info("All %d block(s) meet the minimum block length of %d min.",
                 len(final_blocks), min_block_min)
    return result


def _best_contiguous_window(
    candidates: list[Slot], all_prices: list[Slot], n_slots: int
) -> list[Slot]:
    """Return the cheapest contiguous run of n_slots from all_prices where every slot is a candidate.

    If no fully-eligible window exists (e.g. max_price excludes too many slots),
    logs a warning and returns the cheapest window ignoring the candidate filter
    so the caller always gets a result.
    """
    slots = all_prices
    if len(slots) < n_slots:
        return slots
    candidate_starts: set[datetime] = {s.start for s in candidates}

    best_avg      = float("inf")
    best_start    = None
    # Iterate in reverse so that on equal avg price the latest window wins
    # (closest to departure time).
    for i in range(len(slots) - n_slots, -1, -1):
        window = slots[i:i + n_slots]
        if all(s.start in candidate_starts for s in window):
            avg = sum(s.price_eur_kwh for s in window) / n_slots
            if avg <= best_avg:
                best_avg   = avg
                best_start = i

    if best_start is not None:
        return slots[best_start:best_start + n_slots]

    # No fully-eligible window found — fall back to cheapest window ignoring filter
    log.warning(
        "No contiguous %d-slot window found within candidate set "
        "(price ceiling may be too low). Returning cheapest available window.",
        n_slots,
    )
    best_avg   = float("inf")
    best_start = 0
    for i in range(len(slots) - n_slots, -1, -1):
        window = slots[i:i + n_slots]
        avg = sum(s.price_eur_kwh for s in window) / n_slots
        if avg <= best_avg:
            best_avg   = avg
            best_start = i
    return slots[best_start:best_start + n_slots]


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
    contiguous_only: bool,
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
    - contiguous_only: extend the existing block leftward (earlier slots only).
    - non-contiguous: pick cheapest slots from eligible outside slots.

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

    if contiguous_only:
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
                "contiguous_only spill: extending block %d min earlier (%d slots)",
                remaining, len(spillover),
            )
            return spillover
        # No prior selected block — fall back to best contiguous block.
        # Apply the same win_end_utc restriction used by the non-empty path so
        # the result never spills after the preferred window end.
        filtered = [
            s for s in all_prices
            if s.end <= win_end_utc
            and (max_price_eur is None or s.price_eur_kwh <= max_price_eur)
        ]
        slot_dur = all_prices[0].duration_minutes if all_prices else 15
        n_slots  = (remaining + slot_dur - 1) // slot_dur
        return _best_contiguous_window(filtered, all_prices, n_slots)

    return select_charging_windows(
        candidates,
        required_minutes=remaining,
        contiguous_only=False,
        max_price=max_price_eur,
        min_slot_minutes=min_slot_minutes,
    )


def _group_contiguous(slots: list[Slot]) -> list[list[Slot]]:
    """Split a sorted list of slots into groups of adjacent (contiguous) slots."""
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
        groups = _group_contiguous(slots)
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

def merge_contiguous_slots(slots: list[Slot]) -> list[tuple[datetime, datetime]]:
    if not slots:
        return []
    groups = _group_contiguous(sorted(slots, key=lambda x: x.start))
    return [(g[0].start, g[-1].end) for g in groups]


# ===========================================================================
# Timezone helpers
# ===========================================================================

def _resolve_tz(config_tz: str | None, ref_date: date):
    """Return (tz_name, ZoneInfo) for the given timezone config string.

    config_tz may be an explicit IANA name (e.g. "Europe/Helsinki"), a
    UTC-offset string (e.g. "UTC+02:00"), or None to auto-detect from the
    host system.  Falls back to UTC if nothing can be determined.

    Returns a ZoneInfo (or timezone.utc) object — not an integer offset.
    DST transitions are handled correctly because ZoneInfo resolves the
    actual offset for each specific datetime, not a fixed hourly value.
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

    return tz_name, tz


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
    merged_starts:          set           = field(default_factory=set)


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
          "duration_minutes": 180, "avg_price_cents_kwh": 1.84,
          "gap_merged": false }
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
        # A window is gap_merged if any of its constituent slots was the start
        # of a gap-merged block. Checking only start_utc is fragile when
        # close_gap_merge produces blocks that get further merged.
        is_gap_merged = any(s.start in p.merged_starts for s in block_slots)
        win_list.append({
            "start": start_local.strftime("%H:%M"),
            "end":   end_local.strftime("%H:%M"),
            "duration_minutes": dur,
            "avg_price_cents_kwh": round(avg_p, 4),
            "gap_merged": is_gap_merged,
        })

    total_min = sum(s.duration_minutes for s in p.selected)
    sel_prices = [s.price_eur_kwh for s in p.selected]
    overall_avg = (sum(sel_prices) / len(sel_prices) * 100) if sel_prices else 0.0

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
        "preferred_window_start": p.preferred_window_start,
        "preferred_window_end":   p.preferred_window_end,
        "windows":                win_list,
        "selected_starts_utc":    [s.start.isoformat() for s in p.selected],
    }


def save_plan(plan: dict, path: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(plan, f, indent=2)
    log.info("Plan saved to %s", path)


def load_plan(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        plan = json.load(f)
    v = plan.get("version")
    if v != PLAN_VERSION:
        log.warning("Plan file version %s (expected %d) — proceeding anyway.", v, PLAN_VERSION)
    return plan


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
def _cyan(t):   return _c("36", t)



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
    print(_bold("  ENTSO-E Charging Plan"))
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
            merged_tag = f"  {_yellow('⚡ gap merged')}" if w.get("gap_merged") else ""
            print(_window_bar(w["start"], w["end"],
                              w["duration_minutes"], w["avg_price_cents_kwh"],
                              min_c, max_c) + merged_tag)
        print()
    else:
        print(f"  {_red('✗ No windows selected')}")
        print()

    print(_bold("  " + "═" * W))
    print()


# ===========================================================================
# GitHub Actions job summary
# ===========================================================================

def render_svg_chart(plan: dict, all_prices: list[Slot]) -> str:
    """
    Render an SVG price-area chart with charging windows marked as a bar
    along the x-axis.  Returns a raw SVG string suitable for saving as
    chart.svg and uploading as a GitHub Actions artifact.

    Layout (600×220 px):
      - Filled pink area  : hourly average prices (step chart)
      - Blue shading      : preferred charging window (if configured)
      - Purple bar        : scheduled charging windows (bottom strip)
      - Gridlines + axes  : y = c€/kWh, x = 00:00–24:00 local time
    """
    if not all_prices:
        return ""

    W, H    = 600, 220
    PAD_L   = 48
    PAD_R   = 12
    PAD_T   = 18
    PAD_B   = 52
    BAR_H   = 10
    CHART_W = W - PAD_L - PAD_R
    CHART_H = H - PAD_T - PAD_B - BAR_H - 4

    offset   = plan["utc_offset_hours"]
    wins     = plan["windows"]
    pw_start = plan.get("preferred_window_start")
    pw_end   = plan.get("preferred_window_end")

    hourly: dict[int, list[float]] = defaultdict(list)
    for slot in all_prices:
        h = (slot.start + timedelta(hours=offset)).hour
        hourly[h].append(slot.price_eur_kwh * 100)

    all_c = [v for vals in hourly.values() for v in vals]
    if not all_c:
        return ""
    max_c   = max(all_c) * 1.15
    c_range = max_c or 1.0

    def x(hour: float) -> float:
        return PAD_L + hour / 24 * CHART_W

    def y(price_c: float) -> float:
        return PAD_T + CHART_H * (1 - price_c / c_range)

    def hm_frac(hhmm: str) -> float:
        hh, mm = map(int, hhmm.split(":"))
        return (hh + mm / 60) / 24

    bar_top = PAD_T + CHART_H + 4

    # ── Area path (step chart, one step per slot) ────────────────────────────
    points: list[tuple[float, float]] = []
    for slot in sorted(all_prices, key=lambda s: s.start):
        h_local = (slot.start + timedelta(hours=offset)).hour +                   (slot.start + timedelta(hours=offset)).minute / 60
        h_end   = h_local + slot.duration_minutes / 60
        price_c = slot.price_eur_kwh * 100
        points.append((x(h_local), y(price_c)))
        points.append((x(h_end),   y(price_c)))

    base_y  = y(0)
    path_d  = f"M {points[0][0]:.1f},{base_y:.1f} "
    for px, py in points:
        path_d += f"L {px:.1f},{py:.1f} "
    path_d += f"L {points[-1][0]:.1f},{base_y:.1f} Z"

    # ── Preferred window shading ─────────────────────────────────────────────
    pw_rect = ""
    if pw_start and pw_end:
        px1 = x(hm_frac(pw_start) * 24)
        px2 = x(hm_frac(pw_end)   * 24)
        pw_rect = (
            f'<rect x="{px1:.1f}" y="{PAD_T}" '
            f'width="{px2-px1:.1f}" height="{CHART_H}" '
            f'fill="#bfdbfe" fill-opacity="0.35"/>'
        )

    # ── Gridlines & y-labels ─────────────────────────────────────────────────
    grid_svg = ""
    for i in range(4):
        price = max_c * i / 3
        gy    = y(price)
        grid_svg += (
            f'<line x1="{PAD_L:.1f}" y1="{gy:.1f}" '
            f'x2="{PAD_L+CHART_W:.1f}" y2="{gy:.1f}" '
            f'stroke="#e5e7eb" stroke-width="1"/>'
            f'<text x="{PAD_L-4:.1f}" y="{gy+4:.1f}" '
            f'text-anchor="end" font-size="10" fill="#9ca3af">{price:.1f}</text>'
        )

    # ── X-axis labels ────────────────────────────────────────────────────────
    xaxis_svg = ""
    for h in (0, 6, 12, 18, 24):
        xv = x(h)
        xaxis_svg += (
            f'<text x="{xv:.1f}" y="{bar_top+BAR_H+14:.1f}" '
            f'text-anchor="middle" font-size="10" fill="#9ca3af">{h}</text>'
        )

    # ── Charging window bars ─────────────────────────────────────────────────
    bars_svg = ""
    for w in wins:
        x1 = PAD_L + hm_frac(w["start"]) * CHART_W
        x2 = PAD_L + hm_frac(w["end"])   * CHART_W
        bars_svg += (
            f'<rect x="{x1:.1f}" y="{bar_top:.1f}" '
            f'width="{x2-x1:.1f}" height="{BAR_H}" '
            f'fill="#7c3aed" rx="2"/>'
        )

    # ── Legend ───────────────────────────────────────────────────────────────
    leg_y  = bar_top + BAR_H + 28
    legend = (
        f'<rect x="{PAD_L}" y="{leg_y-8}" width="12" height="8" fill="#f9a8b8" rx="1"/>'
        f'<text x="{PAD_L+16}" y="{leg_y}" font-size="10" fill="#6b7280">Prices</text>'
        f'<rect x="{PAD_L+62}" y="{leg_y-8}" width="12" height="8" fill="#7c3aed" rx="1"/>'
        f'<text x="{PAD_L+78}" y="{leg_y}" font-size="10" fill="#6b7280">Charging</text>'
    )
    if pw_start and pw_end:
        legend += (
            f'<rect x="{PAD_L+152}" y="{leg_y-8}" width="12" height="8" fill="#bfdbfe" rx="1"/>'
            f'<text x="{PAD_L+168}" y="{leg_y}" font-size="10" fill="#6b7280">Preferred window</text>'
        )

    # ── Y-axis label ─────────────────────────────────────────────────────────
    mid_y  = PAD_T + CHART_H // 2
    ylabel = (
        f'<text x="10" y="{mid_y}" text-anchor="middle" font-size="10" fill="#d1d5db" '
        f'transform="rotate(-90,10,{mid_y})">c€/kWh</text>'
    )

    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" '
        f'viewBox="0 0 {W} {H}" width="{W}" height="{H}" '
        f'style="font-family:sans-serif;background:#ffffff">'
        f'{pw_rect}{grid_svg}'
        f'<path d="{path_d}" fill="#fecdd3" fill-opacity="0.7" stroke="#fb7185" stroke-width="1.5"/>'
        f'<rect x="{PAD_L}" y="{bar_top}" width="{CHART_W}" height="{BAR_H}" fill="#f3f4f6" rx="2"/>'
        f'{bars_svg}'
        f'<line x1="{PAD_L}" y1="{PAD_T}" x2="{PAD_L}" y2="{PAD_T+CHART_H}" stroke="#d1d5db" stroke-width="1"/>'
        f'<line x1="{PAD_L}" y1="{PAD_T+CHART_H}" x2="{PAD_L+CHART_W}" y2="{PAD_T+CHART_H}" stroke="#d1d5db" stroke-width="1"/>'
        f'{xaxis_svg}{ylabel}{legend}'
        f'</svg>'
    )


def write_gha_summary(plan: dict, all_prices: list[Slot]) -> None:
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not summary_path:
        return

    ps   = plan["price_stats"]
    req  = plan["required_minutes"]
    tot  = plan["total_minutes"]
    avg  = plan["avg_price_cents_kwh"]
    wins = plan["windows"]
    savings_pct = (1 - avg / ps["avg_cents_kwh"]) * 100 if ps["avg_cents_kwh"] else 0

    md = [f"## \u2705 Charging Plan \u2014 {plan['date']}", ""]

    md += [
        "| | |", "|---|---|",
        f"| **Date** | {plan['date']} |",
        f"| **Area** | {plan['area']} |",
        f"| **Source** | {plan['price_source']} |",
        f"| **Timezone** | {plan['timezone']} (UTC{plan['utc_offset_hours']:+d}) |",
        f"| **Market prices** | {ps['min_cents_kwh']:.2f} \u2013 {ps['max_cents_kwh']:.2f} c\u20ac/kWh (avg {ps['avg_cents_kwh']:.2f}) |",
        f"| **Required** | {req} min |",
        f"| **Scheduled** | {tot} min" +
        (" ⚠️ **incomplete** — only {tot} of {req} min scheduled, tomorrow's prices may not be published yet" if tot < req else "") +
        " |",
        f"| **Avg price** | **{avg:.2f} c\u20ac/kWh** ({abs(savings_pct):.0f}% {'below' if savings_pct >= 0 else 'above'} market avg) |",
        "",
    ]

    if wins:
        md += ["### Charging windows", ""]
        md.append("| # | Start | End | Duration | Avg price |")
        md.append("|---|---|---|---|---|")
        for i, w in enumerate(wins, 1):
            md.append(
                f"| {i} | {w['start']} | {w['end']} | "
                f"{w['duration_minutes']} min | {w['avg_price_cents_kwh']:.2f} c\u20ac/kWh |"
            )
        md.append("")
    else:
        md += ["_No windows selected._", ""]

    if all_prices:
        svg = render_svg_chart(plan, all_prices)
        if svg:
            chart_path = "chart.svg"
            try:
                with open(chart_path, "w", encoding="utf-8") as _cf:
                    _cf.write(svg)
                log.info("Price chart saved to %s", chart_path)
                md += ["### Price profile", "",
                       "chart.svg is included in the run artifact.", ""]
            except OSError as _ce:
                log.warning("Could not save chart: %s", _ce)

    try:
        with open(summary_path, "a", encoding="utf-8") as f:
            f.write("\n".join(md) + "\n")
    except OSError as exc:
        log.warning("Could not write GitHub Actions summary: %s", exc)


# ---------------------------------------------------------------------------
# cmd_plan helpers — each covers one named phase of the planning pipeline
# ---------------------------------------------------------------------------

def _plan_target_date(timezone_str: Optional[str]) -> date:
    """Return the date to plan for: today if before local noon, tomorrow otherwise.

    Uses today's DST offset as a bootstrap — target_date is not yet known.
    This is correct in all practical cases: the only edge is a timezone whose
    DST transition falls exactly at midnight on the day being planned, which
    would require a clock reading within a few minutes of that boundary to
    produce the wrong result.
    """
    now_utc        = datetime.now(tz=timezone.utc)
    bootstrap_name, bootstrap_tz = _resolve_tz(timezone_str, now_utc.date())
    local_now      = now_utc.astimezone(bootstrap_tz)
    if local_now.hour < 12:
        target = local_now.date()
    else:
        target = (local_now + timedelta(days=1)).date()
    log.info("Target date: %s (local time: %s, tz bootstrap: %s)",
             target, local_now.strftime("%Y-%m-%d %H:%M"), bootstrap_name)
    return target


def _fetch_prices(cfg: Config, target_date: date) -> tuple[list[Slot], str]:
    """Fetch ENTSO-E prices for target_date.  Exits the process on failure."""
    try:
        prices = fetch_entsoe_prices(cfg.api_key, cfg.area, target_date)
        return prices, "ENTSO-E"
    except PricesNotYetAvailable:
        log.warning(
            "Tomorrow's prices (%s) are not yet available. "
            "ENTSO-E publishes next-day prices at ~13:00 CET. "
            "The scheduled workflow runs after that — re-run then or wait for the cron.",
            target_date,
        )
        sys.exit(0)
    except Exception as exc:
        log.error("ENTSO-E fetch failed: %s", exc)
        sys.exit(1)


def _select_slots(
    cfg:              Config,
    candidate_prices: list[Slot],
    win_start_utc:    datetime,
    win_end_utc:      datetime,
) -> tuple[list[Slot], set]:
    """Run the full slot-selection pipeline and return (selected, merged_starts).

    Pipeline:
      filter preferred window → greedy cheapest select → spillover fill
      → gap-merge (if enabled) → coverage warning
    """
    inside, outside = filter_preferred_window(
        candidate_prices,
        win_start_utc=win_start_utc,
        win_end_utc=win_end_utc,
        window_start_local=cfg.preferred_window_start,
        window_end_local=cfg.preferred_window_end,
    )

    selected = select_charging_windows(
        inside,
        required_minutes=cfg.required_minutes,
        contiguous_only=cfg.contiguous_only,
        max_price=cfg.max_price_eur,
        min_slot_minutes=cfg.min_slot_minutes,
    )

    remaining = cfg.required_minutes - sum(s.duration_minutes for s in selected)
    if remaining > 0 and outside:
        log.info("Filling %d min from outside preferred window.", remaining)
        spillover = _select_spillover(
            outside=outside,
            selected=selected,
            contiguous_only=cfg.contiguous_only,
            win_end_utc=win_end_utc,
            win_end_local=cfg.preferred_window_end,
            required_minutes=cfg.required_minutes,
            remaining=remaining,
            max_price_eur=cfg.max_price_eur,
            min_slot_minutes=cfg.min_slot_minutes,
            all_prices=candidate_prices,
        )
        selected = sorted(selected + spillover, key=lambda x: x.start)

    merged_starts: set = set()
    if cfg.merge_gaps and not cfg.contiguous_only:
        selected, merged_starts = close_gap_merge(
            selected, candidate_prices, cfg.min_slot_minutes, cfg.required_minutes
        )

    if not selected:
        log.error("No slots selected.")
    else:
        scheduled_min = sum(s.duration_minutes for s in selected)
        if scheduled_min < cfg.required_minutes:
            log.warning(
                "⚠ Only %d min scheduled of %d min required — "
                "not enough price data available yet (tomorrow's prices may not be published).",
                scheduled_min, cfg.required_minutes,
            )

    return selected, merged_starts


# ---------------------------------------------------------------------------
# Main planning command
# ---------------------------------------------------------------------------

def cmd_plan(raw_config: dict, output_path: str) -> dict:
    """Fetch prices, select windows, print the plan, save plan.json."""
    try:
        cfg = parse_config(raw_config)
    except ConfigError as exc:
        log.error("%s", exc)
        sys.exit(1)

    target_date = _plan_target_date(cfg.timezone_str)

    all_prices, price_source = _fetch_prices(cfg, target_date)

    # Resolve tz against target_date — the single authoritative resolve.
    # DST offset is correct for the day being planned (not today's offset).
    cfg_tz_name, cfg_tz = _resolve_tz(cfg.timezone_str, target_date)
    _noon = datetime(target_date.year, target_date.month, target_date.day, 12, 0, tzinfo=cfg_tz)
    log.info("Timezone: %s (UTC%+d)", cfg_tz_name,
             int(_noon.utcoffset().total_seconds() / 3600))

    # Resolve preferred window HH:MM → UTC once; all downstream code uses UTC.
    win_start_utc = _hhmm_to_utc(cfg.preferred_window_start, target_date, cfg_tz)
    win_end_utc   = _hhmm_to_utc(cfg.preferred_window_end,   target_date, cfg_tz)
    log.info("Window UTC: %s – %s", win_start_utc.isoformat(), win_end_utc.isoformat())

    # Trim today's slots that can never be part of the plan (too early for leftward spill).
    earliest_useful  = win_start_utc - timedelta(minutes=cfg.required_minutes)
    candidate_prices = [s for s in all_prices if s.start >= earliest_useful]
    if trimmed := len(all_prices) - len(candidate_prices):
        log.info("Trimmed %d unreachable today-slots (earliest useful: %s UTC)",
                 trimmed, earliest_useful.strftime("%Y-%m-%d %H:%M"))

    selected, merged_starts = _select_slots(
        cfg, candidate_prices, win_start_utc, win_end_utc
    )
    windows = merge_contiguous_slots(selected)

    # Price stats reflect tomorrow's prices only, not today's spill slots.
    tomorrow_prices = [s for s in all_prices if s.start.astimezone(cfg_tz).date() == target_date]
    if not tomorrow_prices:
        log.warning(
            "No prices found for target_date=%s after local-date filter — using full all_prices for stats.",
            target_date,
        )
        tomorrow_prices = list(all_prices)

    plan = build_plan(PlanParams(
        target_date=target_date,
        area=cfg.area,
        price_source=price_source,
        all_prices=tomorrow_prices,
        selected=selected,
        windows=windows,
        required_minutes=cfg.required_minutes,
        tz=cfg_tz,
        timezone_name=cfg_tz_name,
        merged_starts=merged_starts,
        preferred_window_start=cfg.preferred_window_start,
        preferred_window_end=cfg.preferred_window_end,
    ))

    print_plan_summary(plan, tomorrow_prices)
    save_plan(plan, output_path)
    print(f"  Plan saved to: {output_path}\n")
    write_gha_summary(plan, all_prices=tomorrow_prices)

    return plan


# ===========================================================================
# CLI entry point
# ===========================================================================

def main():
    parser = argparse.ArgumentParser(
        description="Fetch ENTSO-E day-ahead prices and plan cheapest charging windows",
    )
    parser.add_argument("--config", "-c", default="config.yaml",
                        help="Path to YAML config file (default: config.yaml)")
    parser.add_argument("--plan", "-p", default="plan.json",
                        help="Path to output plan JSON file (default: plan.json)")
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
    cmd_plan(config, args.plan)


if __name__ == "__main__":
    main()
