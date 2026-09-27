# What Train Is Coming Past My Window

A live display of the trains passing a window that overlooks a railway station - including ones that don't stop,
freight and engineering trains - showing when each will go past, which way, and what it looks like (units, livery,
coaches, photo).

It's set up for Wandsworth Town but works for any station in Great Britain: set one station code and the backend
works out the railway around it from Network Rail data.

```
 Rail Data Marketplace                     backend/ (Python)                      frontend/ (React)
 ─────────────────────                     ─────────────────                      ─────────────────
 Darwin Push Port  ─┐                  ┌─ window_tracker.py  when/which way ─┐
 LINX allocations  ─┼─ Kafka ─ main.py ┤  appearance.py      what it looks like├─ WebSocket ─ card for the train
 NWR Train Describer┘                  │  rolling_stock.py   SpotLog/Commons/  │  :8767        passing now or in
 NWR Schedule/SMART/CORPUS ─ files ────┘  location.py        Wikipedia        ─┘               the next minute
```

## Setup

You need **Python 3.10+**, **Node.js 20.19+ or 22.12+**, and a free
[Rail Data Marketplace](https://raildata.org.uk/) (RDM) account.

### 1. Subscribe to the data products

All are free. Kafka credentials are on each product's **Pub/Sub** tab once subscribed; files are on its
**Data files** tab.

| Product | Needed? | Provides | How |
|---|---|---|---|
| Darwin Push Port | Required | Passenger train times, forecasts, platforms | Kafka |
| LINX Passenger Train Allocation and Consist | Required | Which units form each train | Kafka |
| [NWR Schedule](https://raildata.org.uk/dataProduct/P-dbd92416-2f09-4f72-ad42-d53bbfec50f3/overview) | Required | Timetable for every operator, incl. freight | File: `CIF_ALL_FULL_DAILY_toc-full.json.gz` |
| [NWR CORPUS](https://raildata.org.uk/dataProduct/P-9d26e657-26be-496b-b669-93b217d45859/overview) | Required | Station codes and names | File: `CORPUSExtract.json.gz` |
| [NWR SMART](https://raildata.org.uk/dataProduct/P-883f25d9-9483-4d26-9975-81203795243f/overview) | Recommended | Signal berths → stations, up/down, platforms | File: `SMARTExtract.json.gz` |
| [NWR Train Describer (TD)](https://raildata.org.uk/dataProduct/P-8d5f90a7-2fce-4179-b233-0dd272cee896/overview) | Recommended | Where every train actually is, signal by signal | Kafka (topic `TD_ALL_SIG_AREA`) |

### 2. Download the files

Put the three files in `backend/data/`. They can stay gzipped or be unzipped (browsers often unzip them) - both work.
Re-download the schedule every few days (it's how one-off diversions and cancellations are picked up) and
SMART/CORPUS monthly.

### 3. Configure the backend

```
cd backend
copy .env.example .env        (cp on macOS/Linux)
```

Fill in the Kafka credentials, set `WINDOW_STATION` to your station's CRS code (e.g. `WNT`, `SUR`), and put your
email in `ROLLING_STOCK_USER_AGENT` (Wikimedia asks API users to be contactable). The other settings are
explained in the file and under [Configuration](#configuration).

### 4. Install and run the backend

Windows: `install_dependencies.bat` (creates `.venv`), then `run_backend.bat`.
Elsewhere:

```
python -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python main.py
```

The first start takes a few seconds longer while it works out the railway around your station; watch for
`Window at <station> ...` and `WebSocket server on ws://localhost:8767` in the log.

### 5. Install and run the frontend

```
cd frontend
npm install
npm run dev
```

Open http://localhost:5173. Click the gear icon and choose which way trains heading in the first direction (e.g.
"towards Clapham Junction") go as seen from your window, so they cross the screen the same way as outside. The same
panel (or `?ws=ws://host:port` in the URL) points it at a backend elsewhere. `npm run build` type-checks and writes a
static build to `frontend/dist/`.

To check what the backend derives for a station without starting it: `python location.py SUR`.

## Configuration

All in `backend/.env` (template: `backend/.env.example`).

| Setting | Purpose |
|---|---|
| `KAFKA_BOOTSTRAP_SERVERS` | RDM Kafka server |
| `DARWIN_CONSUMER_GROUP_ID` / `_USERNAME` / `_PASSWORD` | Darwin Push Port subscription |
| `NWR_CONSUMER_GROUP_ID` / `_USERNAME` / `_PASSWORD` | LINX allocation subscription |
| `NWR_TD_TOPIC`, `NWR_TD_CONSUMER_GROUP_ID` / `_USERNAME` / `_PASSWORD`, `NWR_TD_BOOTSTRAP_SERVERS` | Train Describer (optional; skipped if blank) |
| `NWR_SCHEDULE_FILE`, `NWR_CORPUS_FILE`, `NWR_SMART_FILE` | Downloaded files, relative to `backend/` |
| `WINDOW_STATION` | CRS code or TIPLOC of the station the window overlooks (default `WNT`) |
| `WINDOW_OVERRIDES` | Per-station JSON overrides of anything derived (see below) |
| `ROLLING_STOCK_USER_AGENT` | User agent with contact details for Wikimedia/SpotLog requests |

## How it works

### The location (`location.py`)

At startup the backend derives, from CORPUS, the schedule and SMART:

* the station's TIPLOCs (all sharing its STANOX) and name, and the nearby timing points on each of its two sides;
* which pairs of timing points have the station directly between them - a train timed at one then the other must
  pass the window, even when the timetable doesn't time it at the station (common for freight and fast trains);
* typical running times from the station to each nearby timing point, used to place the window between two points;
* a name for each direction (the busiest station close by that way, e.g. "towards Putney"), which side is "up"
  (side `a` when known, usually towards London), and which platforms serve which direction;
* Train Describer approach points: neighbouring stations' signal steps leading straight into the station's.

The result is cached in `backend/cache/location_<code>.json` until the data files change. Anything can be overridden
per station with `WINDOW_OVERRIDES`, keyed by station code so switching station doesn't carry another's tweaks:

```
WINDOW_OVERRIDES={"WNT": {"approach_points": [{"tiploc": "PUTNEY", "event": "B"}]}}
```

That example adds Putney's up departures as an early warning; they pass Wandsworth Town but are in a different
signalling area, so they can't be linked automatically. Other keys: `name`, `sides` (e.g.
`{"a": {"towards": "London"}}`), `run_minutes`, `platform_side`, `up_side`, `through_pairs`.

### When and which way (`window_tracker.py`, `nwr_schedule.py`, `nwr_td.py`)

Each train's itinerary around the station is merged from three sources. Darwin covers passenger operators (including
their empty stock moves): timetables with passing points, and live forecasts/actuals. The Network Rail schedule adds
every other train (freight, engineering and test trains, light engines) and the headcodes of passenger trains Darwin
only sends forecasts for; it's re-read shortly after midnight. The Train Describer shows where any train actually is.

The window time, and how reliable it is (`window_time_source`), is the first available of:

| Source | Meaning |
|---|---|
| `signalled` | The Train Describer saw the train at the station |
| `signalled_estimate` | Seen at a neighbouring station heading for it |
| `actual` / `estimated` / `scheduled` | Darwin or timetable time at the station |
| `interpolated` | Not timed at the station: placed between the timing points either side |

Trains seen only by the Train Describer are added with a type inferred from the headcode (e.g. `6xxx` freight,
`1Qxx` test train). Direction comes from the timing points either side, then the platform, then signalling.

### What it looks like (`appearance.py`, `rolling_stock.py`)

Every train has an `appearance` with a `confidence`:

* **`exact`** - LINX allocated its units for today's run (matched by headcode, route and run date). Each LINX
  message lists every unit in the train in order; units are looked up on SpotLog for their livery and on Wikimedia
  Commons for photos of that exact unit.
* **`likely`** - no allocation, but LINX history shows which class usually works that service group (headcode
  prefix, e.g. `2U` Windsor = Class 458/4). History is recency-weighted (14-day half-life, `HALF_LIFE_DAYS`) so a
  fleet change takes over within about two weeks. The livery is the one most of that class carries, with a photo of
  the class in it.
* **`timetable`** - freight/engineering trains: traction, trailing load, top speed and a likely operator from yard
  names (there's no feed of locomotive numbers).

Enrichment sources, cached in `backend/cache/` (SpotLog 7 days, Commons/Wikipedia 14 days):

1. **SpotLog** `getLocos?set=UK&clss=<class>` - each unit's carriage numbers (so a LINX carriage number resolves to
   its unit) and livery code; descriptions from `codes/getcodes?country=UK&cat=Livery`. Sub-classes such as `700/1`
   are listed separately, so both are loaded.
2. **Wikimedia Commons** - photos from `Category:British Rail Class <class> <unit>`, preferring ones whose livery
   category matches, then newest; else the class in the same livery. Author and licence are kept for attribution.
3. **Wikipedia** - class summary and thumbnail.

To try it on a unit: `python rolling_stock.py 387/1 421142 422142 423142 424142`.

### WebSocket messages (`ws://localhost:8767`)

* `welcome` (on connect) - includes `location`: the station name and its two directions.
* `window_board` (on connect and every 30 s) - trains due in the next 90 minutes, soonest first.
* `train_update` - whenever a train's time, platform or cancellation changes.

A train record (abridged):

```json
{
  "id": "202609267684359", "headcode": "2S55", "operator": "South Western Railway",
  "category_description": "Ordinary passenger", "train_kind": "passenger", "is_passenger": true,
  "origin": {"tiploc": "WATRLMN", "name": "Waterloo"}, "destination": {"tiploc": "WEYBDGB", "name": "Weybridge"},
  "stops_at_station": true, "window_time": "2026-09-26T18:05:00", "window_time_source": "estimated",
  "arrival": "2026-09-26T18:05:00", "departure": "2026-09-26T18:05:00",
  "direction": "towards Putney", "direction_side": "b", "platform": "2", "length": "10", "cancelled": false,
  "appearance": {"confidence": "likely", "summary": "Probably a 10-car Class 701/0 Arterio - ...", "photo": {"...": "..."}},
  "allocation": {"match_method": "headcode", "units": ["..."], "rolling_stock": {"...": "..."}}
}
```

### The frontend (`frontend/src`)

One card per train approaching (within 60 s) or at the window - up to two for trains crossing - with a countdown,
then "Passing now" / "Arriving" / "At the station now" / "Departing", a strip showing it crossing your view, what it
looks like with a photo and its attribution, and how reliable the time is. Timetable-based times get a wider "due"
period than signalling sightings (`MARGINS` in `lib/timing.ts`). With nothing due, it shows a countdown to the next
train, and a "coming up" list below.

```
types.ts               message shapes from the backend
lib/timing.ts          stages (approaching/passing/arriving/dwelling/departing) and track position
hooks/useTrainFeed.ts  WebSocket connection with reconnect; board + updates + location
hooks/useSettings.ts   left/right orientation and backend address (localStorage)
components/            Header, SettingsPanel, TrainCard, Track, TrainPhoto, IdlePanel, UpcomingList
```

### Files the backend writes

* `train_allocations.csv` - LINX allocation history, used to learn typical classes. Rows older than 60 days
  (`ALLOCATION_RETENTION_DAYS`) are pruned at startup.
* `darwin_trains.csv` - a write-only log of trains past the window for your own analysis; delete it whenever.
* `cache/` - SpotLog/Commons/Wikipedia responses and the derived location; safe to delete.

## Future work

### Deploying beyond localhost

Today everything runs on one machine: the backend binds `localhost:8767` without TLS or authentication, the
frontend defaults to `ws://localhost:8767`, the reference files are downloaded by hand, and state lives in local
CSV files. Some simple ways to host it:

* **One small VM (simplest).** A £4-6/month VM (Hetzner, DigitalOcean, Lightsail, or a free-tier e2-micro) running
  Docker Compose with two services: the backend, and [Caddy](https://caddyserver.com/) serving the frontend's static
  build and reverse-proxying `wss://your-domain/ws` to the backend with automatic HTTPS. A named volume holds
  `data/`, `cache/` and the CSVs; secrets come from an env file on the host. The backend needs `WEBSOCKET_HOST`
  (currently a constant in `main.py`) to become configurable so it can listen inside the container.
* **Split static frontend and managed backend.** Host `frontend/dist` on Cloudflare Pages, Netlify, GitHub Pages or
  S3 + CloudFront, and run the backend container on an always-on service such as Fly.io, Railway or Render. The
  backend must stay running (it holds Kafka consumers and in-memory state), so scale-to-zero platforms need a
  minimum of one instance, and the service has to support long-lived WebSockets. The frontend's backend address
  would move to a build-time setting (e.g. `VITE_WS_URL`).
* **Automated data files.** RDM's File transfers feature can push the daily schedule and monthly SMART/CORPUS files
  to an S3 or GCS bucket; the backend would fetch the latest from the bucket at startup and after midnight, removing
  the manual download.
* **State.** Replace the CSV files with SQLite on the volume (or a small managed Postgres), and persist the tracker's
  in-memory trains so a restart doesn't lose the day's timetable matches.
* **Secrets and access.** Use the platform's secret store instead of `.env`. Put the page behind Cloudflare Access or
  basic auth if it shouldn't be public, and check the RDM products' licence terms before redistributing their data.
* **At the window.** A Raspberry Pi or old tablet in kiosk mode (Chromium `--kiosk`) pointed at the hosted page.

### Other ideas

* A window between two stations (a position between two timing points) rather than at one.
* More than two directions at junction stations such as Clapham Junction or Reading.
* Read the schedule's daily update files instead of the full extract, and add a health-check endpoint.
* Tests for the parsers and timing logic (with recorded real messages), linting in CI, and a `Dockerfile`.
* Locomotive identification for freight, if a source of loco-to-headcode data becomes available.

## Data and attribution

* Network Rail data (Schedule, CORPUS, SMART, Train Describer) is used under the
  [Open Government Licence v3.0](https://www.nationalarchives.gov.uk/doc/open-government-licence/version/3/);
  Darwin and LINX are subject to their Rail Data Marketplace terms.
* Photos are from Wikimedia Commons under their individual licences; the frontend shows each photo's author and
  licence with a link to its page.
* Livery data comes from [SpotLog](https://spotlog.org/locolist) via its public endpoints.
