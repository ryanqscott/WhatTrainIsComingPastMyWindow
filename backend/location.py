"""
The window's location: which station (or TIPLOCs) it overlooks, and everything
about the railway around it that the tracker needs, derived from Network Rail
data so the backend works for any station.

Set WINDOW_STATION in .env to a station's CRS code (e.g. WNT, SUR) or a TIPLOC
(e.g. WDWTOWN). At startup this derives, from CORPUS, the SCHEDULE extract and
(optionally) SMART:

* the station's TIPLOCs (every TIPLOC sharing its STANOX) and name;
* the nearby timing points, split into the two sides of the station, from the
  timing points trains pass just before and after it;
* which pairs of timing points have the station directly between them - a train
  timed at one then the other must pass the window, even if it isn't timed there;
* typical running times from the station to each nearby timing point, used to
  place the window between two timing points;
* a name for each direction ("towards Putney"), from the nearest station that way;
* which platform serves which direction (timetable platforms) and which side is
  "up" (SMART events at the station's platforms).

The result is cached in cache/location_<code>.json until the data files change.
Anything can be overridden per station with WINDOW_OVERRIDES (JSON keyed by the
WINDOW_STATION value, so switching station doesn't carry another's tweaks), e.g.
  WINDOW_OVERRIDES={"WNT": {"sides": {"a": {"towards": "London"}}, "run_minutes": {"BARNES": 5.5},
                            "approach_points": [{"tiploc": "PUTNEY", "event": "B"}]}}
"""
import json
import logging
import os
import statistics
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from itertools import combinations
from pathlib import Path

import datafiles

log = logging.getLogger(__name__)

CACHE_DIR = Path(__file__).resolve().parent / "cache"
CACHE_VERSION = 1

DEFAULT_STATION = "WNT"
# Timing points within this many minutes of the station count as "nearby"
AREA_MINUTES = 8
# ...and must be seen this often around the station
MIN_SAMPLES = 3
# Only timing points this close either side are used to work out the two sides
SIDE_LINK_MINUTES = 15
# SMART events: A arrive up, B depart up, C arrive down, D depart down
UP_EVENTS = {"A", "B"}


@dataclass
class Side:
    id: str                    # "a" or "b"
    towards: str               # name of the nearest station that way
    tiplocs: list[str]         # nearby timing points on this side

    @property
    def label(self) -> str:
        return f"towards {self.towards}"


@dataclass
class Location:
    code: str
    name: str
    tiplocs: list[str]                       # the station's TIPLOCs
    stanox: str | None
    sides: list[Side]                        # [side a, side b]
    run_minutes: dict[str, float]            # typical running time between the station and a nearby timing point
    through_pairs: list[tuple[str, str]]     # adjacent timing points seen with the station directly between them
    platform_side: dict[str, str]            # platform -> side id trains on it are heading towards
    up_side: str | None = None               # side id "up" trains (SMART events A/B) head towards
    approach_points: list[dict] = field(default_factory=list)  # extra Train Describer approach sightings

    def __post_init__(self) -> None:
        self._side_of = {tpl: side.id for side in self.sides for tpl in side.tiplocs}
        self._pairs = {tuple(pair) for pair in self.through_pairs}
        self.window_tiplocs = set(self.tiplocs)
        self.area_tiplocs = self.window_tiplocs | set(self._side_of)

    def side_of(self, tiploc: str) -> str | None:
        return self._side_of.get(tiploc)

    def other_side(self, side_id: str | None) -> str | None:
        return {"a": "b", "b": "a"}.get(side_id or "")

    def label(self, side_id: str | None) -> str | None:
        return next((side.label for side in self.sides if side.id == side_id), None)

    def passes_between(self, before: str, after: str) -> bool:
        """True if a train timed at `before` then `after` must pass the window."""
        return (before, after) in self._pairs

    def position(self, before: str, after: str) -> float:
        """Fraction of the way from `before` to `after` at which a train reaches the window."""
        a, b = self.run_minutes.get(before), self.run_minutes.get(after)
        return a / (a + b) if a and b else 0.5

    def to_client(self) -> dict:
        """What the frontend needs: names and the two directions."""
        return {"code": self.code, "name": self.name,
                "sides": [{"id": s.id, "towards": s.towards, "label": s.label} for s in self.sides]}


# ---------------------------------------------------------------------------- CORPUS

@dataclass
class CorpusEntry:
    stanox: str | None
    crs: str | None
    name: str


corpus: dict[str, CorpusEntry] = {}     # TIPLOC -> entry
_stanox_tiplocs: dict[str, set[str]] = defaultdict(set)


def load_corpus() -> bool:
    path = datafiles.resolve(os.getenv("NWR_CORPUS_FILE"))
    if not path or not path.exists():
        return False
    with datafiles.open_binary(path) as f:
        rows = json.load(f).get("TIPLOCDATA", [])
    for row in rows:
        tiploc = (row.get("TIPLOC") or "").strip()
        if not tiploc:
            continue
        stanox = (row.get("STANOX") or "").strip() or None
        crs = (row.get("3ALPHA") or "").strip() or None
        corpus[tiploc] = CorpusEntry(stanox, crs, (row.get("NLCDESC") or "").strip().title() or tiploc)
        if stanox:
            _stanox_tiplocs[stanox].add(tiploc)
    return True


def _station_entry(tiploc: str) -> CorpusEntry | None:
    """The passenger station a timing point belongs to: itself if it has a CRS code,
    else a CRS-coded TIPLOC sharing its STANOX and first word (CLPHMJW, "Clapham Jn
    (Windsor)" -> CLPHMJN "Clapham Junction"), else None (a junction or siding)."""
    entry = corpus.get(tiploc)
    if not entry:
        return None
    if entry.crs:
        return entry
    first_word = entry.name.split()[0] if entry.name else ""
    for other in sorted(_stanox_tiplocs.get(entry.stanox or "", ())):
        candidate = corpus[other]
        if candidate.crs and candidate.name.split()[0] == first_word:
            return candidate
    return None


def station_name(tiploc: str) -> str:
    """A tidy name for a timing point, without CORPUS's "(Windsor)" / " London" suffixes."""
    entry = _station_entry(tiploc) or corpus.get(tiploc)
    if not entry:
        return tiploc
    name = entry.name.split(" (")[0]
    return name.removesuffix(" London") if name != "London" else name


def _station_tiplocs(code: str) -> tuple[list[str], str | None, str]:
    """TIPLOCs, STANOX and name for a CRS code or TIPLOC."""
    code = code.strip().upper()
    matches = [tpl for tpl, entry in corpus.items() if entry.crs == code] or ([code] if code in corpus else [])
    if not matches:
        raise ValueError(f"WINDOW_STATION={code} isn't a CRS code or TIPLOC in CORPUS")
    stanox = corpus[matches[0]].stanox
    tiplocs = sorted(_stanox_tiplocs[stanox]) if stanox else matches
    return tiplocs, stanox, corpus[matches[0]].name


# ---------------------------------------------------------------------------- derivation

def _minutes(value: str | None) -> float | None:
    """CIF time '1804H' -> minutes since midnight (1084.5)."""
    if not value or len(value) < 4 or not value[:4].isdigit():
        return None
    return int(value[:2]) * 60 + int(value[2:4]) + (0.5 if value.endswith("H") else 0)


class _ParityUnion:
    """Union-find tracking whether two timing points are on the same or opposite sides."""

    def __init__(self) -> None:
        self.parent: dict[str, str] = {}
        self.parity: dict[str, int] = {}

    def find(self, x: str) -> tuple[str, int]:
        if self.parent.setdefault(x, x) == x:
            self.parity.setdefault(x, 0)
            return x, 0
        root, p = self.find(self.parent[x])
        self.parent[x] = root
        self.parity[x] ^= p
        return root, self.parity[x]

    def union(self, x: str, y: str, opposite: bool) -> None:
        (rx, px), (ry, py) = self.find(x), self.find(y)
        if rx != ry:  # links contradicting stronger evidence already merged are ignored
            self.parent[ry] = rx
            self.parity[ry] = px ^ py ^ int(opposite)


def _scan_timetable(window: set[str], cif_path: Path) -> dict:
    markers = tuple(f'"{tpl}"'.encode() for tpl in window)
    runs: dict[str, list[float]] = defaultdict(list)
    pair_counts: Counter = Counter()
    links: Counter = Counter()           # (x, y, opposite) -> count
    platforms: list[tuple[str, str]] = []  # (platform at the station, next timing point)
    with datafiles.open_binary(cif_path) as f:
        for line in f:
            if not any(marker in line for marker in markers):
                continue
            schedule = json.loads(line).get("JsonScheduleV1") or {}
            if schedule.get("transaction_type") == "Delete":
                continue
            points, last = [], None
            for loc in (schedule.get("schedule_segment") or {}).get("schedule_location") or []:
                arrive = _minutes(loc.get("pass") or loc.get("arrival") or loc.get("departure"))
                leave = _minutes(loc.get("pass") or loc.get("departure") or loc.get("arrival"))
                if arrive is None or leave is None:
                    continue
                if last is not None and arrive < last - 600:  # past midnight
                    arrive, leave = arrive + 1440, leave + 1440
                leave = max(leave, arrive)
                last = leave
                points.append((loc.get("tiploc_code"), arrive, leave, loc.get("platform")))
            index = next((i for i, p in enumerate(points) if p[0] in window), None)
            if index is None or index == 0 or index == len(points) - 1:
                continue
            _, s_arrive, s_leave, platform = points[index]
            before = [(p[0], s_arrive - p[2]) for p in reversed(points[:index])
                      if p[0] not in window and s_arrive - p[2] <= SIDE_LINK_MINUTES]
            after = [(p[0], p[1] - s_leave) for p in points[index + 1:]
                     if p[0] not in window and p[1] - s_leave <= SIDE_LINK_MINUTES]
            for tpl, minutes in before + after:
                runs[tpl].append(minutes)
            if before and after:
                prev_tpl, next_tpl = before[0][0], after[0][0]
                if points[index - 1][0] == prev_tpl and points[index + 1][0] == next_tpl:
                    pair_counts[(prev_tpl, next_tpl)] += 1
                links[(prev_tpl, next_tpl, True)] += 1
                if platform:
                    platforms.append((str(platform).strip(), next_tpl))
            for group in (before, after):
                for (x, _), (y, _) in combinations(group, 2):
                    links[(x, y, False)] += 1
    return {"runs": runs, "pairs": pair_counts, "links": links, "platforms": platforms}


def _smart_rows() -> list[dict]:
    path = datafiles.resolve(os.getenv("NWR_SMART_FILE"))
    if not path or not path.exists():
        return []
    with datafiles.open_binary(path) as f:
        rows = json.load(f).get("BERTHDATA", [])
    fields = (("td", "TD"), ("from", "FROMBERTH"), ("to", "TOBERTH"), ("stanox", "STANOX"),
              ("event", "EVENT"), ("platform", "PLATFORM"))
    return [{key: (row.get(name) or "").strip() for key, name in fields} for row in rows]


def _up_votes_from_smart_adjacency(rows: list[dict], stanox: str, group_of: dict[str, int]) -> Counter:
    """A neighbour's berth step leading straight into one of the station's steps shows
    which way that train is heading, and the station step's event says whether that's up.
    E.g. a Clapham Junction departure into a Wandsworth Town "down arrival": trains from
    the Clapham side are down trains, so the Clapham side is up."""
    tiploc_stanox = {tpl: corpus[tpl].stanox for tpl in group_of if tpl in corpus}
    group_of_stanox = {st: group_of[tpl] for tpl, st in tiploc_stanox.items() if st and st != stanox}
    window_steps = {(r["td"], r["from"]): r["event"] for r in rows if r["stanox"] == stanox and r["from"]}
    votes = Counter()
    for r in rows:
        came_from = group_of_stanox.get(r["stanox"])
        event = window_steps.get((r["td"], r["to"]))
        if came_from is not None and event:
            heading = 1 - came_from
            votes[heading if event in UP_EVENTS else came_from] += 1
    return votes


def derive(code: str, cif_path: Path) -> Location:
    tiplocs, stanox, name = _station_tiplocs(code)
    window = set(tiplocs)
    scan = _scan_timetable(window, cif_path)
    runs, links = scan["runs"], scan["links"]
    if not runs:
        raise ValueError(f"No trains in the timetable pass {code} ({', '.join(tiplocs)})")

    # Split nearby timing points into two sides, strongest evidence first
    union = _ParityUnion()
    for (x, y, opposite), _count in links.most_common():
        union.union(x, y, opposite)
    nearby = {tpl: statistics.median(minutes) for tpl, minutes in runs.items()
              if len(minutes) >= MIN_SAMPLES and statistics.median(minutes) <= AREA_MINUTES}
    if not nearby:
        raise ValueError(f"Couldn't find timing points near {code}")
    busiest = max(nearby, key=lambda tpl: len(runs[tpl]))
    root = union.find(busiest)[0]
    groups: dict[int, list[str]] = {0: [], 1: []}
    for tpl in nearby:
        tpl_root, parity = union.find(tpl)
        if tpl_root == root:
            groups[parity].append(tpl)

    # Platform -> heading group (timetable platforms at the station)
    group_of = {tpl: parity for parity, members in groups.items() for tpl in members}
    platform_votes: dict[str, Counter] = defaultdict(Counter)
    for platform, next_tpl in scan["platforms"]:
        if next_tpl in group_of:
            platform_votes[platform][group_of[next_tpl]] += 1
    platform_group = {platform: votes.most_common(1)[0][0] for platform, votes in platform_votes.items()}

    # Which group is "up": SMART's up events happen at the platforms of up trains,
    # and neighbours' steps leading into the station's steps show the direction too
    smart = _smart_rows()
    smart_events = [(r["platform"], r["event"]) for r in smart if r["stanox"] == stanox and r["platform"]]
    up_votes = _up_votes_from_smart_adjacency(smart, stanox, group_of) if stanox else Counter()
    for platform, event in smart_events:
        if platform in platform_group:
            up_votes[platform_group[platform] if event in UP_EVENTS else 1 - platform_group[platform]] += 1
    up_group = up_votes.most_common(1)[0][0] if up_votes else None
    # Side "a" is the up side when known
    a_group = up_group if up_group is not None else 0
    side_id = {a_group: "a", 1 - a_group: "b"}
    for platform, event in smart_events:  # platforms only SMART knows about
        if up_group is not None and platform not in platform_group:
            platform_group[platform] = up_group if event in UP_EVENTS else 1 - up_group

    def towards(members: list[str]) -> str:
        """The busiest passenger station close to the nearest timing point that way."""
        if not members:
            return "?"
        nearest = min(nearby[tpl] for tpl in members)
        close = [tpl for tpl in members if nearby[tpl] <= nearest + 3]
        stations = [tpl for tpl in close if _station_entry(tpl)]
        if stations:
            return station_name(max(stations, key=lambda tpl: len(runs[tpl])))
        # No station close by (e.g. a junction-heavy area): the nearest station further out, else the nearest point
        further = [tpl for tpl in members if _station_entry(tpl)]
        return station_name(min(further or members, key=lambda tpl: nearby[tpl]))

    sides = [Side(side_id[g], towards(groups[g]), sorted(groups[g], key=lambda tpl: nearby[tpl]))
             for g in sorted(groups, key=lambda g: side_id[g])]
    # Pairs seen with the station between them, on opposite sides (a route passes in both directions)
    through_pairs = sorted({pair for a, b in scan["pairs"] for pair in ((a, b), (b, a))
                            if a in group_of and b in group_of and group_of[a] != group_of[b]})
    return Location(
        code=code.upper(), name=name, tiplocs=tiplocs, stanox=stanox, sides=sides,
        run_minutes={tpl: round(minutes, 2) for tpl, minutes in nearby.items()},
        through_pairs=through_pairs,
        platform_side={platform: side_id[g] for platform, g in sorted(platform_group.items())},
        up_side=side_id[up_group] if up_group is not None else None,
    )


# ---------------------------------------------------------------------------- configuration

current: Location | None = None


def _cache_key(code: str, *paths: Path | None) -> str:
    stamps = [f"{p.name}:{p.stat().st_mtime_ns}:{p.stat().st_size}" for p in paths if p and p.exists()]
    return json.dumps([CACHE_VERSION, code.upper(), stamps])


def _apply_overrides(location: Location, overrides: dict) -> Location:
    data = asdict(location)
    for side in data["sides"]:
        side.update((overrides.get("sides") or {}).get(side["id"], {}))
    for key in ("run_minutes", "platform_side"):
        data[key].update(overrides.get(key) or {})
    for key in ("name", "up_side", "approach_points"):
        if key in overrides:
            data[key] = overrides[key]
    if "through_pairs" in overrides:
        data["through_pairs"] = [tuple(pair) for pair in overrides["through_pairs"]]
    data["sides"] = [Side(**side) for side in data["sides"]]
    data["through_pairs"] = [tuple(pair) for pair in data["through_pairs"]]
    return Location(**data)


def configure() -> Location:
    """Load CORPUS and derive (or load the cached) window location. Call once at startup."""
    global current
    if not load_corpus():
        raise RuntimeError("NWR_CORPUS_FILE is required to work out the window location")
    code = (os.getenv("WINDOW_STATION") or DEFAULT_STATION).strip().upper()
    cif_path = datafiles.resolve(os.getenv("NWR_SCHEDULE_FILE"))
    smart_path = datafiles.resolve(os.getenv("NWR_SMART_FILE"))
    if not cif_path or not cif_path.exists():
        raise RuntimeError("NWR_SCHEDULE_FILE is required to work out the window location")

    cache_file = CACHE_DIR / f"location_{code}.json"
    key = _cache_key(code, cif_path, smart_path)
    location = None
    if cache_file.exists():
        try:
            cached = json.loads(cache_file.read_text(encoding="utf-8"))
            if cached.get("key") == key:
                data = cached["location"]
                data["sides"] = [Side(**side) for side in data["sides"]]
                data["through_pairs"] = [tuple(pair) for pair in data["through_pairs"]]
                location = Location(**data)
        except (OSError, ValueError, KeyError, TypeError) as e:
            log.warning("Ignoring unreadable %s: %s", cache_file.name, e)
    if location is None:
        log.info("Working out the railway around %s from %s...", code, cif_path.name)
        location = derive(code, cif_path)
        CACHE_DIR.mkdir(exist_ok=True)
        cache_file.write_text(json.dumps({"key": key, "location": asdict(location)}, indent=1), encoding="utf-8")

    overrides = json.loads(os.getenv("WINDOW_OVERRIDES") or "{}").get(code)
    if overrides:
        location = _apply_overrides(location, overrides)
    for point in location.approach_points:
        if not location.side_of(point.get("tiploc", "")):
            log.warning("Approach point %s isn't a timing point near %s; it will be ignored",
                        point.get("tiploc"), location.name)

    current = location
    log.info("Window at %s (%s): %s / %s; up side %s", location.name, ", ".join(location.tiplocs),
             location.sides[0].label, location.sides[1].label, location.up_side or "unknown")
    return location


def get() -> Location:
    if current is None:
        raise RuntimeError("location.configure() hasn't been called")
    return current


if __name__ == "__main__":
    # python location.py [CODE]  - print what would be derived for a station
    import sys

    from dotenv import load_dotenv
    load_dotenv()
    logging.basicConfig(level=logging.INFO)
    if len(sys.argv) > 1:
        os.environ["WINDOW_STATION"] = sys.argv[1]
    print(json.dumps(asdict(configure()), indent=2))
