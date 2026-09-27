"""
What a train past the window looks like, at the best confidence available:

1. "exact"  - LINX allocated units for this train: unit numbers, each unit's
              livery (SpotLog) and photos of those units (Wikimedia Commons).
2. "likely" - no allocation, but LINX history shows which class normally works
              this service group (headcode prefix, e.g. 2U = Windsor = 458/4).
              Class-level livery (what most of the class carries), photo of the
              class in that livery, and the Wikipedia class summary.
3. "timetable" - loco-hauled trains (freight, engineering, test trains): the
              Network Rail timetable's traction type, trailing load and speed.
"""
import threading
from collections import Counter, defaultdict
from datetime import datetime

import rolling_stock
from operators import LINX_CODE_FOR_TOC

CLASS_NICKNAMES = {
    "701": "Arterio", "707": "Desiro City", "458": "Juniper", "450": "Desiro", "444": "Desiro",
    "387": "Electrostar", "377": "Electrostar", "345": "Aventra", "710": "Aventra",
    "720": "Aventra", "800": "Azuma / IET", "802": "IET", "158": "Express Sprinter",
}

POWER_TYPES = {
    "D": "diesel locomotive", "E": "electric locomotive", "ED": "electro-diesel locomotive",
    "EML": "EMU plus locomotive", "EMU": "electric multiple unit", "DMU": "diesel multiple unit",
    "HST": "High Speed Train", "DEM": "diesel-electric multiple unit",
}

# Freight yard names in CORPUS often end with the operator's initials
FREIGHT_OPERATOR_HINTS = {
    "GBRF": "GB Railfreight", "FLHH": "Freightliner Heavy Haul", "FLT": "Freightliner",
    "DBC": "DB Cargo", "EWS": "DB Cargo", "DRS": "Direct Rail Services", "COLAS": "Colas Rail",
}

MIN_OBSERVATIONS = 2  # allocations needed before we call a class "typical" for a service group
# Recency weighting: an allocation counts half as much every HALF_LIFE_DAYS, so
# after a fleet change the new class takes over "likely" within about two weeks.
HALF_LIFE_DAYS = 14


class ServicePatterns:
    """Learns which class usually works each (LINX operator code, headcode prefix)."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.observations: dict[tuple[str, str], list[tuple[str, datetime]]] = defaultdict(list)
        self.cars: dict[str, Counter] = defaultdict(Counter)  # fleet_id -> Counter(cars per unit)

    def observe(self, allocation: dict) -> None:
        """Learn from one LINX allocation (as produced by main.parse_linx_allocation / allocation_from_row)."""
        operator = allocation.get("operator_code")
        headcode = allocation.get("headcode") or ""
        fleet_id = allocation["formation"].get("fleet_id")
        if not operator or len(headcode) < 2 or not fleet_id:
            return
        # Cars per unit from the per-unit list (a two-unit train's vehicles cover both units)
        units = allocation.get("units") or []
        count = units[0].get("vehicle_count") if units else len(allocation.get("vehicles") or [])
        try:
            received = datetime.fromisoformat(allocation.get("timestamp"))
        except (TypeError, ValueError):
            received = datetime.now()
        with self.lock:
            self.observations[(operator, headcode[:2])].append((fleet_id, received))
            if count:
                self.cars[fleet_id][count] += 1

    def typical(self, toc: str | None, headcode: str | None) -> tuple[str, float, int, int | None, str] | None:
        """(fleet_id, share, observations, cars per unit, basis) or None. Tries the
        exact prefix (e.g. '1Y'), then the route letter across train classes
        ('?Y': 1Y, 2Y, 5Y ... usually share a route and stock)."""
        operator = LINX_CODE_FOR_TOC.get(toc or "")
        if not operator or not headcode or len(headcode) < 2:
            return None
        now = datetime.now()
        with self.lock:
            by_letter = [obs for (op, prefix), seen in self.observations.items()
                         if op == operator and prefix[1:] == headcode[1] for obs in seen]
            candidates = [(self.observations.get((operator, headcode[:2]), []), f"{headcode[:2]}xx"),
                          (by_letter, f"?{headcode[1]}xx")]
            for seen, basis in candidates:
                if not seen:
                    continue
                weights, counts = Counter(), Counter()
                for fleet_id, received in seen:
                    age_days = max((now - received).total_seconds() / 86400, 0)
                    weights[fleet_id] += 0.5 ** (age_days / HALF_LIFE_DAYS)
                    counts[fleet_id] += 1
                fleet_id, weight = weights.most_common(1)[0]
                if counts[fleet_id] >= MIN_OBSERVATIONS:
                    cars = self.cars[fleet_id].most_common(1)[0][0] if self.cars[fleet_id] else None
                    return fleet_id, round(weight / sum(weights.values()), 2), len(seen), cars, basis
        return None


patterns = ServicePatterns()


def _class_label(fleet_id: str | None) -> str:
    nickname = CLASS_NICKNAMES.get((fleet_id or "").split("/")[0])
    return f"Class {fleet_id}" + (f" {nickname}" if nickname else "")


def _freight_operator(train: dict) -> str | None:
    for place in (train.get("origin"), train.get("destination")):
        for token in reversed(((place or {}).get("name") or "").upper().split()):
            if token in FREIGHT_OPERATOR_HINTS:
                return FREIGHT_OPERATOR_HINTS[token]
    return None


def _appearance(confidence: str, source: str, summary: str, cars: int | None, **fields) -> dict:
    return {"confidence": confidence, "source": source, "summary": summary, "cars": cars,
            "train_class": None, "units": [], "livery": None, "photo": None, "class_info": None} | fields


def _units_summary(cars: int | None, fleet_id: str | None, unit_numbers: list[str]) -> str:
    plural = "s" if len(unit_numbers) > 1 else ""
    return f"{cars or '?'}-car {_class_label(fleet_id)}, unit{plural} {' + '.join(unit_numbers)}"


def describe(train: dict, allocation: dict | None = None) -> dict | None:
    """Appearance for a window record. May do (cached) network lookups."""
    cars = int(train["length"]) if str(train.get("length") or "").isdigit() else None
    allocation = allocation or {}
    seats = (allocation.get("summary") or {}).get("total_seats")

    # 1. Exact units from LINX, known to SpotLog (per-unit livery and photos)
    units = (allocation.get("rolling_stock") or {}).get("units") or []
    if units:
        fleet_id = units[0].get("fleet_id")
        liveries = {u["livery"]["description"] for u in units if u["livery"].get("description")}
        cars = cars or (allocation.get("summary") or {}).get("vehicle_count")
        summary = _units_summary(cars, fleet_id, [u["unit_number"] for u in units])
        return _appearance(
            "exact", "linx_allocation", summary + (f" - {next(iter(liveries))}" if len(liveries) == 1 else ""), cars,
            train_class=fleet_id, units=units, livery=units[0]["livery"], seats=seats,
            photo=next((u["photos"][0] for u in units if u.get("photos")), None),
            class_info=allocation["rolling_stock"].get("class_info"),
        )

    # 1b. LINX named the units but SpotLog doesn't know them: still exact, with class-level livery
    linx_units = [u for u in allocation.get("units") or [] if u.get("unit_number")]
    if linx_units:
        linx_units = [u for u in linx_units if u.get("leg_start") == linx_units[0].get("leg_start")]
        fleet_id = linx_units[0].get("fleet_id")
        described = rolling_stock.describe_class(fleet_id, train.get("operator")) if fleet_id else {}
        cars = cars or sum(u.get("vehicle_count") or 0 for u in linx_units) or None
        livery = described.get("livery")
        summary = _units_summary(cars, fleet_id, [u["unit_number"] for u in linx_units])
        return _appearance(
            "exact", "linx_allocation",
            summary + (f" - usually {livery['description']}" if livery and livery.get("description") else ""), cars,
            train_class=fleet_id, units=linx_units, livery=livery, seats=seats,
            photo=(described.get("photos") or [None])[0], class_info=described.get("class_info"),
        )

    # 2. Typical class for this service group
    typical = patterns.typical(train.get("toc"), train.get("headcode"))
    if typical and (train.get("is_passenger") is not False or train.get("train_kind") == "empty_stock"):
        fleet_id, share, observations, cars_per_unit, basis = typical
        described = rolling_stock.describe_class(fleet_id, train.get("operator"))
        formation = f"{cars}-car " if cars else ""
        if cars and cars_per_unit:
            if cars % cars_per_unit:
                # The length doesn't fit the learned sub-class's units (e.g. 10 cars vs 4-car 458/4s),
                # so it's likely a sibling sub-class (458/5): only claim the class
                fleet_id = fleet_id.split("/")[0]
            elif cars > cars_per_unit:
                formation = f"{cars}-car ({cars // cars_per_unit} x {cars_per_unit}-car) "
        livery_description = described["livery"]["description"]
        return _appearance(
            "likely", "typical_for_service",
            f"Probably a {formation}{_class_label(fleet_id)}"
            + (f" - {livery_description}" if livery_description else ""),
            cars, train_class=fleet_id, livery=described["livery"], class_info=described["class_info"],
            photo=(described["photos"] or [None])[0],
            basis=f"{int(share * 100)}% (recency-weighted) of {observations} LINX allocations for {basis} services",
        )

    # 3. Loco-hauled / non-passenger trains from the Network Rail timetable
    power = POWER_TYPES.get(train.get("power_type") or "")
    if power and train.get("train_kind") not in ("passenger", None):
        hauled = "locomotive" in power
        parts = [f"{power.capitalize()}-hauled" if hauled else power.capitalize()]
        load = str(train.get("timing_load") or "")
        if hauled and load.isdigit():
            parts.append(f"trailing load up to {load} t")
        if train.get("max_speed_mph"):
            parts.append(f"max {train['max_speed_mph']} mph")
        operator = _freight_operator(train)
        if operator:
            parts.append(f"likely {operator}")
        return _appearance("timetable", "network_rail_timetable",
                           f"{train.get('category_description') or 'Train'}: " + ", ".join(parts), cars,
                           likely_operator=operator)

    return _appearance("unknown", "darwin", f"{cars} coaches", cars) if cars else None
