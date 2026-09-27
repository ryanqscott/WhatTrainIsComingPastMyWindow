"""
Rolling stock enrichment: turns LINX vehicle (carriage) numbers into units,
liveries, rich descriptions and photos.

Sources
-------
* SpotLog locolist (https://spotlog.org/locolist)
    - getLocos?set=UK&clss=<class>  -> every unit in a class, including its
      livery code and its formation ("form": comma-separated carriage numbers).
      The formation lets us map a LINX VehicleId (e.g. 421142) to its unit
      (387142) and therefore its livery (GWU), even when LINX sends "ZZ".
    - codes/getcodes?country=UK&cat=Livery -> livery code descriptions.
* Wikimedia Commons (https://commons.wikimedia.org)
    - "Category:British Rail Class <class> <unit>" -> photos of that exact unit,
      with licence/attribution metadata and livery categories.
    - "Category:British Rail Class <class>s by livery" -> fallback photo of the
      same class in the same livery when the exact unit has no photos.
* Wikipedia REST summary -> description and thumbnail for the class.

Everything is cached in memory and on disk (./cache) so each class / unit is
only fetched occasionally. Negative results are cached too.
"""
import json
import logging
import os
import re
import threading
import time
import urllib.parse
import urllib.request
from collections import Counter
from datetime import datetime
from pathlib import Path

log = logging.getLogger(__name__)

CACHE_DIR = Path(__file__).resolve().parent / "cache"

SPOTLOG_BASE = "https://spotlog.org/locolist"
COMMONS_API = "https://commons.wikimedia.org/w/api.php"
WIKIPEDIA_SUMMARY = "https://en.wikipedia.org/api/rest_v1/page/summary/"

# Wikimedia asks API clients to identify themselves with contact details
USER_AGENT = os.getenv(
    "ROLLING_STOCK_USER_AGENT",
    "WhatTrainIsComingPastMyWindow/0.1 (personal train tracker; python-urllib)",
)

SPOTLOG_CLASS_TTL = 7 * 24 * 3600
SPOTLOG_CODES_TTL = 30 * 24 * 3600
COMMONS_TTL = 14 * 24 * 3600
HTTP_TIMEOUT = 15
MAX_PHOTOS_PER_UNIT = 3

_STOPWORDS = {
    "the", "and", "with", "of", "in", "a", "livery", "british", "rail", "class",
    "multiple", "units", "unit", "doors", "door", "from", "branding", "s",
}


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _http_get_json(url: str, params: dict | None = None):
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def _strip_html(text: str | None) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", text or "")).strip()


def _tokens(text: str | None) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9]+", (text or "").lower()) if t not in _STOPWORDS and len(t) > 1}


def _base_class(fleet_id: str | None) -> str:
    """'387/1' -> '387'"""
    return (fleet_id or "").split("/")[0].strip()


class _DiskCache:
    """Tiny JSON-file cache with per-entry TTL."""

    def __init__(self, name: str) -> None:
        self.path = CACHE_DIR / f"{name}.json"
        self.lock = threading.Lock()
        self.data: dict = {}
        if self.path.exists():
            try:
                self.data = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as e:
                log.warning("Rolling stock cache %s unreadable, starting fresh: %s", self.path, e)

    def get(self, key: str, ttl: float):
        entry = self.data.get(key)
        if entry and time.time() - entry.get("fetched", 0) < ttl:
            return entry.get("value"), True
        return None, False

    def set(self, key: str, value) -> None:
        with self.lock:
            self.data[key] = {"fetched": time.time(), "value": value}
            try:
                CACHE_DIR.mkdir(exist_ok=True)
                tmp = self.path.with_suffix(".json.tmp")
                tmp.write_text(json.dumps(self.data), encoding="utf-8")
                tmp.replace(self.path)
            except OSError as e:
                log.error("Error writing rolling stock cache %s: %s", self.path, e)


# ---------------------------------------------------------------------------
# SpotLog
# ---------------------------------------------------------------------------

class SpotlogClient:
    def __init__(self):
        self.cache = _DiskCache("spotlog")
        self.lock = threading.Lock()
        self.vehicle_index = {}   # carriage number -> unit record
        self.unit_index = {}      # unit number -> unit record
        self.indexed_classes = set()
        self.livery_codes = None

    def _fetch_class(self, clss):
        """Return the list of unit records for a SpotLog class code (e.g. '387' or '700/1')."""
        key = f"class:{clss}"
        cached, fresh = self.cache.get(key, SPOTLOG_CLASS_TTL)
        if fresh:
            return cached
        try:
            data = _http_get_json(f"{SPOTLOG_BASE}/locolist/getLocos", {"set": "UK", "clss": clss, "page": 0})
            locos = ((data or {}).get("classInfo") or {}).get("Locos") or []
            units = [{
                "number": loco.get("number"),
                "livery": loco.get("livery"),
                "name": loco.get("name"),
                "owner": loco.get("owner"),
                "pool": loco.get("pool"),
                "depot": loco.get("depot"),
                "status": loco.get("status"),
                "form": [v.strip() for v in (loco.get("form") or "").split(",") if v.strip()],
                "spotlog_class": clss,
            } for loco in locos if loco.get("number")]
            self.cache.set(key, units)
            log.info("SpotLog: fetched %d units for class %s", len(units), clss)
            return units
        except Exception as e:  # network error or unexpected response shape
            log.warning("SpotLog: error fetching class %s: %s", clss, e)
            return cached or []  # fall back to stale data if we have it

    def _ensure_class(self, fleet_id):
        """Index units for a LINX fleet id. SpotLog splits some classes into
        sub-classes (e.g. '700/1', '380/1'), so we load both the full fleet id
        and the base class."""
        candidates = []
        if fleet_id:
            candidates.append(fleet_id.strip())
            base = _base_class(fleet_id)
            if base and base not in candidates:
                candidates.append(base)
        with self.lock:
            for clss in candidates:
                if clss in self.indexed_classes:
                    continue
                for unit in self._fetch_class(clss):
                    self.unit_index.setdefault(unit["number"], unit)
                    for vehicle in unit["form"]:
                        self.vehicle_index.setdefault(vehicle, unit)
                self.indexed_classes.add(clss)

    def class_units(self, fleet_id):
        """Units of a fleet, narrowed to the sub-class where the unit numbers
        show it (e.g. '701/5' -> 7015xx, '450/0' -> 4500xx-4501xx)."""
        self._ensure_class(fleet_id)
        base = _base_class(fleet_id)
        units = [u for u in self.unit_index.values() if (u.get("number") or "").startswith(base)]
        sub = fleet_id.split("/")[1].strip() if fleet_id and "/" in fleet_id else ""
        narrowed = [u for u in units if len(u["number"]) == 6 and sub and u["number"][3] == sub]
        return narrowed or units

    def find_unit(self, vehicle_id, fleet_id=None):
        vehicle_id = str(vehicle_id or "").strip()
        if not vehicle_id:
            return None
        if vehicle_id not in self.vehicle_index and vehicle_id not in self.unit_index:
            self._ensure_class(fleet_id)
        return self.vehicle_index.get(vehicle_id) or self.unit_index.get(vehicle_id)

    def livery_description(self, code):
        if not code:
            return None
        if self.livery_codes is None:
            with self.lock:
                if self.livery_codes is None:
                    cached, fresh = self.cache.get("codes:Livery", SPOTLOG_CODES_TTL)
                    if not fresh:
                        try:
                            data = _http_get_json(f"{SPOTLOG_BASE}/codes/getcodes", {"country": "UK", "cat": "Livery"})
                            cached = {c["code1"]: c.get("description") for c in data.get("data", []) if c.get("code1")}
                            self.cache.set("codes:Livery", cached)
                        except Exception as e:  # network error or unexpected response shape
                            log.warning("SpotLog: error fetching livery codes: %s", e)
                    self.livery_codes = cached or {}
        return self.livery_codes.get(code)


# ---------------------------------------------------------------------------
# Wikimedia Commons / Wikipedia
# ---------------------------------------------------------------------------

class CommonsClient:
    def __init__(self):
        self.cache = _DiskCache("commons")

    def _category_files(self, category, limit=50):
        key = f"files:{category}"
        cached, fresh = self.cache.get(key, COMMONS_TTL)
        if fresh:
            return cached
        try:
            data = _http_get_json(COMMONS_API, {
                "action": "query", "format": "json",
                "generator": "categorymembers", "gcmtitle": f"Category:{category}",
                "gcmtype": "file", "gcmlimit": limit,
                "prop": "imageinfo|categories",
                "iiprop": "url|extmetadata|mime", "iiurlwidth": 800,
                "iiextmetadatafilter": "DateTimeOriginal|Artist|LicenseShortName|LicenseUrl|ImageDescription",
                "clshow": "!hidden", "cllimit": "max",
            })
            files = []
            for page in (data.get("query") or {}).get("pages", {}).values():
                info = (page.get("imageinfo") or [{}])[0]
                if not str(info.get("mime", "")).startswith("image/"):
                    continue
                meta = info.get("extmetadata") or {}
                taken = _strip_html(meta.get("DateTimeOriginal", {}).get("value"))
                date_match = re.search(r"(\d{4})(?:[-:](\d{2})[-:](\d{2}))?", taken)
                files.append({
                    "title": page.get("title", "").replace("File:", "", 1),
                    "thumb_url": info.get("thumburl") or info.get("url"),
                    "full_url": info.get("url"),
                    "page_url": info.get("descriptionurl"),
                    "author": _strip_html(meta.get("Artist", {}).get("value")),
                    "license": meta.get("LicenseShortName", {}).get("value"),
                    "license_url": meta.get("LicenseUrl", {}).get("value"),
                    "description": _strip_html(meta.get("ImageDescription", {}).get("value"))[:300] or None,
                    "date_taken": "-".join(g for g in date_match.groups() if g) if date_match else None,
                    "livery_categories": [c["title"].replace("Category:", "", 1)
                                          for c in page.get("categories", []) if "livery" in c["title"].lower()],
                })
            self.cache.set(key, files)
            return files
        except Exception as e:  # network error or unexpected response shape
            log.warning("Commons: error fetching %s: %s", category, e)
            return cached or []

    def _livery_categories(self, base_class):
        key = f"liveries:{base_class}"
        cached, fresh = self.cache.get(key, COMMONS_TTL)
        if fresh:
            return cached
        try:
            data = _http_get_json(COMMONS_API, {
                "action": "query", "format": "json", "list": "categorymembers",
                "cmtitle": f"Category:British Rail Class {base_class}s by livery",
                "cmtype": "subcat", "cmlimit": "max",
            })
            cats = [m["title"].replace("Category:", "", 1) for m in data.get("query", {}).get("categorymembers", [])]
            self.cache.set(key, cats)
            return cats
        except Exception as e:  # network error or unexpected response shape
            log.warning("Commons: error fetching livery categories for %s: %s", base_class, e)
            return cached or []

    @staticmethod
    def _livery_score(livery_text, category_names):
        """Token overlap between a SpotLog livery description and Commons livery categories."""
        wanted = _tokens(livery_text)
        if not wanted:
            return 0.0
        best = 0.0
        for cat in category_names:
            have = _tokens(re.sub(r"^.*? in ", "", cat))  # "British Rail Class 387s in X livery" -> "X livery"
            if have:
                best = max(best, len(wanted & have) / len(have))
        return best

    def unit_photos(self, base_class, unit_number, livery_text=None):
        """Photos of the exact unit, best first. Photos whose Commons livery
        category matches the current livery are preferred, then newest."""
        files = self._category_files(f"British Rail Class {base_class} {unit_number}")

        def score(f):
            in_title = 1 if unit_number in f["title"] else 0
            livery = self._livery_score(livery_text, f["livery_categories"]) if livery_text else 0
            return (round(livery, 1), in_title, f.get("date_taken") or "")

        ranked = sorted(files, key=score, reverse=True)
        # Unit categories sometimes include photos where the unit is only in the
        # background; drop those if there's a photo that clearly features it.
        relevant = [f for f in ranked if score(f)[0] > 0 or score(f)[1]]
        if relevant:
            ranked = relevant
        return [dict(f, match="exact_unit") for f in ranked[:MAX_PHOTOS_PER_UNIT]]

    def livery_photos(self, base_class, livery_text):
        """Representative photos of the same class in the same livery."""
        if not livery_text:
            return [], None
        cats = self._livery_categories(base_class)
        scored = sorted(((self._livery_score(livery_text, [c]), c) for c in cats), reverse=True)
        if not scored or scored[0][0] < 0.5:
            return [], None
        category = scored[0][1]
        files = sorted(self._category_files(category, limit=20),
                       key=lambda f: f.get("date_taken") or "", reverse=True)
        return [dict(f, match="same_class_and_livery") for f in files[:MAX_PHOTOS_PER_UNIT]], category

    def class_summary(self, base_class):
        if not base_class or not base_class.isdigit():
            return None
        key = f"wiki:{base_class}"
        cached, fresh = self.cache.get(key, COMMONS_TTL)
        if fresh:
            return cached
        try:
            data = _http_get_json(WIKIPEDIA_SUMMARY + urllib.parse.quote(f"British_Rail_Class_{base_class}"))
            summary = {
                "title": data.get("title"),
                "description": data.get("description"),
                "extract": data.get("extract"),
                "url": (data.get("content_urls") or {}).get("desktop", {}).get("page"),
                "thumbnail": (data.get("thumbnail") or {}).get("source"),
            } if data.get("type") != "disambiguation" else None
        except Exception as e:  # network error or unexpected response shape
            log.warning("Wikipedia: error fetching Class %s: %s", base_class, e)
            summary = None
        self.cache.set(key, summary)
        return summary


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

spotlog = SpotlogClient()
commons = CommonsClient()
_unit_cache = {}
_unit_cache_lock = threading.Lock()


def _describe_unit(unit: dict, fleet_id: str, linx_livery_code: str | None = None) -> dict:
    base_class = _base_class(fleet_id) or unit["number"][:3]
    spotlog_code = unit.get("livery")
    spotlog_desc = spotlog.livery_description(spotlog_code)

    photos = commons.unit_photos(base_class, unit["number"], spotlog_desc)
    livery_photo_category = None
    if not photos:
        photos, livery_photo_category = commons.livery_photos(base_class, spotlog_desc)

    return {
        "unit_number": unit["number"],
        "class": base_class,
        "fleet_id": fleet_id,
        "name": unit.get("name"),
        "livery": {
            "code": spotlog_code,
            "description": spotlog_desc,
            "source": "spotlog",
            "linx_code": linx_livery_code,
        },
        "owner": unit.get("owner"),
        "pool": unit.get("pool"),
        "depot": unit.get("depot"),
        "carriages": unit.get("form", []),
        "photos": photos,
        "photo_match": photos[0]["match"] if photos else None,
        "livery_photo_category": livery_photo_category,
        "spotlog_url": f"{SPOTLOG_BASE}/class/UK/"
                       f"{urllib.parse.quote(unit.get('spotlog_class') or base_class, safe='')}",
    }


_class_cache = {}


def describe_class(fleet_id: str, operator_hint: str | None = None) -> dict:
    """
    Class-level appearance for when we know (or expect) the class but not the
    exact units: the livery most units of the class carry (preferring one that
    mentions the operator), a representative photo in that livery, and the
    class's Wikipedia summary. Cached per (fleet, operator).
    """
    key = (fleet_id, operator_hint)
    cached = _class_cache.get(key)
    if cached and time.time() - cached[0] < COMMONS_TTL:
        return cached[1]

    base_class = _base_class(fleet_id)
    units = spotlog.class_units(fleet_id)
    liveries = Counter(unit["livery"] for unit in units if unit.get("livery"))
    operator_tokens = _tokens(operator_hint)

    def rank(item):
        code, count = item
        mentions_operator = bool(operator_tokens & _tokens(spotlog.livery_description(code)))
        return (mentions_operator, count)

    livery_code = max(liveries.items(), key=rank)[0] if liveries else None
    livery_desc = spotlog.livery_description(livery_code)
    photos, _category = commons.livery_photos(base_class, livery_desc)
    described = {
        "class": base_class,
        "fleet_id": fleet_id,
        "livery": {
            "code": livery_code,
            "description": livery_desc,
            "share_of_class": round(liveries[livery_code] / sum(liveries.values()), 2) if livery_code else None,
            "source": "spotlog",
        },
        "photos": photos,
        "class_info": commons.class_summary(base_class),
        "unit_count": len(units),
    }
    _class_cache[key] = (time.time(), described)
    return described


def enrich_vehicles(vehicles: list[dict], fleet_id: str) -> dict:
    """
    Annotate LINX vehicles in place with their unit number / SpotLog livery and
    return {"units": [...], "class_info": {...}} describing each distinct unit
    in the formation (in formation order).
    """
    units = []
    seen = set()
    for vehicle in sorted(vehicles, key=lambda v: v.get("position") or 0):
        unit = spotlog.find_unit(vehicle.get("vehicle_id"), fleet_id)
        if not unit and vehicle.get("unit_number"):
            # LINX gives the unit number (ResourceGroupId) directly
            unit = spotlog.find_unit(vehicle["unit_number"], fleet_id)
        if not unit:
            continue
        vehicle["unit_number"] = unit["number"]
        if unit.get("livery"):
            vehicle["spotlog_livery_code"] = unit["livery"]
            vehicle["livery_description"] = spotlog.livery_description(unit["livery"])
            if vehicle.get("livery_code") in (None, "", "ZZ"):
                vehicle["livery_code_resolved"] = unit["livery"]
        if unit["number"] in seen:
            continue
        seen.add(unit["number"])

        cache_key = (unit["number"], fleet_id)
        with _unit_cache_lock:
            described = _unit_cache.get(cache_key)
        if described is None or time.time() - described[0] > COMMONS_TTL:
            described = (time.time(), _describe_unit(unit, fleet_id, vehicle.get("livery_code")))
            with _unit_cache_lock:
                _unit_cache[cache_key] = described
        units.append(described[1])

    return {
        "units": units,
        "class_info": commons.class_summary(_base_class(fleet_id)),
        "enriched_at": datetime.now().isoformat(),
    }


if __name__ == "__main__":
    # Usage: python rolling_stock.py <fleet_id> <vehicle_id> [<vehicle_id> ...]
    #   e.g. python rolling_stock.py 387/1 421142 422142 423142 424142
    import sys
    if len(sys.argv) < 3:
        print(__doc__)
        print("Usage: python rolling_stock.py <fleet_id> <vehicle_id> [<vehicle_id> ...]")
        sys.exit(1)
    fleet = sys.argv[1]
    vs = [{"vehicle_id": v, "position": i + 1, "livery_code": "ZZ"} for i, v in enumerate(sys.argv[2:])]
    result = enrich_vehicles(vs, fleet)
    print(json.dumps({"vehicles": vs, **result}, indent=2, ensure_ascii=False))
