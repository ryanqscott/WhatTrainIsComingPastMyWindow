"""
Network Rail Train Describer (TD): every train's headcode stepping from signal
berth to berth, in (near) real time - including freight, engineering and test
trains that have no Darwin data.

Berth steps are mapped to places using Network Rail's SMART reference data
(TD area + from/to berth -> STANOX + event):
* a step that SMART says is an arrive/depart/pass event at the window's station
  is the train at the window ("signalled");
* a step at a neighbouring station that leads straight into one of the station's
  berths gives an estimate a minute or two ahead ("signalled_estimate"), as do any
  extra approach points in WINDOW_OVERRIDES, e.g. {"WNT": {"approach_points":
  [{"tiploc": "PUTNEY", "event": "B"}]}} for up departures from Putney, which pass
  Wandsworth Town but are in a different TD area.

RDM products: "NWR Train Describer (TD)" (Kafka) and "NWR SMART" (reference file).
Configure in .env:
  NWR_TD_TOPIC, NWR_TD_CONSUMER_GROUP_ID, NWR_TD_CONSUMER_USERNAME,
  NWR_TD_CONSUMER_PASSWORD (and NWR_TD_BOOTSTRAP_SERVERS if it differs from
  KAFKA_BOOTSTRAP_SERVERS)
  NWR_SMART_FILE=path/to/SMARTExtract.json(.gz)

Messages are JSON lists of {"CA_MSG": {...}}, {"SF_MSG": {...}}, ... items; only
the berth messages (CA step, CB cancel, CC interpose) are used.
"""
import json
import logging
import os
from datetime import datetime, timedelta
from typing import NamedTuple

import datafiles
import location
from window_tracker import tracker

log = logging.getLogger(__name__)

# SMART events: A = arrive up, B = depart up, C = arrive down, D = depart down
UP_EVENTS = {"A", "B"}
# Minutes to the window from an approach step when no running time is known
DEFAULT_APPROACH_MINUTES = 1.5

# SMART step types matched by each TD message type:
# CA step: B between / D, E intermediate (from->to), F from (from->any), T to (any->to);
# CB cancel: C clearout (from); CC interpose: I (to)
BERTH_MESSAGE_TYPES = {"CA", "CB", "CC"}


class BerthStep(NamedTuple):
    kind: str          # "window" or "approach"
    tiploc: str
    event: str         # SMART event A/B/C/D
    step_type: str     # SMART step type
    offset: int        # seconds from the berth step to the event
    heading: str | None  # side id the train is heading towards
    minutes_to_window: float


def _load_json(path) -> dict:
    with datafiles.open_binary(path) as f:
        return json.load(f)


def _smart_rows(smart: dict):
    for row in smart.get("BERTHDATA", []):
        try:
            offset = int(str(row.get("BERTHOFFSET") or "0").replace("+", ""))
        except ValueError:
            offset = 0
        yield {key: (row.get(field) or "").strip() for key, field in
               (("td", "TD"), ("from", "FROMBERTH"), ("to", "TOBERTH"), ("stanox", "STANOX"),
                ("event", "EVENT"), ("type", "STEPTYPE"))} | {"offset": offset}


class TrainDescriber:
    def __init__(self) -> None:
        self.steps: dict[tuple[str, str, str], list[BerthStep]] = {}  # (area, from berth, to berth) -> steps
        self.areas: set[str] = set()
        self.stats = {"messages": 0, "berth_messages": 0, "in_our_areas": 0, "matched": 0}

    @property
    def loaded(self) -> bool:
        return bool(self.steps)

    def stats_summary(self) -> dict:
        return dict(self.stats)

    def load_reference(self) -> bool:
        smart_path = datafiles.resolve(os.getenv("NWR_SMART_FILE"))
        if not smart_path or not smart_path.exists():
            log.info("Train describer reference data not configured (set NWR_SMART_FILE)")
            return False
        loc = location.get()
        if not loc.stanox:
            log.warning("No STANOX for %s, so its signalling berths can't be found", loc.name)
            return False

        def heading_for(event: str) -> str | None:
            if not loc.up_side:
                return None
            return loc.up_side if event in UP_EVENTS else loc.other_side(loc.up_side)

        def stanox(tiploc: str) -> str | None:
            entry = location.corpus.get(tiploc)
            return entry.stanox if entry else None

        rows = list(_smart_rows(_load_json(smart_path)))
        window_rows = [r for r in rows if r["stanox"] == loc.stanox]
        # Heading of a train entering each of the station's berths
        heading_into = {(r["td"], r["from"]): heading_for(r["event"]) for r in window_rows if r["from"]}
        for r in window_rows:
            self._add((r["td"], r["from"], r["to"]),
                      BerthStep("window", loc.tiplocs[0], r["event"], r["type"], r["offset"],
                                heading_for(r["event"]), 0))

        # Neighbouring stations' steps that lead straight into one of the station's berths
        neighbour_minutes: dict[str, float] = {}
        for tiploc in loc.area_tiplocs - loc.window_tiplocs:
            st = stanox(tiploc)
            if st and st != loc.stanox and tiploc in loc.run_minutes:
                neighbour_minutes[st] = min(neighbour_minutes.get(st, 99.0), loc.run_minutes[tiploc])
        for r in rows:
            if r["stanox"] in neighbour_minutes and (r["td"], r["to"]) in heading_into:
                self._add((r["td"], r["from"], r["to"]),
                          BerthStep("approach", r["stanox"], r["event"], r["type"], r["offset"],
                                    heading_into[(r["td"], r["to"])], neighbour_minutes[r["stanox"]]))

        # Extra approach points from WINDOW_OVERRIDES: a train leaving there heads across the station
        for point in loc.approach_points:
            tiploc, event = point["tiploc"], point["event"]
            st, side = stanox(tiploc), loc.side_of(tiploc)
            if not side:
                continue
            minutes = point.get("minutes") or loc.run_minutes.get(tiploc) or DEFAULT_APPROACH_MINUTES
            for r in rows:
                if st and r["stanox"] == st and r["event"] == event:
                    self._add((r["td"], r["from"], r["to"]),
                              BerthStep("approach", tiploc, event, r["type"], r["offset"],
                                        loc.other_side(side), minutes))

        self.areas = {key[0] for key in self.steps}
        window_steps = sum(step.kind == "window" for steps in self.steps.values() for step in steps)
        approach_steps = sum(len(steps) for steps in self.steps.values()) - window_steps
        log.info("Train describer: %d berth steps at %s, %d approach steps", window_steps, loc.name, approach_steps)
        if not window_steps:
            log.warning("SMART has no berth steps for %s; only approach sightings will be used", loc.name)
        return self.loaded

    def _add(self, key: tuple[str, str, str], step: BerthStep) -> None:
        self.steps.setdefault(key, []).append(step)

    def handle(self, raw: bytes) -> set[str]:
        """Process one Kafka message value. Returns train keys whose record changed."""
        if not self.loaded:
            return set()
        self.stats["messages"] += 1
        changed: set[str] = set()
        for item in json.loads(raw):
            for msg_key, msg in item.items():
                msg_type = msg_key.removesuffix("_MSG")
                if msg_type in BERTH_MESSAGE_TYPES:
                    self.stats["berth_messages"] += 1
                    changed |= self._step(msg, msg_type)
        return changed

    def _step(self, msg: dict, msg_type: str) -> set[str]:
        headcode = (msg.get("descr") or "").strip()
        if not headcode:
            return set()
        area = (msg.get("area_id") or "").strip()
        from_berth, to_berth = (msg.get("from") or "").strip(), (msg.get("to") or "").strip()
        if area in self.areas:
            self.stats["in_our_areas"] += 1
        if msg_type == "CA":
            candidates = [((area, from_berth, to_berth), {"B", "D", "E"}),
                          ((area, from_berth, ""), {"F"}), ((area, "", to_berth), {"T"})]
        elif msg_type == "CB":
            candidates = [((area, from_berth, ""), {"C"})]
        else:  # CC interpose
            candidates = [((area, "", to_berth), {"I"})]
        matches = [step for key, types in candidates for step in self.steps.get(key, []) if step.step_type in types]
        if not matches:
            return set()
        self.stats["matched"] += 1
        try:
            stepped_at = datetime.fromtimestamp(int(msg.get("time")) // 1000)
        except (TypeError, ValueError):
            stepped_at = datetime.now().replace(microsecond=0)

        changed: set[str] = set()
        for step in matches:
            event_time = stepped_at + timedelta(seconds=step.offset)
            where = f"{area} {from_berth}->{to_berth} ({step.event})"
            if step.kind == "window":
                changed |= tracker.ingest_td_event(headcode, event_time, step.heading, True, where)
            else:
                changed |= tracker.ingest_td_event(headcode, event_time, step.heading, False,
                                                   f"{step.tiploc} {where}",
                                                   eta=event_time + timedelta(minutes=step.minutes_to_window))
        return changed


describer = TrainDescriber()


def td_configured() -> bool:
    return all(os.getenv(k) for k in ("NWR_TD_TOPIC", "NWR_TD_CONSUMER_USERNAME", "NWR_TD_CONSUMER_PASSWORD"))
