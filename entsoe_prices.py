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
from datetime import date, datetime, timedelta, timezone
from typing import Optional
import urllib.request
import urllib.error

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
        "preferred_window_start": None,
        "preferred_window_end": None,
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

    for key in ("preferred_window_start", "preferred_window_end"):
        val = ch.get(key)
        if val is not None:
            try:
                h, m = map(int, str(val).split(":"))
                if not (0 <= h <= 23 and 0 <= m <= 59):
                    raise ValueError
            except (ValueError, AttributeError):
                errors.append(f"charging.{key}={val!r} is not valid. Use 'HH:MM'.")

    _emit_errors(errors)

    # Advisory warnings — only reached when config is valid (no ConfigError raised above)
    win_start = ch.get("preferred_window_start")
    win_end   = ch.get("preferred_window_end")
    if ch.get("contiguous_only") and win_start and win_end:
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
) -> list[dict]:
    """Fetch tomorrow's day-ahead prices plus today's remaining slots.

    Today's slots (from now onwards) are prepended so that leftward spillover
    can extend a contiguous charging block into the current evening.
    """
    eic = _resolve_area(area)

    def _fetch_one(d: date) -> list[dict]:
        period_start = (d - timedelta(days=1)).strftime("%Y%m%d2000")
        period_end   = d.strftime("%Y%m%d2300")
        url = (
            f"{ENTSOE_API}"
            f"?documentType=A44"
            f"&in_Domain={eic}"
            f"&out_Domain={eic}"
            f"&periodStart={period_start}"
            f"&periodEnd={period_end}"
            f"&securityToken={api_key}"
        )
        log.info("Fetching ENTSO-E prices: area=%s date=%s", area, d)
        req = urllib.request.Request(url, headers={"Accept": "application/xml"})
        try:
            raw = _http_request_with_retry(req, timeout=20, retries=3, label="ENTSO-E")
        except Exception as e:
            log.error("ENTSO-E request failed after retries: %s", e)
            raise
        return _parse_entsoe_xml(raw, d, area)

    tomorrow_prices = _fetch_one(target_date)

    # Fetch today and keep only slots from now onwards
    today = target_date - timedelta(days=1)
    try:
        today_prices = _fetch_one(today)
        now_utc      = datetime.now(tz=timezone.utc)
        today_prices = [s for s in today_prices if s["start"] >= now_utc]
        log.info("Today's remaining slots (>= now): %d", len(today_prices))
    except Exception as e:
        log.warning("Could not fetch today's prices, proceeding with tomorrow only: %s", e)
        today_prices = []

    # Merge and re-number slots
    combined = sorted(today_prices + tomorrow_prices, key=lambda x: x["start"])
    for i, s in enumerate(combined):
        s["slot"] = i
    return combined


def _parse_entsoe_xml(xml_text: str, target_date: date, area: str) -> list[dict]:
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
            prices.append({
                "slot": len(prices),
                "start": slot_start,
                "end": slot_end,
                "duration_minutes": slot_minutes,
                "price_eur_kwh": last_price / 1000.0,
            })

    seen: set[datetime] = set()
    unique = []
    for p in sorted(prices, key=lambda x: x["start"]):
        if p["start"] not in seen:
            seen.add(p["start"])
            unique.append(p)

    for i, p in enumerate(unique):
        p["slot"] = i

    if not unique:
        raise PricesNotYetAvailable(
            f"No price slots found for area={area} date={target_date}."
        )

    slot_dur = unique[0]["duration_minutes"]
    log.info("Fetched %d price slots for %s (resolution: %d-minute)", len(unique), target_date, slot_dur)
    return unique


# ===========================================================================
# Charging window selection
# ===========================================================================

def select_charging_windows(
    prices: list[dict],
    required_minutes: int,
    contiguous_only: bool = False,
    max_price: Optional[float] = None,
    min_slot_minutes: int = 15,
) -> list[dict]:
    if not prices:
        return []

    slot_dur = prices[0]["duration_minutes"]
    if required_minutes % slot_dur != 0:
        required_minutes = ((required_minutes + slot_dur - 1) // slot_dur) * slot_dur
        log.warning("required_minutes rounded up to %d to align with %d-minute slots.",
                    required_minutes, slot_dur)

    n_slots = required_minutes // slot_dur
    log.info("Selecting %d slots × %d min = %d min of charging", n_slots, slot_dur, required_minutes)

    candidates = prices
    if max_price is not None:
        candidates = [p for p in prices if p["price_eur_kwh"] <= max_price]
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
        selected = sorted(candidates, key=lambda x: (x["price_eur_kwh"], -x["start"].timestamp()))[:n_slots]
        selected.sort(key=lambda x: x["start"])
        return selected

    return _select_with_min_block(candidates, n_slots, min_slots_per_block)


def _select_with_min_block(
    candidates: list[dict],
    n_slots: int,
    min_slots_per_block: int,
) -> list[dict]:
    sorted_candidates = sorted(candidates, key=lambda x: (x["price_eur_kwh"], -x["start"].timestamp()))
    slot_dur = candidates[0]["duration_minutes"] if candidates else 15

    selected_set: set[int] = set()
    disqualified: set[int] = set()

    def pick_next(n: int) -> list[dict]:
        result = []
        for s in sorted_candidates:
            if len(result) == n:
                break
            if id(s) not in selected_set and id(s) not in disqualified:
                result.append(s)
        return result

    for s in pick_next(n_slots):
        selected_set.add(id(s))

    slot_by_id = {id(s): s for s in sorted_candidates}

    for iteration in range(len(candidates)):
        current = sorted([slot_by_id[i] for i in selected_set], key=lambda x: x["start"])
        blocks = _group_contiguous(current)
        short_blocks = [b for b in blocks if len(b) < min_slots_per_block]
        if not short_blocks:
            break

        for block in short_blocks:
            for s in block:
                selected_set.discard(id(s))
                disqualified.add(id(s))

        deficit = n_slots - len(selected_set)
        if deficit <= 0:
            continue
        backfill = pick_next(deficit)
        if not backfill:
            log.warning("Could not backfill %d slot(s) while respecting min_slot_minutes=%d min.",
                        deficit, min_slots_per_block * slot_dur)
            break
        for s in backfill:
            selected_set.add(id(s))
    else:
        log.warning("min_slot_minutes enforcement loop exhausted without converging.")

    result = sorted([slot_by_id[i] for i in selected_set], key=lambda x: x["start"])
    final_blocks  = _group_contiguous(result)
    slot_dur      = candidates[0]["duration_minutes"] if candidates else 15
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
    candidates: list[dict], all_prices: list[dict], n_slots: int
) -> list[dict]:
    """Return the cheapest contiguous run of n_slots from all_prices where every slot is a candidate.

    If no fully-eligible window exists (e.g. max_price excludes too many slots),
    logs a warning and returns the cheapest window ignoring the candidate filter
    so the caller always gets a result.
    """
    slots = all_prices
    if len(slots) < n_slots:
        return slots
    candidate_starts: set[datetime] = {s["start"] for s in candidates}

    best_avg      = float("inf")
    best_start    = None
    for i in range(len(slots) - n_slots + 1):
        window = slots[i:i + n_slots]
        if all(s["start"] in candidate_starts for s in window):
            avg = sum(s["price_eur_kwh"] for s in window) / n_slots
            if avg < best_avg:
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
    for i in range(len(slots) - n_slots + 1):
        window = slots[i:i + n_slots]
        avg = sum(s["price_eur_kwh"] for s in window) / n_slots
        if avg < best_avg:
            best_avg   = avg
            best_start = i
    return slots[best_start:best_start + n_slots]


def _hhmm_to_utc(hhmm: str, ref_date: date, utc_offset_hours: int) -> datetime:
    """Convert a local HH:MM string on ref_date to a UTC datetime."""
    h, m = map(int, hhmm.split(":"))
    return datetime(ref_date.year, ref_date.month, ref_date.day,
                    h, m, tzinfo=timezone.utc) - timedelta(hours=utc_offset_hours)


def filter_preferred_window(
    prices: list[dict],
    window_start_local: Optional[str],
    window_end_local: Optional[str],
    local_tz_offset_hours: int,
    target_date: date,
) -> tuple[list[dict], list[dict]]:
    win_start_utc = _hhmm_to_utc(window_start_local, target_date, local_tz_offset_hours) if window_start_local else None
    win_end_utc   = _hhmm_to_utc(window_end_local,   target_date, local_tz_offset_hours) if window_end_local   else None

    inside, outside = [], []
    for slot in prices:
        in_start = (win_start_utc is None) or (slot["start"] >= win_start_utc)
        in_end   = (win_end_utc   is None) or (slot["end"]   <= win_end_utc)
        if in_start and in_end:
            inside.append(slot)
        else:
            outside.append(slot)

    if win_start_utc or win_end_utc:
        win_s = window_start_local or "00:00"
        win_e = window_end_local   or "24:00"
        log.info("Preferred window %s–%s local: %d slots inside, %d outside",
                 win_s, win_e, len(inside), len(outside))

    return inside, outside


def _group_contiguous(slots: list[dict]) -> list[list[dict]]:
    """Split a sorted list of slots into groups of adjacent (contiguous) slots."""
    if not slots:
        return []
    groups: list[list[dict]] = []
    block = [slots[0]]
    for slot in slots[1:]:
        if slot["start"] == block[-1]["end"]:
            block.append(slot)
        else:
            groups.append(block)
            block = [slot]
    groups.append(block)
    return groups


def close_gap_merge(
    selected: list[dict],
    all_prices: list[dict],
    min_slot_minutes: int,
    required_minutes: int,
) -> tuple[list[dict], set]:
    """
    If two selected blocks are separated by a gap smaller than min_slot_minutes,
    bridge the gap by including those intervening slots, then trim from either
    end of the newly formed continuous block until total selected minutes equals
    required_minutes again.

    Trimming prefers removing the most expensive endpoint slot. On a tie the
    earliest slot is dropped.

    Returns (slots, merged_starts) where merged_starts is a set of UTC start
    datetimes for windows that were produced by a gap merge.
    """
    if not selected:
        return selected, set()

    slots = sorted(selected, key=lambda x: x["start"])
    price_map = {s["start"]: s for s in all_prices}
    merged_starts: set = set()

    changed = True
    while changed:
        changed = False
        groups = _group_contiguous(slots)
        for i in range(len(groups) - 1):
            gap_start = groups[i][-1]["end"]
            gap_end   = groups[i + 1][0]["start"]
            gap_min   = int((gap_end - gap_start).total_seconds() / 60)
            if 0 < gap_min < min_slot_minutes:
                # Collect gap slots from all_prices
                gap_slots = [
                    price_map[s["start"]]
                    for s in all_prices
                    if gap_start <= s["start"] < gap_end and s["start"] in price_map
                ]
                if not gap_slots:
                    continue
                # Merge the two blocks + gap into one
                merged_block = sorted(
                    groups[i] + gap_slots + groups[i + 1],
                    key=lambda x: x["start"],
                )
                # Trim endpoint slots until total selected equals required_minutes.
                # extra_minutes = how much over required we are after bridging the gap.
                # merged_block already contains all slots including the gap, so summing
                # it gives the exact post-bridge total without double-counting.
                current_total = sum(s["duration_minutes"] for s in merged_block)
                extra_minutes = current_total - required_minutes
                log.info(
                    "Gap of %d min between %s and %s is below min_slot_minutes=%d — merging blocks.",
                    gap_min,
                    gap_start.isoformat(), gap_end.isoformat(),
                    min_slot_minutes,
                )
                # Trim endpoint slots until we shed extra_minutes.
                # On a tie in price, always drop the earliest (leading) slot.
                slot_dur = merged_block[0]["duration_minutes"]
                while extra_minutes >= slot_dur and len(merged_block) > 1:
                    first = merged_block[0]
                    last  = merged_block[-1]
                    if last["price_eur_kwh"] > first["price_eur_kwh"]:
                        log.info("Trimming trailing slot %s (%.4f €/kWh)", last["start"].isoformat(), last["price_eur_kwh"])
                        merged_block = merged_block[:-1]
                    else:
                        # first is more expensive, or equal — drop earliest (first)
                        log.info("Trimming leading slot %s (%.4f €/kWh)", first["start"].isoformat(), first["price_eur_kwh"])
                        merged_block = merged_block[1:]
                    extra_minutes -= slot_dur

                merged_starts.add(merged_block[0]["start"])

                # Rebuild slots list with the merged block in place
                other_groups = groups[:i] + [merged_block] + groups[i + 2:]
                slots = sorted(
                    [s for g in other_groups for s in g],
                    key=lambda x: x["start"],
                )
                changed = True
                break  # restart loop with updated groups

    return slots, merged_starts

def merge_contiguous_slots(slots: list[dict]) -> list[tuple[datetime, datetime]]:
    if not slots:
        return []
    groups = _group_contiguous(sorted(slots, key=lambda x: x["start"]))
    return [(g[0]["start"], g[-1]["end"]) for g in groups]


# ===========================================================================
# Timezone helpers
# ===========================================================================

def _resolve_tz(config_tz: str | None, ref_date: date) -> tuple[str, int]:
    """Return (tz_name, utc_offset_hours) for the given date.

    config_tz may be an explicit IANA name (e.g. "Europe/Helsinki"), a
    UTC-offset string (e.g. "UTC+02:00"), or None to auto-detect from the
    host system.
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
            offset = datetime.now().astimezone().utcoffset()
            if offset is not None:
                total_min = int(offset.total_seconds() / 60)
                sign = "+" if total_min >= 0 else "-"
                h, m = divmod(abs(total_min), 60)
                tz_name = f"UTC{sign}{h:02d}:{m:02d}"
        except Exception:
            pass
    tz_name = tz_name or "UTC"

    # Determine UTC offset in whole hours for ref_date
    offset_hours = 0
    try:
        from zoneinfo import ZoneInfo
        dt = datetime(ref_date.year, ref_date.month, ref_date.day, 12, 0,
                      tzinfo=ZoneInfo(tz_name))
        utcoff = dt.utcoffset()
        if utcoff is not None:
            offset_hours = int(utcoff.total_seconds() / 3600)
    except Exception:
        try:
            utcoff = datetime.now().astimezone().utcoffset()
            if utcoff is not None:
                offset_hours = int(utcoff.total_seconds() / 3600)
        except Exception:
            pass

    return tz_name, offset_hours


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
    all_prices:             list
    selected:               list
    windows:                list
    required_minutes:       int
    local_tz_offset_hours:  int
    timezone_name:          str
    merged_starts:          set           = field(default_factory=set)
    preferred_window_start: str | None    = None
    preferred_window_end:   str | None    = None


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
    all_prices_eur = [s["price_eur_kwh"] for s in stat_source]
    if all_prices_eur:
        price_stats = {
            "min_cents_kwh":  round(min(all_prices_eur) * 100, 4),
            "max_cents_kwh":  round(max(all_prices_eur) * 100, 4),
            "avg_cents_kwh":  round(sum(all_prices_eur) / len(all_prices_eur) * 100, 4),
        }
    else:
        price_stats = {"min_cents_kwh": 0.0, "max_cents_kwh": 0.0, "avg_cents_kwh": 0.0}

    win_list = []
    for start_utc, end_utc in p.windows:
        start_local = start_utc + timedelta(hours=p.local_tz_offset_hours)
        end_local   = end_utc   + timedelta(hours=p.local_tz_offset_hours)
        dur = int((end_utc - start_utc).total_seconds() / 60)
        block_slots = [s for s in p.selected if start_utc <= s["start"] < end_utc]
        avg_p = (sum(s["price_eur_kwh"] for s in block_slots) / len(block_slots) * 100
                 if block_slots else 0.0)
        # A window is gap_merged if any of its constituent slots was the start
        # of a gap-merged block. Checking only start_utc is fragile when
        # close_gap_merge produces blocks that get further merged.
        is_gap_merged = any(s["start"] in p.merged_starts for s in block_slots)
        win_list.append({
            "start": start_local.strftime("%H:%M"),
            "end":   end_local.strftime("%H:%M"),
            "duration_minutes": dur,
            "avg_price_cents_kwh": round(avg_p, 4),
            "gap_merged": is_gap_merged,
        })

    total_min = sum(s["duration_minutes"] for s in p.selected)
    sel_prices = [s["price_eur_kwh"] for s in p.selected]
    overall_avg = (sum(sel_prices) / len(sel_prices) * 100) if sel_prices else 0.0

    return {
        "version":                PLAN_VERSION,
        "date":                   str(p.target_date),
        "area":                   p.area,
        "price_source":           p.price_source,
        "timezone":               p.timezone_name,
        "utc_offset_hours":       p.local_tz_offset_hours,
        "price_stats":            price_stats,
        "required_minutes":       p.required_minutes,
        "total_minutes":          total_min,
        "avg_price_cents_kwh":    round(overall_avg, 4),
        "preferred_window_start": p.preferred_window_start,
        "preferred_window_end":   p.preferred_window_end,
        "windows":                win_list,
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
import sys as _sys
_USE_COLOR = _sys.stdout.isatty() and _sys.platform != "win32"

def _c(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _USE_COLOR else text

def _green(t):  return _c("32", t)
def _yellow(t): return _c("33", t)
def _red(t):    return _c("31", t)
def _bold(t):   return _c("1",  t)
def _dim(t):    return _c("2",  t)
def _cyan(t):   return _c("36", t)


def _price_color(price_eur: float, min_p: float, max_p: float) -> str:
    """Return an ANSI colour based on relative price (green=cheap, red=expensive)."""
    if max_p == min_p:
        return "33"
    ratio = (price_eur - min_p) / (max_p - min_p)
    if ratio < 0.33:
        return "32"   # green
    elif ratio < 0.66:
        return "33"   # yellow
    else:
        return "31"   # red


def render_price_chart(
    all_prices: list[dict],
    selected_starts: set,
    local_tz_offset_hours: int,
    bar_width: int = 38,
) -> str:
    """
    Render a rich terminal bar chart of the 24-hour price profile.

    Each row = one hour (sub-hourly slots averaged).
    Bars are colour-coded green→yellow→red by relative price.
    Selected hours are rendered with a filled bar and a ▶ marker.
    A sparkline footer summarises cheapest/priciest hours at a glance.
    """
    if not all_prices:
        return ""

    # Sub-hourly slots are averaged per hour. all_prices should contain only
    # tomorrow's slots (target_date) so each hour appears at most once.
    hourly_prices: dict[int, list[float]] = defaultdict(list)
    hourly_sel:    dict[int, bool]        = {}

    for slot in all_prices:
        local_start = slot["start"] + timedelta(hours=local_tz_offset_hours)
        h = local_start.hour
        hourly_prices[h].append(slot["price_eur_kwh"])
        if slot["start"] in selected_starts:
            hourly_sel[h] = True

    all_eur  = [s["price_eur_kwh"] for s in all_prices]
    min_p    = min(all_eur)
    max_p    = max(all_eur)
    avg_p    = sum(all_eur) / len(all_eur)
    p_range  = max_p - min_p or 1.0

    SEL_CHAR  = "█"
    BASE_CHAR = "▒"
    EMPTY     = "░"

    sep = "  " + "─" * (bar_width + 22)
    lines = [
        "",
        _bold("  Hour  c€/kWh  " + " " * (bar_width // 2 - 3) + "Price profile"),
        sep,
    ]

    for hour in range(24):
        if hour not in hourly_prices:
            continue
        avg   = sum(hourly_prices[hour]) / len(hourly_prices[hour])
        ratio = (avg - min_p) / p_range
        filled = max(1, int(ratio * bar_width))
        empty  = bar_width - filled
        color  = _price_color(avg, min_p, max_p)
        is_sel = hourly_sel.get(hour, False)

        bar_char  = SEL_CHAR if is_sel else BASE_CHAR
        bar_body  = _c(color + (";1" if is_sel else ""), bar_char * filled) + _dim(EMPTY * empty)
        marker    = _bold(_green(" ◀")) if is_sel else "  "
        price_str = _c(color, f"{avg * 100:5.2f}")

        lines.append(f"  {_bold(f'{hour:02d}:00')}  {price_str}  {bar_body}{marker}")

    lines += [
        sep,
        f"  {_dim('▒ = available    █ = selected (cheapest)    colours: ')}",
        f"  {_dim('min')} {_green(f'{min_p*100:.2f}')} {_dim('avg')} {_yellow(f'{avg_p*100:.2f}')} {_dim('max')} {_red(f'{max_p*100:.2f}')} {_dim('c€/kWh')}",
        "",
    ]
    return "\n".join(lines)


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


def print_plan_summary(plan: dict, all_prices: list[dict]) -> None:
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

    # Price chart
    if all_prices:
        offset = plan["utc_offset_hours"]
        sel_starts: set = set()
        for slot in all_prices:
            local_start = slot["start"] + timedelta(hours=offset)
            hhmm = local_start.strftime("%H:%M")
            for w in wins:
                if w["start"] <= hhmm < w["end"]:
                    sel_starts.add(slot["start"])
                    break
        print(render_price_chart(all_prices, sel_starts, offset))

    print(_bold("  " + "═" * W))
    print()


# ===========================================================================
# GitHub Actions job summary
# ===========================================================================

def render_svg_chart(plan: dict, all_prices: list[dict]) -> str:
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
        h = (slot["start"] + timedelta(hours=offset)).hour
        hourly[h].append(slot["price_eur_kwh"] * 100)

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
    for slot in sorted(all_prices, key=lambda s: s["start"]):
        h_local = (slot["start"] + timedelta(hours=offset)).hour +                   (slot["start"] + timedelta(hours=offset)).minute / 60
        h_end   = h_local + slot["duration_minutes"] / 60
        price_c = slot["price_eur_kwh"] * 100
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


def write_gha_summary(plan: dict, all_prices: list[dict]) -> None:
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
        f"| **Scheduled** | {tot} min |",
        f"| **Avg price** | **{avg:.2f} c\u20ac/kWh** ({savings_pct:.0f}% below market avg) |",
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

# ===========================================================================
# Top-level commands
# ===========================================================================

def _select_spillover(
    outside: list[dict],
    selected: list[dict],
    contiguous_only: bool,
    win_start_local: Optional[str],
    win_end_local: Optional[str],
    target_date: date,
    local_offset: int,
    required_minutes: int,
    remaining: int,
    max_price_eur: Optional[float],
    min_slot_minutes: int,
    all_prices: list[dict],
) -> list[dict]:
    """
    Select spillover slots to cover `remaining` minutes when the preferred
    window alone cannot satisfy `required_minutes`.

    Rules:
    - Never spill after win_end_local (if set).
    - contiguous_only: extend the existing block leftward (earlier slots only).
    - non-contiguous: pick cheapest slots from eligible outside slots.
    """
    selected_ids = {id(s) for s in selected}
    candidates = [s for s in outside if id(s) not in selected_ids]

    # Restrict to slots before the window end when one is configured.
    if win_end_local:
        win_end_utc = _hhmm_to_utc(win_end_local, target_date, local_offset)
        candidates = [s for s in candidates if s["end"] <= win_end_utc]
        log.info(
            "Spillover restricted to slots before window end %s (%d candidates)",
            win_end_local, len(candidates),
        )

    if contiguous_only:
        if win_start_local and selected:
            # Extend leftward: take the slots immediately before the block start.
            block_start_utc = min(s["start"] for s in selected)
            before = sorted(
                [s for s in candidates if s["end"] <= block_start_utc],
                key=lambda x: x["start"],
                reverse=True,
            )
            slot_dur = all_prices[0]["duration_minutes"] if all_prices else 15
            n_extra  = (remaining + slot_dur - 1) // slot_dur
            spillover = list(reversed(before[:n_extra]))
            log.info(
                "contiguous_only spill: extending block %d min earlier (%d slots)",
                remaining, len(spillover),
            )
            return spillover
        # No preferred window start — fall back to best contiguous block.
        # Use select_charging_windows so max_price_eur is respected, and pass
        # all_prices as the pool so boundary-spanning windows are visible.
        filtered = [s for s in all_prices if max_price_eur is None or s["price_eur_kwh"] <= max_price_eur]
        slot_dur = all_prices[0]["duration_minutes"] if all_prices else 15
        n_slots  = (remaining + slot_dur - 1) // slot_dur
        return _best_contiguous_window(filtered, all_prices, n_slots)

    return select_charging_windows(
        candidates,
        required_minutes=remaining,
        contiguous_only=False,
        max_price=max_price_eur,
        min_slot_minutes=min_slot_minutes,
    )


def cmd_plan(config: dict, output_path: str) -> dict:
    """
    Fetch prices, select windows, print the plan, save plan.json.
    Returns the plan dict.
    """
    try:
        validate_plan_config(config)
    except ConfigError as exc:
        log.error("%s", exc)
        sys.exit(1)

    et_cfg = config["entsoe"]
    ch_cfg = config["charging"]

    # target_date is the day whose prices we are planning for.
    # Before noon local time: prices for today were just published (ENTSO-E ~13:00 CET),
    # but we are already into that day — plan for today.
    # After noon: plan for tomorrow (prices not yet published; run will exit cleanly if absent).
    # Heuristic: use local noon as the cutover. We derive the precise offset below once
    # _resolve_tz runs, but we need a date to call _resolve_tz — bootstrap with a UTC guess.
    _now_utc = datetime.now(tz=timezone.utc)
    _tz_guess = ch_cfg.get("timezone")
    _offset_guess = 0
    if _tz_guess:
        try:
            import zoneinfo as _zi_mod
            _offset_guess = int(
                _now_utc.astimezone(_zi_mod.ZoneInfo(_tz_guess)).utcoffset().total_seconds() // 3600
            )
        except Exception:
            pass
    _local_now = _now_utc + timedelta(hours=_offset_guess)
    if _local_now.hour < 12:
        # We are in the early hours of a new day — plan for today
        target_date = _local_now.date()
    else:
        # Daytime/evening — plan for tomorrow
        target_date = (_local_now + timedelta(days=1)).date()
    log.info("Target date: %s (local time: %s)", target_date, _local_now.strftime("%Y-%m-%d %H:%M"))

    # 1. Fetch prices
    # Today's remaining slots are included so leftward spill can reach back
    # into the current evening when required_hours exceeds the preferred window.
    try:
        all_prices   = fetch_entsoe_prices(
            et_cfg["api_key"], et_cfg["area"], target_date,
        )
        price_source = "ENTSO-E"
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

    if not all_prices:
        log.error("No prices available. Cannot build plan.")
        sys.exit(1)

    # 2. Timezone
    tz_name, local_offset = _resolve_tz(ch_cfg.get("timezone"), target_date)
    log.info("Timezone: %s (UTC%+d)", tz_name, local_offset)

    # 3. Trim today's slots to those reachable by leftward spill.
    # The earliest a block can start is: win_start_utc - required_minutes.
    # Slots before that can never be part of the charging plan regardless.
    # Keep all_prices intact for display; use candidate_prices for selection.
    win_start_local  = ch_cfg.get("preferred_window_start")
    required_minutes = int(ch_cfg["required_hours"] * 60)
    min_slot_minutes = int(ch_cfg.get("min_slot_minutes", 30))
    ceil_cents       = ch_cfg.get("max_price_cents_kwh")
    max_price_eur    = ceil_cents / 100.0 if ceil_cents is not None else None

    if win_start_local:
        win_start_utc    = _hhmm_to_utc(win_start_local, target_date, local_offset)
        earliest_useful  = win_start_utc - timedelta(minutes=required_minutes)
        candidate_prices = [s for s in all_prices if s["start"] >= earliest_useful]
        trimmed = len(all_prices) - len(candidate_prices)
        if trimmed:
            log.info(
                "Trimmed %d unreachable today-slots (earliest useful: %s UTC)",
                trimmed, earliest_useful.strftime("%Y-%m-%d %H:%M"),
            )
    else:
        candidate_prices = all_prices

    # 4. Split into preferred-window inside / outside
    inside, outside = filter_preferred_window(
        candidate_prices,
        window_start_local=win_start_local,
        window_end_local=ch_cfg.get("preferred_window_end"),
        local_tz_offset_hours=local_offset,
        target_date=target_date,
    )

    # 5. Select charging slots
    selected = select_charging_windows(
        inside,
        required_minutes=required_minutes,
        contiguous_only=ch_cfg["contiguous_only"],
        max_price=max_price_eur,
        min_slot_minutes=min_slot_minutes,
    )
    sel_min   = sum(s["duration_minutes"] for s in selected)
    remaining = required_minutes - sel_min
    if remaining > 0 and outside:
        log.info("Filling %d min from outside preferred window.", remaining)
        spillover = _select_spillover(
            outside=outside,
            selected=selected,
            contiguous_only=ch_cfg["contiguous_only"],
            win_start_local=win_start_local,
            win_end_local=ch_cfg.get("preferred_window_end"),
            target_date=target_date,
            local_offset=local_offset,
            required_minutes=required_minutes,
            remaining=remaining,
            max_price_eur=max_price_eur,
            min_slot_minutes=min_slot_minutes,
            all_prices=candidate_prices,
        )
        selected = sorted(selected + spillover, key=lambda x: x["start"])

    # 6. Gap merging
    # Note: close_gap_merge uses candidate_prices for price_map lookup.
    # Gap slots that fall outside the trimmed window won't be found and the
    # merge will be silently skipped for that gap — acceptable in practice
    # since such gaps would span the trim boundary (yesterday evening).
    windows       = merge_contiguous_slots(selected)
    merged_starts: set = set()
    if ch_cfg.get("merge_gaps", True) and not ch_cfg["contiguous_only"]:
        selected, merged_starts = close_gap_merge(
            selected, candidate_prices, min_slot_minutes, required_minutes
        )
        windows = merge_contiguous_slots(selected)

    if not selected:
        log.error("No slots selected.")

    # 7. Build plan — price stats should reflect tomorrow only, not today's spill slots
    # Slots are stored as UTC datetimes; convert to local before comparing to target_date,
    # otherwise UTC-offset timezones (e.g. FI UTC+2) silently drop all tomorrow slots.
    tomorrow_prices = [
        s for s in all_prices
        if (s["start"] + timedelta(hours=local_offset)).date() == target_date
    ]
    if not tomorrow_prices:
        log.warning(
            "No prices found for target_date=%s after local-date filter — using full all_prices for stats.",
            target_date,
        )
        tomorrow_prices = list(all_prices)
    plan = build_plan(PlanParams(
        target_date=target_date,
        area=et_cfg["area"],
        price_source=price_source,
        all_prices=tomorrow_prices,
        selected=selected,
        windows=windows,
        required_minutes=required_minutes,
        local_tz_offset_hours=local_offset,
        timezone_name=tz_name,
        merged_starts=merged_starts,
        preferred_window_start=win_start_local,
        preferred_window_end=ch_cfg.get("preferred_window_end"),
    ))

    # 8. Display
    print_plan_summary(plan, tomorrow_prices)

    # 9. Save
    save_plan(plan, output_path)
    print(f"  Plan saved to: {output_path}\n")

    # 10. GHA summary
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
