"""
Loads the Network Rail SCHEDULE (JSON CIF) extract and feeds today's and
tomorrow's trains through the window area into the window tracker.

This adds trains Darwin doesn't carry (freight, engineering, test trains,
light engines) and gives headcodes/origins/destinations to passenger trains
that Darwin only sends TS updates for after a restart (Darwin "uid" is the CIF
train UID).

RDM product: "NWR Schedule" - download CIF_ALL_FULL_DAILY_toc-full.json.gz from
the product's Data files tab and set NWR_SCHEDULE_FILE in .env (e.g.
data/CIF_ALL_FULL_DAILY_toc-full.json). It is re-read shortly after each midnight.
"""
import json
import logging
import os
import threading
import time
from datetime import date, timedelta
from itertools import pairwise

import datafiles
import location
from window_tracker import tracker

log = logging.getLogger(__name__)

# Cancellations and overlays replace a permanent schedule that does pass the window
# even when they don't mention the area themselves (e.g. a diversion away), so they're
# always parsed; other lines only if they mention the area (a cheap pre-filter on a
# multi-GB file)
_OVERRIDE_MARKERS = (b'"CIF_stp_indicator":"C"', b'"CIF_stp_indicator":"O"')

RELOAD_CHECK_SECONDS = 15 * 60


def _cif_time(value: str | None) -> str | None:
    """'1804H' -> '18:04:30', '1805' -> '18:05' (Darwin's working time format)."""
    if not value or len(value) < 4 or not value[:4].isdigit():
        return None
    formatted = f"{value[:2]}:{value[2:4]}"
    return formatted + ":30" if value.endswith("H") else formatted


def _public_time(value: str | None) -> str | None:
    return None if not value or value == "0000" else _cif_time(value)


def _runs_on(schedule: dict, day: date) -> bool:
    try:
        start = date.fromisoformat(schedule["schedule_start_date"])
        end = date.fromisoformat(schedule["schedule_end_date"])
    except (KeyError, ValueError):
        return False
    days = schedule.get("schedule_days_runs") or "1111111"
    return start <= day <= end and days[day.weekday()] == "1"


def _locations(segment: dict) -> list[tuple[str, str, dict]]:
    """(kind, tiploc, times) per timing point; kind is OR/IP/PP/DT like Darwin."""
    locations = []
    for loc in segment.get("schedule_location") or []:
        times = {
            "wta": _cif_time(loc.get("arrival")),
            "wtd": _cif_time(loc.get("departure")),
            "wtp": _cif_time(loc.get("pass")),
            "pta": _public_time(loc.get("public_arrival")),
            "ptd": _public_time(loc.get("public_departure")),
        }
        kind = "PP" if times["wtp"] else {"LO": "OR", "LT": "DT"}.get(loc.get("location_type"), "IP")
        locations.append((kind, loc.get("tiploc_code"), {k: v for k, v in times.items() if v}))
    return locations


def load() -> int:
    """Parse the extract and add matching trains to the tracker. Returns the count loaded."""
    path = datafiles.resolve(os.getenv("NWR_SCHEDULE_FILE"))
    if not path or not path.exists():
        log.info("Network Rail schedule not configured (set NWR_SCHEDULE_FILE); "
                 "freight/engineering trains will only appear via the train describer")
        return 0

    loc = location.get()
    markers = tuple(f'"{tpl}"'.encode() for tpl in loc.area_tiplocs) + _OVERRIDE_MARKERS
    days = [date.today(), date.today() + timedelta(days=1)]
    started = time.monotonic()
    candidates: dict[tuple[str, date], list[dict]] = {}
    with datafiles.open_binary(path) as f:
        for line in f:
            if not any(marker in line for marker in markers):
                continue
            try:
                schedule = json.loads(line).get("JsonScheduleV1")
            except json.JSONDecodeError:
                continue
            if not schedule or schedule.get("transaction_type") == "Delete":
                continue
            for day in days:
                if _runs_on(schedule, day):
                    candidates.setdefault((schedule.get("CIF_train_uid"), day), []).append(schedule)

    loaded = 0
    for (uid, day), schedules in candidates.items():
        # STP precedence: C (cancel), N (new), O (overlay) all beat P (permanent)
        schedule = min(schedules, key=lambda s: s.get("CIF_stp_indicator") or "P")
        cancelled = schedule.get("CIF_stp_indicator") == "C"
        if cancelled:
            # A cancellation has no route of its own; the permanent schedule it cancels does
            schedule = next((s for s in schedules if s.get("CIF_stp_indicator") == "P"), None)
            if not schedule:
                continue
        segment = schedule.get("schedule_segment") or {}
        locations = _locations(segment)
        # Only trains that pass the window: timed there, or at two adjacent points either side of it
        tiplocs = [tpl for _, tpl, _ in locations]
        if not loc.window_tiplocs.intersection(tiplocs) and \
                not any(loc.passes_between(a, b) for a, b in pairwise(tiplocs)):
            continue
        tracker.ingest_planned(
            uid=uid, ssd=day.isoformat(),
            headcode=segment.get("signalling_id"),
            toc=schedule.get("atoc_code"),
            category=segment.get("CIF_train_category"),
            train_status=schedule.get("train_status"),
            power_type=segment.get("CIF_power_type"),
            locations=locations,
            cancelled=cancelled,
            timing_load=segment.get("CIF_timing_load"),
            max_speed=segment.get("CIF_speed"),
        )
        loaded += 1

    log.info("Loaded %d Network Rail scheduled trains through the window area from %s in %.0fs",
             loaded, path.name, time.monotonic() - started)
    return loaded


def start_daily_loader() -> None:
    """Load now, then again shortly after each midnight, in a background thread."""
    def run() -> None:
        loaded_for = None
        while True:
            if loaded_for != date.today():
                try:
                    load()
                except Exception:
                    log.exception("Error loading the Network Rail schedule")
                loaded_for = date.today()
            time.sleep(RELOAD_CHECK_SECONDS)

    threading.Thread(target=run, daemon=True, name="nwr_schedule").start()
