"""
Tracks when each train will be at (or pass) the window (see location.py for
which station that is and the railway around it).

Sources, merged per train:
* Darwin (passenger operators, incl. their empty stock moves):
  - uR.schedule: the full timetable (OR/IP/PP/DT and operational OPOR/OPIP/OPDT),
    headcode ("trainId"), category ("trainCat") and "isPassengerSvc".
  - uR.TS: forecasts ("et") / actuals ("at") for arr/dep/pass at each location.
* Network Rail SCHEDULE (nwr_schedule.py): the planned timetable for *all*
  operators - freight, engineering, test trains, light engines - plus the
  headcodes of passenger trains that Darwin only sends TS updates for.
* Network Rail Train Describer (nwr_td.py): signal berth steps, i.e. where a
  train actually is, for every train including ones with no timetable.

Trains are keyed by Darwin RID where known. Timetable-only trains use
"cif:<uid>:<date>" (Darwin's uid is the same CIF uid, so they merge when Darwin
data arrives) and describer-only trains use "td:<headcode>:<time>".

Itinerary locations are keyed by (tiploc, working time) because trains can call
at a tiploc twice on loops. The window time is then:
1. a train describer event at the window, if we've seen one;
2. the train's own entry at the station (stop or pass), actual > estimated > scheduled;
3. if the station isn't timed, an interpolation between adjacent timing points
   that have the station between them (every train timed at one then the other
   passes the window), placed by typical running times.
"""
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from itertools import pairwise

import location
from operators import TOC_NAMES

# Slack on top of the typical running time between two timing points before an
# interpolation is treated as not being a direct run past the window
INTERPOLATION_SLACK = timedelta(minutes=10)

# How close (either side) a describer event must be to a train's expected
# window time to be treated as the same train, since headcodes get reused.
TD_MATCH_WINDOW = timedelta(minutes=45)

# CIF / Darwin train categories -> (description, kind)
TRAIN_CATEGORIES = {
    "OL": ("London Underground / Metro service", "passenger"),
    "OU": ("Unadvertised ordinary passenger", "passenger"),
    "OO": ("Ordinary passenger", "passenger"),
    "OS": ("Staff train", "passenger"),
    "OW": ("Mixed", "passenger"),
    "XC": ("Channel Tunnel", "passenger"),
    "XD": ("Sleeper (Europe night services)", "passenger"),
    "XI": ("International", "passenger"),
    "XR": ("Motorail", "passenger"),
    "XU": ("Unadvertised express", "passenger"),
    "XX": ("Express passenger", "passenger"),
    "XZ": ("Sleeper (domestic)", "passenger"),
    "BR": ("Bus - replacement due to engineering work", "bus"),
    "BS": ("Bus - WTT service", "bus"),
    "SS": ("Ship", "ship"),
    "EE": ("Empty coaching stock", "empty_stock"),
    "EL": ("Empty coaching stock (London Underground / Metro)", "empty_stock"),
    "ES": ("Empty coaching stock and staff", "empty_stock"),
    "JJ": ("Postal", "parcels"),
    "PM": ("Post Office controlled parcels", "parcels"),
    "PP": ("Parcels", "parcels"),
    "PV": ("Empty non-passenger coaching stock", "parcels"),
    "DD": ("Departmental", "engineering"),
    "DH": ("Civil engineer", "engineering"),
    "DI": ("Mechanical & electrical engineer", "engineering"),
    "DQ": ("Stores", "engineering"),
    "DT": ("Test", "engineering"),
    "DY": ("Signal & telecommunications engineer", "engineering"),
    "ZB": ("Locomotive & brake van", "light_locomotive"),
    "ZZ": ("Light locomotive", "light_locomotive"),
    "J2": ("Automotive components", "freight"),
    "H2": ("Automotive vehicles", "freight"),
    "J3": ("Edible products", "freight"),
    "J4": ("Industrial minerals", "freight"),
    "J5": ("Chemicals", "freight"),
    "J6": ("Building materials", "freight"),
    "J8": ("General merchandise", "freight"),
    "H8": ("European freight", "freight"),
    "J9": ("Intermodal (contract)", "freight"),
    "H9": ("Intermodal (other)", "freight"),
    "A0": ("Coal (distributive)", "freight"),
    "E0": ("Coal (electricity) MGR", "freight"),
    "B0": ("Coal (other) and nuclear", "freight"),
    "B1": ("Metals", "freight"),
    "B4": ("Aggregates", "freight"),
    "B5": ("Domestic and industrial waste", "freight"),
    "B6": ("Building materials (train load)", "freight"),
    "B7": ("Petroleum products", "freight"),
    "H0": ("Channel Tunnel freight (mixed business)", "freight"),
    "H1": ("Channel Tunnel freight (intermodal)", "freight"),
    "H3": ("Channel Tunnel freight (automotive)", "freight"),
    "H4": ("Channel Tunnel freight (contract services)", "freight"),
    "H5": ("Channel Tunnel freight (haulmark)", "freight"),
    "H6": ("Channel Tunnel freight (joint venture)", "freight"),
}

# First digit of a headcode, for trains we only know from the train describer
HEADCODE_CLASSES = {
    "0": ("Light locomotive", "light_locomotive"),
    "1": ("Express passenger / priority train", "passenger"),
    "2": ("Ordinary passenger", "passenger"),
    "3": ("Priority empty stock, parcels or infrastructure train", "other"),
    "4": ("Freight (up to 75 mph)", "freight"),
    "5": ("Empty coaching stock", "empty_stock"),
    "6": ("Freight (up to 60 mph)", "freight"),
    "7": ("Freight (up to 45 mph)", "freight"),
    "8": ("Freight (up to 35 mph)", "freight"),
    "9": ("Class 9 (special / international)", "other"),
}

STOP_TYPES = ("OR", "IP", "DT", "OPOR", "OPIP", "OPDT")
PASS_TYPES = ("PP",)


def describe_train_type(category: str | None = None, headcode: str | None = None,
                        is_passenger: bool | None = None) -> tuple[str | None, str | None, bool | None]:
    """Return (description, kind, is_passenger) from a CIF/Darwin category,
    falling back to the headcode's train class."""
    description, kind = TRAIN_CATEGORIES.get(category or "", (None, None))
    if not description and headcode and headcode[:1] in HEADCODE_CLASSES:
        description, kind = HEADCODE_CLASSES[headcode[0]]
    if not category and headcode and len(headcode) > 1 and headcode[0] in "123" and headcode[1] == "Q":
        # Network Rail test / infrastructure monitoring trains (1Q, 2Q, 3Q) seen without a timetable
        description, kind = "Network Rail test / infrastructure monitoring train", "engineering"
    if is_passenger is None and kind:
        is_passenger = kind == "passenger"
    return description, kind, is_passenger


def _as_list(value):
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _text(value):
    """Darwin JSON renders elements with attributes as {'attr': .., '': text}."""
    if isinstance(value, dict):
        return value.get("")
    return value


def _is_true(value):
    return str(value).lower() == "true"


def _to_datetime(ssd, time_str, reference=None):
    """Combine schedule start date + HH:MM[:SS] into a datetime, rolling over
    midnight if the time is well before the reference (e.g. the origin time)."""
    if not ssd or not time_str:
        return None
    try:
        parts = [int(p) for p in time_str.split(":")]
        dt = datetime.strptime(ssd, "%Y-%m-%d").replace(hour=parts[0], minute=parts[1],
                                                        second=parts[2] if len(parts) > 2 else 0)
    except (ValueError, IndexError):
        return None
    if reference and dt < reference - timedelta(hours=12):
        dt += timedelta(days=1)
    return dt


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def _place(tiploc: str | None) -> dict | None:
    return {"tiploc": tiploc, "name": location.station_name(tiploc)} if tiploc else None


def _window_datetime(record: dict | None) -> datetime | None:
    return datetime.fromisoformat(record["window_time"]) if record and record["window_time"] else None


@dataclass
class _Train:
    key: str
    rid: str | None = None
    uid: str | None = None
    headcode: str | None = None
    toc: str | None = None
    ssd: str | None = None
    origin: str | None = None
    destination: str | None = None
    category: str | None = None
    is_passenger: bool | None = None
    power_type: str | None = None
    timing_load: str | None = None   # CIF: class for some units, trailing tonnes for loco-hauled
    max_speed: str | None = None     # CIF: mph
    cancelled: bool = False
    cancel_reason: str | None = None
    late_reason: str | None = None
    deactivated: bool = False
    sources: set[str] = field(default_factory=set)
    locations: dict[tuple[str, str | None], dict] = field(default_factory=dict)  # (tpl, working time) -> entry
    td_window: tuple[datetime, str | None, str] | None = None      # (time, heading side id, berth description)
    td_approach: tuple[datetime, str | None, str] | None = None    # (eta, heading side id, where seen)
    updated: datetime = field(default_factory=datetime.now)

    def location(self, tpl: str, working_time: str | None) -> dict:
        return self.locations.setdefault((tpl, working_time), {"tpl": tpl})


class WindowTracker:
    def __init__(self):
        self.trains = {}
        self.alias = {}           # (uid, ssd) -> train key
        self.lock = threading.Lock()
        self.last_published = {}  # key -> signature of last broadcast record
        self.last_expired = datetime.min

    # ------------------------------------------------------------ train lookup

    def _darwin_train(self, rid, uid, ssd):
        """Get/create the train for a Darwin RID, adopting a timetable-only
        (cif:) train with the same uid/date if we already loaded one."""
        if rid in self.trains:
            return self.trains[rid]
        existing = self.alias.get((uid, ssd)) if uid and ssd else None
        if existing and existing != rid and existing in self.trains:
            train = self.trains.pop(existing)
            self.last_published.pop(existing, None)
        else:
            train = _Train(rid)
        train.key = rid
        train.rid = rid
        self.trains[rid] = train
        if uid and ssd:
            self.alias[(uid, ssd)] = rid
        return train

    def _adopt_td_sightings(self, train):
        """Merge describer-only (td:) trains with the same headcode into `train`.
        Sightings can arrive before the timetable or Darwin schedule that names
        the train, e.g. while the schedule is still loading at startup."""
        if not train.headcode:
            return
        orphans = [k for k, t in self.trains.items()
                   if k.startswith("td:") and k != train.key and t.headcode == train.headcode]
        if not orphans:
            return
        expected = _window_datetime(self._build(train))
        for key in orphans:
            orphan = self.trains[key]
            seen = (orphan.td_window or orphan.td_approach or (None,))[0]
            if expected is None or seen is None or abs(seen - expected) > TD_MATCH_WINDOW:
                continue
            train.td_window = train.td_window or orphan.td_window
            train.td_approach = train.td_approach or orphan.td_approach
            train.sources |= orphan.sources
            del self.trains[key]
            self.last_published.pop(key, None)

    # ------------------------------------------------------------------ Darwin

    def ingest(self, darwin_message: dict) -> set[str]:
        """Process one decoded Darwin message (the parsed 'bytes' JSON).
        Returns the set of train keys whose window record changed."""
        ur = darwin_message.get("uR") if isinstance(darwin_message, dict) else None
        if not isinstance(ur, dict):
            return set()
        touched = set()
        with self.lock:
            for schedule in _as_list(ur.get("schedule")):
                if self._ingest_schedule(schedule):
                    touched.add(schedule["rid"])
            for ts in _as_list(ur.get("TS")):
                if self._ingest_ts(ts):
                    touched.add(ts["rid"])
            for deactivated in _as_list(ur.get("deactivated")):
                rid = deactivated.get("rid") if isinstance(deactivated, dict) else None
                if rid in self.trains:
                    self.trains[rid].deactivated = True
            self._expire()
            return {key for key in touched if self._changed(key)}

    def _ingest_schedule(self, schedule):
        rid = schedule.get("rid")
        entries = []
        for kind in STOP_TYPES + PASS_TYPES:
            for loc in _as_list(schedule.get(kind)):
                if isinstance(loc, dict) and loc.get("tpl"):
                    entries.append((kind, loc))
        if not rid or not any(loc["tpl"] in location.get().area_tiplocs for _, loc in entries):
            return False

        train = self._darwin_train(rid, schedule.get("uid"), schedule.get("ssd"))
        train.sources.add("darwin")
        train.uid = schedule.get("uid") or train.uid
        train.headcode = schedule.get("trainId") or train.headcode
        train.toc = schedule.get("toc") or train.toc
        train.ssd = schedule.get("ssd") or train.ssd
        # Darwin omits these attributes when they have their default values
        train.category = schedule.get("trainCat") or train.category or "OO"
        train.is_passenger = str(schedule.get("isPassengerSvc", "true")).lower() != "false"
        train.cancelled = _is_true(schedule.get("can")) or train.cancelled
        train.cancel_reason = _text(schedule.get("cancelReason")) or train.cancel_reason
        train.updated = datetime.now()

        for kind, loc in entries:
            if kind in ("OR", "OPOR"):
                train.origin = loc["tpl"]
            elif kind in ("DT", "OPDT"):
                train.destination = loc["tpl"]
            working = loc.get("wta") or loc.get("wtd") or loc.get("wtp")
            entry = train.location(loc["tpl"], working)
            entry.update({k: loc[k] for k in ("wta", "wtd", "wtp", "pta", "ptd") if loc.get(k)})
            entry["stops"] = kind in STOP_TYPES
            entry["cancelled"] = _is_true(loc.get("can"))
        self._adopt_td_sightings(train)
        return True

    def _ingest_ts(self, ts):
        rid = ts.get("rid")
        area_tiplocs = location.get().area_tiplocs
        area = [loc for loc in _as_list(ts.get("Location"))
                if isinstance(loc, dict) and loc.get("tpl") in area_tiplocs]
        known = rid in self.trains or (ts.get("uid"), ts.get("ssd")) in self.alias
        if not rid or (not area and not known):
            return False

        train = self._darwin_train(rid, ts.get("uid"), ts.get("ssd"))
        train.sources.add("darwin")
        train.uid = ts.get("uid") or train.uid
        train.ssd = ts.get("ssd") or train.ssd
        train.late_reason = _text(ts.get("LateReason")) or train.late_reason
        train.updated = datetime.now()

        for loc in area:
            working = loc.get("wta") or loc.get("wtd") or loc.get("wtp")
            entry = train.location(loc["tpl"], working)
            entry.update({k: loc[k] for k in ("wta", "wtd", "wtp", "pta", "ptd") if loc.get(k)})
            entry.setdefault("stops", not loc.get("wtp"))
            for block in ("arr", "dep", "pass"):
                data = loc.get(block)
                if isinstance(data, dict):
                    entry[f"{block}_et"] = data.get("et") or data.get("wet") or entry.get(f"{block}_et")
                    if data.get("at"):
                        entry[f"{block}_at"] = data["at"]
                    entry[f"{block}_delayed"] = _is_true(data.get("delayed"))
            if loc.get("plat") is not None:
                entry["platform"] = _text(loc["plat"])
            if loc.get("length"):
                entry["length"] = _text(loc["length"])
        return True

    # -------------------------------------------------- Network Rail timetable

    def ingest_planned(self, uid: str, ssd: str, headcode: str | None, toc: str | None, category: str | None,
                       train_status: str | None, power_type: str | None,
                       locations: list[tuple[str, str, dict]], cancelled: bool = False,
                       timing_load: str | None = None, max_speed: str | None = None) -> None:
        """Add a train from the Network Rail (CIF) timetable. `locations` is a
        list of (kind, tiploc, times) with kind in OR/IP/PP/DT and times holding
        Darwin-style wta/wtd/wtp/pta/ptd strings. Darwin data, when it arrives,
        takes precedence."""
        with self.lock:
            key = self.alias.get((uid, ssd)) or f"cif:{uid}:{ssd}"
            train = self.trains.get(key)
            if train is None:
                train = _Train(key)
                self.trains[key] = train
                self.alias[(uid, ssd)] = key
            darwin_known = "darwin" in train.sources
            train.sources.add("timetable")
            train.uid = uid
            train.ssd = ssd
            train.headcode = train.headcode or headcode
            train.toc = train.toc or toc
            train.power_type = power_type
            train.timing_load = timing_load
            train.max_speed = max_speed
            if not darwin_known:
                train.category = category or train.category
                # Train status P/1 = passenger & parcels, F/2 freight, T/3 trip, B/5 bus
                train.is_passenger = None if train_status not in ("F", "2", "T", "3") else False
                train.cancelled = cancelled
            elif category and train.category in (None, "OO") and category != "OO":
                train.category = category

            for kind, tpl, times in locations:
                if kind == "OR":
                    train.origin = train.origin or tpl
                elif kind == "DT":
                    train.destination = train.destination or tpl
                working = times.get("wta") or times.get("wtd") or times.get("wtp")
                loc_key = (tpl, working)
                if loc_key in train.locations and darwin_known:
                    continue  # Darwin's own schedule is more current
                entry = train.location(tpl, working)
                entry.update(times)
                entry["stops"] = kind != "PP"
            self._adopt_td_sightings(train)

    # ----------------------------------------------- Network Rail describer

    def ingest_td_event(self, headcode: str, event_time: datetime, heading: str | None, at_window: bool,
                        description: str, eta: datetime | None = None) -> set[str]:
        """Record a train describer observation. `at_window` events are the train
        at the station; others are sightings nearby with an `eta`. `heading` is the
        side id the train is heading towards, if known.
        Returns the set of train keys whose window record changed."""
        with self.lock:
            target_time = event_time if at_window else eta
            best_key, best_gap = None, None
            for key, train in self.trains.items():
                if train.headcode != headcode:
                    continue
                expected = _window_datetime(self._build(train))
                if expected is None:
                    continue
                gap = abs(expected - target_time)
                if gap <= TD_MATCH_WINDOW and (best_gap is None or gap < best_gap):
                    best_key, best_gap = key, gap

            if best_key is None:
                if not at_window:
                    return set()  # only create describer-only trains from sightings at the window
                best_key = f"td:{headcode}:{event_time:%Y%m%d%H%M}"
                train = self.trains.setdefault(best_key, _Train(best_key))
                train.headcode = headcode
            train = self.trains[best_key]
            train.sources.add("train_describer")
            train.updated = datetime.now()
            if at_window:
                if train.td_window is None:  # first berth step at the window is when it's there
                    train.td_window = (event_time, heading, description)
            else:
                train.td_approach = (eta, heading, description)
            return {best_key} if self._changed(best_key) else set()

    # ------------------------------------------------------------ bookkeeping

    def _expire(self):
        now = datetime.now()
        if now - self.last_expired < timedelta(minutes=5):
            return
        self.last_expired = now
        for key, train in list(self.trains.items()):
            window_time = _window_datetime(self._build(train))
            if (window_time and window_time < now - timedelta(hours=2)) or \
                    (not window_time and train.updated < now - timedelta(hours=6)):
                self.trains.pop(key, None)
                self.last_published.pop(key, None)
        live_keys = set(self.trains)
        self.alias = {k: v for k, v in self.alias.items() if v in live_keys}

    def _changed(self, key):
        record = self._build(self.trains.get(key))
        if record is None:
            return False
        signature = (record["window_time"], record["window_time_source"], record["platform"],
                     record["cancelled"], record["headcode"], record["length"], record["category"])
        if self.last_published.get(key) == signature:
            return False
        self.last_published[key] = signature
        return True

    # ------------------------------------------------------------------- query

    def record(self, key: str) -> dict | None:
        with self.lock:
            return self._build(self.trains.get(key))

    def board(self, minutes_ahead: int = 90, minutes_behind: int = 2) -> list[dict]:
        """Upcoming trains past the window, soonest first."""
        now = datetime.now()
        with self.lock:
            records = [self._build(t) for t in self.trains.values() if not t.deactivated]
        earliest, latest = now - timedelta(minutes=minutes_behind), now + timedelta(minutes=minutes_ahead)
        records = [r for r in records if r and r["window_time"] and earliest <= _window_datetime(r) <= latest]
        return sorted(records, key=lambda r: r["window_time"])

    def _build(self, train: _Train | None) -> dict | None:
        if train is None:
            return None
        loc = location.get()
        located = sorted(((e.get("wta") or e.get("wtd") or e.get("wtp") or "", e) for e in train.locations.values()),
                         key=lambda x: x[0])
        reference = _to_datetime(train.ssd, located[0][0]) if located and located[0][0] else None

        def best(entry, kind):
            """(time, source) for arr/dep/pass at an itinerary entry."""
            working = {"arr": "wta", "dep": "wtd", "pass": "wtp"}[kind]
            for suffix, source in (("at", "actual"), ("et", "estimated")):
                if entry.get(f"{kind}_{suffix}"):
                    return _to_datetime(train.ssd, entry[f"{kind}_{suffix}"], reference), source
            if entry.get(working):
                return _to_datetime(train.ssd, entry[working], reference), "scheduled"
            return None, None

        window = next((e for _, e in located if e["tpl"] in loc.window_tiplocs), None)
        arrival = departure = passing = None
        window_time = source = None
        sources = {}
        stops = None
        if window:
            stops = window.get("stops", False) and not window.get("wtp")
            if stops:
                arrival, sources["arrival"] = best(window, "arr")
                departure, sources["departure"] = best(window, "dep")
                window_time, source = (arrival, sources["arrival"]) if arrival else (departure, sources["departure"])
            else:
                passing, sources["pass"] = best(window, "pass")
                window_time, source = passing, sources["pass"]
        elif located:
            window_time, source = self._interpolate(loc, located, best)
            if window_time:
                stops = False
                passing = window_time
                sources["pass"] = source

        # Train describer: an actual sighting at the window beats everything;
        # a sighting nearby beats timetable-based estimates.
        td_heading = None
        if train.td_window:
            window_time, source, td_heading = train.td_window[0], "signalled", train.td_window[1]
        elif train.td_approach and source not in ("actual",):
            window_time, source, td_heading = train.td_approach[0], "signalled_estimate", train.td_approach[1]

        if window_time is None:
            return None

        # Heading: towards the side of the nearest timing point if the train reaches it
        # after the window, else away from it; failing that the platform, then signalling
        heading = None
        neighbours = []
        for _, entry in located:
            side = loc.side_of(entry["tpl"])
            if side:
                t, _src = best(entry, "arr" if entry.get("wta") else ("pass" if entry.get("wtp") else "dep"))
                if t:
                    neighbours.append((abs((t - window_time).total_seconds()), side, t))
        if neighbours:
            _, side, t = min(neighbours)
            heading = side if t > window_time else loc.other_side(side)
        if heading is None and window:
            heading = loc.platform_side.get(str(window.get("platform")))
        heading = heading or td_heading

        type_description, kind, is_passenger = describe_train_type(train.category, train.headcode, train.is_passenger)

        return {
            "id": train.key,
            "rid": train.rid,
            "uid": train.uid,
            "headcode": train.headcode,
            "toc": train.toc,
            "operator": TOC_NAMES.get(train.toc, train.toc),
            "category": train.category,
            "category_description": type_description,
            "train_kind": kind,
            "is_passenger": is_passenger,
            "power_type": train.power_type,
            "timing_load": train.timing_load,
            "max_speed_mph": int(train.max_speed) if str(train.max_speed or "").isdigit() else None,
            "data_sources": sorted(train.sources),
            "origin": _place(train.origin),
            "destination": _place(train.destination),
            "stops_at_station": stops,
            "passes_without_stopping": None if stops is None else not stops,
            "window_time": _iso(window_time),
            "window_time_source": source,
            "arrival": _iso(arrival),
            "departure": _iso(departure),
            "pass_time": _iso(passing),
            "time_sources": sources,
            "scheduled": {k: window.get(k) for k in ("wta", "wtd", "wtp", "pta", "ptd") if window and window.get(k)},
            "signalling": {
                "seen_at_window": _iso(train.td_window[0]) if train.td_window else None,
                "last_seen_nearby": train.td_approach[2] if train.td_approach else None,
            } if (train.td_window or train.td_approach) else None,
            "direction": loc.label(heading),
            "direction_side": heading,
            "platform": window.get("platform") if window else None,
            # Darwin gives the number of coaches at stops; fall back to nearby timing points
            "length": (window.get("length") if window else None)
                      or next((e.get("length") for _, e in located if e.get("length")), None),
            "cancelled": bool(train.cancelled or (window and window.get("cancelled"))),
            "cancel_reason": train.cancel_reason,
            "late_reason": train.late_reason,
            "delayed": bool(window and any(window.get(f"{b}_delayed") for b in ("arr", "dep", "pass"))),
        }

    @staticmethod
    def _interpolate(loc: location.Location, located: list, best) -> tuple[datetime | None, str | None]:
        """Estimate the passing time from adjacent timing points that have the station
        between them (e.g. Clapham Junction then Putney), placed by typical running times."""
        for (_, a), (_, b) in pairwise(located):
            if not loc.passes_between(a["tpl"], b["tpl"]):
                continue
            ta, _ = best(a, "dep" if a.get("wtd") else "pass")
            tb, _ = best(b, "arr" if b.get("wta") else "pass")
            typical = timedelta(minutes=loc.run_minutes.get(a["tpl"], 0) + loc.run_minutes.get(b["tpl"], 0))
            if ta and tb and timedelta(0) <= tb - ta <= typical + INTERPOLATION_SLACK:
                return (ta + (tb - ta) * loc.position(a["tpl"], b["tpl"])).replace(microsecond=0), "interpolated"
        return None, None


tracker = WindowTracker()
