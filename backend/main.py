"""
What Train Is Coming Past My Window - backend.

Consumes the Rail Data Marketplace Kafka feeds (Darwin, LINX allocations and,
optionally, the Network Rail Train Describer), works out which trains pass the
window (at the station set by WINDOW_STATION, see location.py) and what they look
like, and serves them to the frontend over a WebSocket.
"""
import asyncio
import csv
import json
import logging
import os
import threading
import time
import xml.etree.ElementTree as ET
from collections.abc import Callable, Iterable
from datetime import datetime, timedelta
from pathlib import Path

import websockets
from dotenv import load_dotenv
from kafka import KafkaConsumer

import appearance
import location
import nwr_schedule
import rolling_stock
from nwr_td import describer as train_describer
from nwr_td import td_configured
from operators import LINX_OPERATOR_NAMES
from window_tracker import tracker as window_tracker

load_dotenv()

log = logging.getLogger("backend")

# ---------------------------------------------------------------------------- configuration

KAFKA_BOOTSTRAP_SERVERS = os.getenv("KAFKA_BOOTSTRAP_SERVERS")
DARWIN_TOPIC = "prod-1010-Darwin-Train-Information-Push-Port-IIII2_0-AVRO"
LINX_TOPIC = "prod-1033-Passenger-Train-Allocation-and-Consist-1_0"

WEBSOCKET_HOST = "localhost"
WEBSOCKET_PORT = 8767
# How far ahead the "window_board" of upcoming trains looks, and how often it's re-sent
BOARD_MINUTES_AHEAD = 90
BOARD_INTERVAL_SECONDS = 30
# How often the background thread fills in allocation/appearance for trains on the board
ENRICH_INTERVAL_SECONDS = 20
TD_STATS_INTERVAL_SECONDS = 300

BACKEND_DIR = Path(__file__).resolve().parent
ALLOCATION_CSV = BACKEND_DIR / "train_allocations.csv"
ALLOCATION_CSV_COLUMNS = ["headcode", "train_id", "operator_code", "operator_name",
                          "origin", "destination", "vehicle_count", "fleet_id",
                          "vehicles_json", "timestamp", "train_origin_datetime", "units_json"]
# train_allocations.csv keeps this much history (for learning which class works
# each service); older rows are pruned from the file at startup
ALLOCATION_RETENTION_DAYS = 60
# Without a TrainOriginDateTime, an allocation received longer ago than this is
# assumed to be for an earlier run of the headcode (units change day to day)
ALLOCATION_MAX_AGE_HOURS = 18

# Write-only log of Darwin trains past the window, for your own analysis
DARWIN_LOG_CSV = BACKEND_DIR / "darwin_trains.csv"
DARWIN_LOG_COLUMNS = ["logged_at", "rid", "uid", "headcode", "window_time", "window_time_source",
                      "stops", "platform", "length", "origin", "destination", "allocation_match"]

LINX_NS = {"taftsi": "http://www.era.europa.eu/schemes/TAFTSI/5.3"}

# ---------------------------------------------------------------------------- state

# Latest LINX allocation per headcode (normalised dicts, see allocation_from_row)
allocations_by_headcode: dict[str, dict] = {}
# Allocation (with rolling stock enrichment) and appearance per train id, so the
# board can be rebuilt on the WebSocket loop without any network lookups there
allocation_by_id: dict[str, dict | None] = {}
appearance_by_id: dict[str, dict | None] = {}

connected_clients: set = set()
# Event loop the WebSocket server runs on (Kafka threads schedule sends onto it)
websocket_loop: asyncio.AbstractEventLoop | None = None

# ---------------------------------------------------------------------------- LINX allocations


def _json_list(value) -> list:
    if isinstance(value, list):
        return value
    try:
        return json.loads(value) if value else []
    except json.JSONDecodeError:
        return []


def allocation_from_row(row: dict) -> dict:
    """A train_allocations.csv row in the same shape as a freshly parsed allocation."""
    return {
        "headcode": row.get("headcode") or None,
        "train_id": row.get("train_id") or None,
        "operator_code": row.get("operator_code") or None,
        "operator_name": row.get("operator_name") or None,
        "origin": row.get("origin") or None,
        "destination": row.get("destination") or None,
        "timestamp": row.get("timestamp") or None,
        "train_origin_datetime": row.get("train_origin_datetime") or None,
        "formation": {"fleet_id": row.get("fleet_id") or None, "vehicle_count": row.get("vehicle_count") or None},
        "vehicles": _json_list(row.get("vehicles_json")),
        "units": _json_list(row.get("units_json")),
    }


def remember_allocation(allocation: dict) -> None:
    if allocation.get("headcode"):
        allocations_by_headcode[allocation["headcode"]] = allocation
    appearance.patterns.observe(allocation)


def load_allocations() -> None:
    """Load train_allocations.csv into memory, first pruning rows older than
    ALLOCATION_RETENTION_DAYS (and upgrading files written with older columns)."""
    if not ALLOCATION_CSV.exists():
        log.info("No %s yet; starting with no allocation history", ALLOCATION_CSV.name)
        return
    try:
        with open(ALLOCATION_CSV, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            fieldnames = reader.fieldnames
            rows = list(reader)
    except (OSError, csv.Error) as e:
        log.error("Couldn't read %s: %s", ALLOCATION_CSV.name, e)
        return

    cutoff = datetime.now() - timedelta(days=ALLOCATION_RETENTION_DAYS)

    def is_recent(row: dict) -> bool:
        try:
            return datetime.fromisoformat(row.get("timestamp")) >= cutoff
        except (TypeError, ValueError):
            return False

    kept = [row for row in rows if is_recent(row)]
    if len(kept) < len(rows) or fieldnames != ALLOCATION_CSV_COLUMNS:
        # Rewrite via a temporary file so a crash mid-write can't lose the history
        tmp = ALLOCATION_CSV.with_suffix(".csv.tmp")
        with open(tmp, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=ALLOCATION_CSV_COLUMNS, restval="", extrasaction="ignore")
            writer.writeheader()
            writer.writerows(kept)
        tmp.replace(ALLOCATION_CSV)
        if len(kept) < len(rows):
            log.info("Pruned %d allocations older than %d days from %s",
                     len(rows) - len(kept), ALLOCATION_RETENTION_DAYS, ALLOCATION_CSV.name)

    for row in kept:
        remember_allocation(allocation_from_row(row))
    log.info("Loaded %d allocations (%d headcodes) from %s",
             len(kept), len(allocations_by_headcode), ALLOCATION_CSV.name)


def save_allocation(allocation: dict) -> None:
    new_file = not ALLOCATION_CSV.exists()
    try:
        with open(ALLOCATION_CSV, "a", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=ALLOCATION_CSV_COLUMNS)
            if new_file:
                writer.writeheader()
            writer.writerow({
                "headcode": allocation["headcode"] or "",
                "train_id": allocation["train_id"] or "",
                "operator_code": allocation["operator_code"] or "",
                "operator_name": allocation["operator_name"] or "",
                "origin": allocation["origin"] or "",
                "destination": allocation["destination"] or "",
                "vehicle_count": allocation["formation"]["vehicle_count"] or "",
                "fleet_id": allocation["formation"]["fleet_id"] or "",
                "vehicles_json": json.dumps(allocation["vehicles"]),
                "timestamp": allocation["timestamp"],
                "train_origin_datetime": allocation["train_origin_datetime"] or "",
                "units_json": json.dumps(allocation["units"]),
            })
    except OSError as e:
        log.error("Couldn't save allocation to %s: %s", ALLOCATION_CSV.name, e)


def _text(element: ET.Element, path: str) -> str | None:
    found = element.find(path, LINX_NS)
    return found.text.strip() if found is not None and found.text else None


def _number(element: ET.Element, path: str) -> int | None:
    value = _text(element, path)
    try:
        return int(value) if value is not None else None
    except ValueError:
        return None


def parse_linx_allocation(xml: str) -> dict:
    """
    Parse a LINX Passenger Train Allocation and Consist message. A message has one
    <Allocation> per unit (Resource Group) per journey leg: ResourceGroupPosition
    orders the units from the front of the train and ResourceGroupId is the unit
    number (e.g. 450085). Raises ET.ParseError for malformed XML.
    """
    root = ET.fromstring(xml)
    operator_code = _text(root, ".//taftsi:ResponsibleRU")
    allocation = {
        "headcode": _text(root, ".//taftsi:OperationalTrainNumber"),
        "train_id": _text(root, ".//taftsi:TrainOperationalIdentification//taftsi:Core"),
        "operator_code": operator_code,
        "operator_name": LINX_OPERATOR_NAMES.get(operator_code or "", f"Unknown ({operator_code})"),
        "origin": None,
        "destination": None,
        "timestamp": datetime.now().isoformat(),
        "train_origin_datetime": None,
    }

    units = []
    for leg in root.findall(".//taftsi:Allocation", LINX_NS):
        if allocation["origin"] is None:
            allocation["origin"] = _text(leg, "taftsi:TrainOriginLocation//taftsi:LocationSubsidiaryCode")
            allocation["destination"] = _text(leg, "taftsi:TrainDestLocation//taftsi:LocationSubsidiaryCode")
            allocation["train_origin_datetime"] = _text(leg, "taftsi:TrainOriginDateTime")
        for group in leg.findall("taftsi:ResourceGroup", LINX_NS):
            fleet_id = _text(group, "taftsi:FleetId")
            unit_number = _text(group, "taftsi:ResourceGroupId")
            vehicles = []
            for vehicle in group.findall("taftsi:Vehicle", LINX_NS):
                data = {
                    "vehicle_id": _text(vehicle, "taftsi:VehicleId"),
                    "position": _number(vehicle, "taftsi:ResourcePosition"),
                    "unit_number": unit_number,
                    "type": _text(vehicle, "taftsi:SpecificType"),
                    "train_class": f"Class {fleet_id}" if fleet_id else None,
                    "length_mm": _number(vehicle, "taftsi:Length/taftsi:Value"),
                    "livery_code": _text(vehicle, "taftsi:Livery"),
                    "seats": _number(vehicle, "taftsi:NumberOfSeats"),
                    "max_speed_mph": _number(vehicle, "taftsi:MaximumSpeed"),
                    "date_entered_service": _text(vehicle, "taftsi:DateEnteredService"),
                }
                vehicles.append({k: v for k, v in data.items() if v is not None})
            vehicles.sort(key=lambda v: v.get("position") or 0)
            units.append({
                "unit_number": unit_number,
                "type_of_resource": _text(group, "taftsi:TypeOfResource"),  # U unit, L loco, S set
                "fleet_id": fleet_id,
                "position": _number(leg, "taftsi:ResourceGroupPosition"),
                "reversed": _text(leg, "taftsi:Reversed") == "Y",
                "diagram": _text(leg, "taftsi:DiagramNo"),
                "leg_origin": _text(leg, "taftsi:AllocationOriginLocation//taftsi:LocationSubsidiaryCode"),
                "leg_destination": _text(leg, "taftsi:AllocationDestinationLocation//taftsi:LocationSubsidiaryCode"),
                "leg_start": _text(leg, "taftsi:AllocationOriginDateTime"),
                "leg_end": _text(leg, "taftsi:AllocationDestinationDateTime"),
                "vehicles": vehicles,
            })

    units.sort(key=lambda u: (u["leg_start"] or "", u["position"] or 0))
    # Vehicles front of train first, numbered through the whole train. Only the
    # first leg's units, so a unit swap en route isn't counted twice.
    first_leg = units[0]["leg_start"] if units else None
    first_leg_units = sorted((u for u in units if u["leg_start"] == first_leg), key=lambda u: u["position"] or 0)
    allocation["vehicles"] = [dict(vehicle, position=position) for position, vehicle in
                              enumerate((v for u in first_leg_units for v in u["vehicles"]), start=1)]
    allocation["units"] = [{k: v for k, v in u.items() if k != "vehicles"} | {"vehicle_count": len(u["vehicles"])}
                           for u in units]
    allocation["formation"] = {
        "fleet_id": units[0]["fleet_id"] if units else None,
        "vehicle_count": len(allocation["vehicles"]) or None,
        "unit_numbers": [u["unit_number"] for u in first_leg_units],
    }
    return allocation


def handle_linx_message(value: bytes) -> None:
    xml = value.decode("utf-8")
    try:
        allocation = parse_linx_allocation(xml)
    except ET.ParseError as e:
        log.warning("Malformed LINX message (%s): %.300s", e, xml)
        return
    remember_allocation(allocation)
    save_allocation(allocation)
    units = allocation["formation"]["unit_numbers"]
    log.info("LINX %s (%s): %s", allocation["headcode"], allocation["operator_name"],
             " + ".join(n or "?" for n in units) if units else "no units")


def allocation_is_for_run(allocation: dict, run_date: str | None) -> bool:
    """True if a LINX allocation is for this run of the train: same TrainOriginDateTime
    date when both are known, otherwise received within ALLOCATION_MAX_AGE_HOURS."""
    origin_datetime = allocation.get("train_origin_datetime")
    if origin_datetime and run_date:
        return origin_datetime[:10] == run_date
    try:
        received = datetime.fromisoformat(allocation.get("timestamp"))
    except (TypeError, ValueError):
        return False
    return datetime.now() - received < timedelta(hours=ALLOCATION_MAX_AGE_HOURS)


def allocation_matches_route(allocation: dict, origin: str | None, destination: str | None) -> bool:
    """Headcodes aren't unique nationally, so check the LINX origin/destination
    agree with the train's when both are known."""
    if not (origin or destination) or not (allocation.get("origin") or allocation.get("destination")):
        return True
    return allocation.get("origin") == origin or allocation.get("destination") == destination


def lookup_allocation(train: dict) -> dict | None:
    """The LINX allocation for a window record (same headcode, route and run date),
    with its rolling stock enriched from SpotLog / Wikimedia Commons / Wikipedia."""
    headcode = train.get("headcode")
    allocation = allocations_by_headcode.get(headcode) if headcode else None
    if not allocation:
        return None
    origin = (train.get("origin") or {}).get("tiploc")
    destination = (train.get("destination") or {}).get("tiploc")
    run_date = (train.get("window_time") or "")[:10] or None
    if not allocation_matches_route(allocation, origin, destination) or not allocation_is_for_run(allocation, run_date):
        return None

    vehicles = allocation["vehicles"]
    fleet_id = allocation["formation"]["fleet_id"]
    result = {
        "match_method": "headcode",
        "headcode": allocation["headcode"],
        "operator_code": allocation["operator_code"],
        "operator_name": allocation["operator_name"],
        "origin": allocation["origin"],
        "destination": allocation["destination"],
        "train_origin_datetime": allocation["train_origin_datetime"],
        "formation": allocation["formation"],
        "vehicles": vehicles,
        "units": allocation["units"],
    }
    if vehicles and fleet_id:
        try:
            result["rolling_stock"] = rolling_stock.enrich_vehicles(vehicles, fleet_id)
        except Exception:
            log.exception("Rolling stock enrichment failed for %s", headcode)
    if vehicles:
        total_seats = sum(v.get("seats", 0) for v in vehicles)
        total_length = round(sum(v.get("length_mm", 0) for v in vehicles) / 1000, 1)
        result["summary"] = {
            "vehicle_count": len(vehicles),
            "total_seats": total_seats,
            "total_length_meters": total_length,
            "formation": f"{len(vehicles)} cars, {total_seats} seats, {total_length}m total length",
        }
    return result

# ---------------------------------------------------------------------------- trains past the window


def refresh_train_details(train: dict) -> None:
    """Look up (or retry) a train's allocation and appearance, caching both, and attach them."""
    train_id = train["id"]
    if allocation_by_id.get(train_id) is None:
        allocation_by_id[train_id] = lookup_allocation(train)
    train["allocation"] = allocation_by_id[train_id]
    previous = appearance_by_id.get(train_id)
    if previous is None or previous.get("confidence") != "exact" or train["allocation"] is None:
        try:
            appearance_by_id[train_id] = appearance.describe(train, train["allocation"])
        except Exception:
            log.exception("Couldn't describe the appearance of %s", train_id)
    train["appearance"] = appearance_by_id.get(train_id)


def prepare_darwin_log() -> None:
    """Set aside a darwin_trains.csv written by an older version with different columns."""
    if not DARWIN_LOG_CSV.exists():
        return
    with open(DARWIN_LOG_CSV, newline="", encoding="utf-8") as f:
        header = next(csv.reader(f), None)
    if header != DARWIN_LOG_COLUMNS:
        legacy = DARWIN_LOG_CSV.with_name("darwin_trains_legacy.csv")
        DARWIN_LOG_CSV.replace(legacy)
        log.info("Moved %s (old column layout) to %s", DARWIN_LOG_CSV.name, legacy.name)


def log_darwin_train(train: dict) -> None:
    """Append a train to darwin_trains.csv (a write-only log for your own analysis)."""
    new_file = not DARWIN_LOG_CSV.exists()
    try:
        with open(DARWIN_LOG_CSV, "a", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=DARWIN_LOG_COLUMNS)
            if new_file:
                writer.writeheader()
            writer.writerow({
                "logged_at": datetime.now().isoformat(timespec="seconds"),
                "rid": train.get("rid") or "",
                "uid": train.get("uid") or "",
                "headcode": train.get("headcode") or "",
                "window_time": train.get("window_time") or "",
                "window_time_source": train.get("window_time_source") or "",
                "stops": {True: "yes", False: "no"}.get(train.get("stops_at_station"), ""),
                "platform": train.get("platform") or "",
                "length": train.get("length") or "",
                "origin": (train.get("origin") or {}).get("tiploc", ""),
                "destination": (train.get("destination") or {}).get("tiploc", ""),
                "allocation_match": (train.get("allocation") or {}).get("match_method", ""),
            })
    except OSError as e:
        log.error("Couldn't write %s: %s", DARWIN_LOG_CSV.name, e)


def publish_train_changes(train_ids: Iterable[str], from_darwin: bool = False) -> None:
    """Attach details to changed trains, log them and broadcast train_update messages."""
    for train_id in train_ids:
        train = window_tracker.record(train_id)
        if not train:
            continue
        refresh_train_details(train)

        action = {True: "passes", False: "stops"}.get(train["passes_without_stopping"], "is")
        log.info("%s (%s) %s at %s %s (%s) %s -> %s%s", train.get("headcode") or train_id,
                 train.get("category_description") or "unknown type", action, location.get().name,
                 train["window_time"], train["window_time_source"],
                 (train.get("origin") or {}).get("name", "?"), (train.get("destination") or {}).get("name", "?"),
                 " CANCELLED" if train["cancelled"] else "")
        if train["appearance"]:
            log.info("  looks like (%s): %s", train["appearance"]["confidence"], train["appearance"]["summary"])

        if from_darwin:
            log_darwin_train(train)
        broadcast({"type": "train_update", "data": train, "timestamp": datetime.now().isoformat()})


def enrich_board_periodically() -> None:
    """Background thread: make sure every train on the board has details, so the
    board shows what each train looks like, not only trains that just changed."""
    while True:
        try:
            for train in window_tracker.board(minutes_ahead=BOARD_MINUTES_AHEAD):
                if train["id"] not in appearance_by_id or allocation_by_id.get(train["id"]) is None:
                    refresh_train_details(train)
        except Exception:
            log.exception("Error enriching the board")
        time.sleep(ENRICH_INTERVAL_SECONDS)


def handle_darwin_message(value: bytes) -> None:
    envelope = json.loads(value)
    if isinstance(envelope, dict) and envelope.get("bytes"):
        publish_train_changes(window_tracker.ingest(json.loads(envelope["bytes"])), from_darwin=True)


def make_td_handler() -> Callable[[bytes], None]:
    last_stats = time.monotonic()

    def handle(value: bytes) -> None:
        nonlocal last_stats
        publish_train_changes(train_describer.handle(value))
        if time.monotonic() - last_stats > TD_STATS_INTERVAL_SECONDS:
            last_stats = time.monotonic()
            log.info("Train Describer stats: %s", train_describer.stats_summary())

    return handle

# ---------------------------------------------------------------------------- WebSocket


def build_board_message() -> dict:
    """Upcoming trains past the window (stopping and non-stopping), soonest first."""
    board = window_tracker.board(minutes_ahead=BOARD_MINUTES_AHEAD)
    for train in board:
        train["allocation"] = allocation_by_id.get(train["id"])
        train["appearance"] = appearance_by_id.get(train["id"])
    return {"type": "window_board", "data": board, "timestamp": datetime.now().isoformat()}


async def send_to_clients(payload: dict) -> None:
    """Send a message to every connected client (runs on the WebSocket loop)."""
    if not connected_clients:
        return
    message = json.dumps(payload)
    results = await asyncio.gather(*(client.send(message) for client in list(connected_clients)),
                                   return_exceptions=True)
    for result in results:
        if isinstance(result, Exception) and not isinstance(result, websockets.exceptions.ConnectionClosed):
            log.warning("Error sending to a client: %s", result)


def broadcast(payload: dict) -> None:
    """Send a message to all clients from any thread, via the WebSocket server's loop."""
    if websocket_loop is not None and connected_clients:
        asyncio.run_coroutine_threadsafe(send_to_clients(payload), websocket_loop)


async def handle_client(websocket, path=None) -> None:  # `path` is only passed by websockets < 13
    connected_clients.add(websocket)
    log.info("Client connected (%d connected)", len(connected_clients))
    try:
        await websocket.send(json.dumps({"type": "welcome", "message": "Connected to train update server",
                                         "location": location.get().to_client()}))
        await websocket.send(json.dumps(build_board_message()))
        await websocket.wait_closed()
    finally:
        connected_clients.discard(websocket)
        log.info("Client disconnected (%d connected)", len(connected_clients))


async def run_websocket_server() -> None:
    global websocket_loop
    websocket_loop = asyncio.get_running_loop()
    async with websockets.serve(handle_client, WEBSOCKET_HOST, WEBSOCKET_PORT):
        log.info("WebSocket server on ws://%s:%d", WEBSOCKET_HOST, WEBSOCKET_PORT)
        while True:
            await asyncio.sleep(BOARD_INTERVAL_SECONDS)
            await send_to_clients(build_board_message())

# ---------------------------------------------------------------------------- Kafka


def create_consumer(topic: str, group_id: str | None, username: str | None, password: str | None,
                    bootstrap_servers: str | None = None) -> KafkaConsumer:
    """A Kafka consumer for an RDM topic (SASL/PLAIN over SSL), reading new messages only."""
    return KafkaConsumer(
        topic,
        bootstrap_servers=bootstrap_servers or KAFKA_BOOTSTRAP_SERVERS,
        group_id=group_id,
        security_protocol="SASL_SSL",
        sasl_mechanism="PLAIN",
        sasl_plain_username=username,
        sasl_plain_password=password,
        auto_offset_reset="latest",
        enable_auto_commit=True,
        session_timeout_ms=30000,
        heartbeat_interval_ms=10000,
    )


def run_consumer(name: str, consumer: KafkaConsumer, handle: Callable[[bytes], None]) -> None:
    """Feed each message's raw bytes to `handle` until the consumer stops."""
    log.info("Listening for %s messages", name)
    try:
        for message in consumer:
            if message.value is None:
                continue
            try:
                handle(message.value)
            except (ValueError, UnicodeDecodeError) as e:  # includes json.JSONDecodeError
                log.warning("Couldn't parse a %s message: %s", name, e)
    except Exception:
        log.exception("%s consumer stopped", name)
    finally:
        consumer.close()


def start_thread(target: Callable, *args) -> None:
    threading.Thread(target=target, args=args, daemon=True, name=getattr(target, "__name__", None)).start()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
                        datefmt="%H:%M:%S")
    logging.getLogger("kafka").setLevel(logging.WARNING)
    log.info("Starting WhatTrainIsComingPastMyWindow backend")

    location.configure()
    load_allocations()
    prepare_darwin_log()
    darwin_consumer = create_consumer(DARWIN_TOPIC, os.getenv("DARWIN_CONSUMER_GROUP_ID"),
                                      os.getenv("DARWIN_CONSUMER_USERNAME"), os.getenv("DARWIN_CONSUMER_PASSWORD"))
    linx_consumer = create_consumer(LINX_TOPIC, os.getenv("NWR_CONSUMER_GROUP_ID"),
                                    os.getenv("NWR_CONSUMER_USERNAME"), os.getenv("NWR_CONSUMER_PASSWORD"))

    start_thread(asyncio.run, run_websocket_server())
    start_thread(run_consumer, "LINX allocation", linx_consumer, handle_linx_message)
    # Network Rail timetable: freight, engineering and test trains; headcodes after restarts
    nwr_schedule.start_daily_loader()
    start_thread(enrich_board_periodically)

    # Network Rail Train Describer (optional; needs the RDM subscription and SMART/CORPUS files)
    if td_configured() and train_describer.load_reference():
        td_consumer = create_consumer(os.getenv("NWR_TD_TOPIC"), os.getenv("NWR_TD_CONSUMER_GROUP_ID"),
                                      os.getenv("NWR_TD_CONSUMER_USERNAME"), os.getenv("NWR_TD_CONSUMER_PASSWORD"),
                                      os.getenv("NWR_TD_BOOTSTRAP_SERVERS"))
        start_thread(run_consumer, "Train Describer", td_consumer, make_td_handler())
    else:
        log.info("Train Describer feed not configured; skipping")

    run_consumer("Darwin", darwin_consumer, handle_darwin_message)


if __name__ == "__main__":
    main()
