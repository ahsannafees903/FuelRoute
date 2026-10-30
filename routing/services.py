"""Fuel-stop planner.
Speed plan: local gazetteer for place names (0 API calls), ONE OSRM call for the route,
numpy + KD-tree for the station search, greedy look-ahead for the plan, cache for repeats."""
import csv
import hashlib
import re
from bisect import bisect_right
from functools import lru_cache
from pathlib import Path
from time import perf_counter

import numpy as np
import requests
from django.conf import settings
from django.core.cache import cache
from scipy.spatial import cKDTree

R_EARTH = 3958.8
DATA = Path(__file__).resolve().parent / "data"
US_BOX = (24.3, 49.6, -125.1, -66.8)          # lower 48: lat_min, lat_max, lon_min, lon_max
CANADA = set("AB BC MB NB NL NS NT NU ON PE QC SK YT".split())
STATES = dict(p.split(":") for p in (
    "Alabama:AL,Alaska:AK,Arizona:AZ,Arkansas:AR,California:CA,Colorado:CO,Connecticut:CT,Delaware:DE,"
    "District Of Columbia:DC,Florida:FL,Georgia:GA,Hawaii:HI,Idaho:ID,Illinois:IL,Indiana:IN,Iowa:IA,"
    "Kansas:KS,Kentucky:KY,Louisiana:LA,Maine:ME,Maryland:MD,Massachusetts:MA,Michigan:MI,Minnesota:MN,"
    "Mississippi:MS,Missouri:MO,Montana:MT,Nebraska:NE,Nevada:NV,New Hampshire:NH,New Jersey:NJ,"
    "New Mexico:NM,New York:NY,North Carolina:NC,North Dakota:ND,Ohio:OH,Oklahoma:OK,Oregon:OR,"
    "Pennsylvania:PA,Rhode Island:RI,South Carolina:SC,South Dakota:SD,Tennessee:TN,Texas:TX,Utah:UT,"
    "Vermont:VT,Virginia:VA,Washington:WA,West Virginia:WV,Wisconsin:WI,Wyoming:WY").split(","))
from urllib3.util import Retry
from requests.adapters import HTTPAdapter

COORD = re.compile(r"\s*(-?\d{1,3}(?:\.\d+)?)\s*,\s*(-?\d{1,3}(?:\.\d+)?)\s*")

SESSION = requests.Session()
_retries = Retry(total=2, backoff_factor=0.3, status_forcelist=[502, 503, 504])
_adapter = HTTPAdapter(max_retries=_retries)
SESSION.mount("https://", _adapter)
SESSION.mount("http://", _adapter)

def get_user_agent():
    return {"User-Agent": getattr(settings, "NOMINATIM_USER_AGENT", "FuelRoute-Engine/1.0 (ops@fuelroute.internal)")}


class LocationError(ValueError): pass          # -> HTTP 400
class NoStationInRange(ValueError): pass       # -> HTTP 422
class RoutingError(RuntimeError): pass         # -> HTTP 502


# ---------- places ----------
def norm(s):
    s = str(s).upper().strip()
    s = re.sub(r"^ST\.?\s", "SAINT ", s)
    s = re.sub(r"^MT\.?\s", "MOUNT ", s)
    s = re.sub(r"^FT\.?\s", "FORT ", s)
    return re.sub(r"[^A-Z0-9 ]", "", s)


@lru_cache(maxsize=1)
def gazetteer():
    g = {}
    with open(DATA / "us_cities.csv", newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            g.setdefault((norm(r["city"]), r["state"]), (float(r["lat"]), float(r["lon"])))
    return g


def in_us(lat, lon):
    return US_BOX[0] <= lat <= US_BOX[1] and US_BOX[2] <= lon <= US_BOX[3]


def _local(text):
    t = re.sub(r"[,\s]+(USA|US|United States)$", "", text.strip(), flags=re.I)
    parts = [p.strip() for p in t.split(",") if p.strip()]
    if len(parts) >= 2:
        city, st = parts[0], parts[1]
    else:
        m = re.fullmatch(r"(.+?)\s+([A-Za-z]{2})", t)
        if not m:
            return None
        city, st = m[1], m[2]
    st = st.upper() if len(st) == 2 else STATES.get(st.title(), "")
    if st in CANADA:
        raise LocationError("Only locations inside the USA are supported.")
    return gazetteer().get((norm(city), st))


def _nominatim(text, counter):
    """Geocoding fallback for inputs the local gazetteer cannot resolve."""
    counter["calls"] += 1
    try:
        resp = SESSION.get(settings.NOMINATIM_URL, headers=get_user_agent(), timeout=(5, 10),
                           params={"q": text, "format": "json", "limit": 1, "countrycodes": "us"})
        resp.raise_for_status()
        j = resp.json()
    except requests.Timeout:
        raise RoutingError("Geocoding service timed out, try again.")
    except requests.RequestException:
        raise RoutingError("Geocoding service unavailable, try again.")
    except ValueError:
        raise RoutingError("Geocoding service returned an invalid response.")
    if not j:
        raise LocationError(f"Could not find a US location for '{text}'.")
    return float(j[0]["lat"]), float(j[0]["lon"])


def resolve(text, counter):
    key = "geo:" + hashlib.md5(re.sub(r"\s+", " ", text.strip().lower()).encode(), usedforsecurity=False).hexdigest()
    hit = cache.get(key)
    if hit:
        return hit
    m = COORD.fullmatch(text)
    pt = (float(m[1]), float(m[2])) if m else (_local(text) or _nominatim(text, counter))
    if not in_us(*pt):
        raise LocationError("Location must be inside the contiguous USA.")
    cache.set(key, pt, 86400)
    return pt


# ---------- routing (the ONE external call) ----------
def decode_polyline(s, f=1e5):
    if not s or not isinstance(s, str):
        raise RoutingError("Route geometry is empty or missing.")
    lat = lon = i = 0
    out = []
    while i < len(s):
        for k in (0, 1):
            shift = res = 0
            while True:
                b = ord(s[i]) - 63
                i += 1
                res |= (b & 31) << shift
                shift += 5
                if b < 32:
                    break
            d = ~(res >> 1) if res & 1 else res >> 1
            if k == 0:
                lat += d
            else:
                lon += d
        out.append((lat / f, lon / f))
    return np.array(out)


def fetch_route(a, b, counter):
    """Fetch a driving route from OSRM. One call per uncached route."""
    counter["calls"] += 1
    url = f"{settings.OSRM_URL}/route/v1/driving/{a[1]},{a[0]};{b[1]},{b[0]}"
    try:
        resp = SESSION.get(url, params={"overview": "full", "geometries": "polyline"},
                           headers=get_user_agent(), timeout=(5, 25))
        resp.raise_for_status()
        j = resp.json()
    except requests.Timeout:
        raise RoutingError("Routing service timed out, try again.")
    except requests.RequestException:
        raise RoutingError("Routing service unavailable, try again.")
    except ValueError:
        raise RoutingError("Routing service returned an invalid response.")
    if j.get("code") != "Ok":
        raise RoutingError(f"No driving route found ({j.get('code')}).")
    if not j.get("routes"):
        raise RoutingError("Routing service returned no routes.")
    rt = j["routes"][0]
    if "geometry" not in rt or "distance" not in rt:
        raise RoutingError("Routing service returned an incomplete route.")
    return decode_polyline(rt["geometry"]), rt["distance"] / 1609.344


# ---------- geometry ----------
def haversine(lat1, lon1, lat2, lon2):
    p1, p2 = np.radians(lat1), np.radians(lat2)
    h = np.sin((p2 - p1) / 2) ** 2 + np.cos(p1) * np.cos(p2) * np.sin(np.radians(lon2 - lon1) / 2) ** 2
    return 2 * R_EARTH * np.arcsin(np.sqrt(h))


def xyz(lat, lon):
    la, lo = np.radians(lat), np.radians(lon)
    return np.column_stack((np.cos(la) * np.cos(lo), np.cos(la) * np.sin(lo), np.sin(la)))


@lru_cache(maxsize=1)
def station_index():
    from .models import Station
    rows = list(Station.objects.values_list("id", "name", "address", "city", "state", "price", "lat", "lon"))
    if not rows:
        raise RuntimeError("No stations loaded. Run: python manage.py load_stations")
    lat = np.array([r[6] for r in rows])
    lon = np.array([r[7] for r in rows])
    price = np.array([r[5] for r in rows])
    return {"rows": rows, "price": price, "xyz": xyz(lat, lon), "median": float(np.median(price))}


def stations_along(path, total_miles):
    """Resample the route every 1 mile, then keep stations within the corridor (one KD-tree query).

    Returns a list of station dicts sorted by mile marker.  Each dict includes:
      - ``detour``: **one-way** crow-fly distance from the route to the station, in miles.
    """
    idx = station_index()
    seg = haversine(path[:-1, 0], path[:-1, 1], path[1:, 0], path[1:, 1])
    cum = np.concatenate(([0.0], np.cumsum(seg)))
    k = (total_miles / cum[-1]) if cum[-1] > 1e-6 else 1.0     # match OSRM's road distance
    grid = np.append(np.arange(0, cum[-1], 1.0), cum[-1])
    lat, lon = np.interp(grid, cum, path[:, 0]), np.interp(grid, cum, path[:, 1])
    chord = 2 * np.sin(settings.CORRIDOR_MILES / (2 * R_EARTH))
    d, j = cKDTree(xyz(lat, lon)).query(idx["xyz"], distance_upper_bound=chord)
    sel = np.flatnonzero(np.isfinite(d))
    miles = grid[j[sel]] * k
    ok = miles < total_miles - 0.5                              # a station AT the destination is useless
    sel, miles = sel[ok], miles[ok]
    # keep only the cheapest station in each 2-mile slice of road
    b = (miles // 2).astype(int)
    order = np.lexsort((idx["price"][sel], b))
    keep = order[np.r_[True, b[order][1:] != b[order][:-1]]] if len(order) else order
    stops = []
    for i in keep[np.argsort(miles[keep])]:
        r = idx["rows"][sel[i]]
        stops.append({"id": r[0], "name": r[1], "address": r[2], "city": r[3], "state": r[4],
                      "price": float(r[5]), "lat": float(r[6]), "lon": float(r[7]),
                      "mile": float(miles[i]),
                      "detour": float(2 * R_EARTH * np.arcsin(d[sel[i]] / 2))})
    thin = [[round(float(x), 4), round(float(y), 4)] for y, x in zip(lat[::3], lon[::3])]
    thin.append([round(float(lon[-1]), 4), round(float(lat[-1]), 4)])
    return stops, thin


# ---------- the optimizer ----------
def plan(stops, total, start_price, rng=500, mpg=10, min_buy=0):
    """Look one tank ahead: if a cheaper stop is reachable, buy only enough to get there;
    otherwise fill up and drive to the cheapest stop in range.  The origin buys at start_price.
    min_buy (miles of range): skip a cheaper stop if it would mean buying less than this here.

    Constants:
        rng   = max driving range on a full tank (500 miles)
        mpg   = fuel economy (10 miles per gallon)
        implied tank capacity = rng / mpg = 50 gallons

    The ``fuel`` variable tracks **miles of driving range remaining** (not gallons).
    Gallons are derived at purchase time: gallons = miles_purchased / mpg.
    """
    pts = stops + [{"mile": total, "price": 0.0, "end": True}]
    miles = [p["mile"] for p in pts]
    cur = {"mile": 0.0, "price": start_price, "start": True}
    fuel = cost = 0.0                                           # fuel = miles of range left
    buys = []
    while True:
        ahead = pts[bisect_right(miles, cur["mile"]):bisect_right(miles, cur["mile"] + rng)]
        if not ahead:
            raise NoStationInRange(f"No fuel station within {rng} miles after mile {cur['mile']:.0f}.")
        def worth_it(s):                                        # no tiny purchases just to save pennies
            need = s["mile"] - cur["mile"] - fuel
            return s["price"] < cur["price"] and (s.get("end") or need <= 0 or need >= min_buy)
        cheaper = next((s for s in ahead if worth_it(s)), None)
        if cheaper:
            target, buy = cheaper, max(0.0, cheaper["mile"] - cur["mile"] - fuel)
        else:
            target, buy = min(ahead, key=lambda s: s["price"]), rng - fuel
            if buy < min_buy:                                   # tiny top-up: skip it if we can already reach a stop
                reach = [s for s in ahead if s["mile"] - cur["mile"] <= fuel]
                if reach:
                    target, buy = min(reach, key=lambda s: s["price"]), 0.0
        if buy > 1e-9:
            gal = buy / mpg
            cost += gal * cur["price"]
            buys.append({**cur, "gallons": gal, "cost": gal * cur["price"]})
        fuel += buy - (target["mile"] - cur["mile"])
        cur = target
        if cur.get("end"):
            return buys, cost


# ---------- public entry point ----------
def build_plan(start_text, finish_text):
    t0 = perf_counter()
    counter = {"calls": 0}
    a, b = resolve(start_text, counter), resolve(finish_text, counter)
    key = "plan:" + hashlib.md5(f"{a[0]:.3f},{a[1]:.3f}>{b[0]:.3f},{b[1]:.3f}".encode(), usedforsecurity=False).hexdigest()
    out = cache.get(key)
    cached = out is not None
    if not cached:
        out = _compute(a, b, counter)
        cache.set(key, out, 3600)
    return {**out,
            "start": {"query": start_text, "lat": a[0], "lon": a[1]},
            "finish": {"query": finish_text, "lat": b[0], "lon": b[1]},
            "meta": {"external_api_calls": counter["calls"], "cached": cached,
                     "elapsed_ms": round((perf_counter() - t0) * 1000, 1)}}


def _compute(a, b, counter):
    path, miles = fetch_route(a, b, counter)
    if miles < 0.1:
        raise LocationError("Start and finish locations resolve to the same point (trip distance is 0 miles).")
    stops, thin = stations_along(path, miles)
    p0 = stops[0]["price"] if stops else station_index()["median"]
    buys, route_cost = plan(stops, miles, p0, settings.MAX_RANGE_MILES, settings.MPG, settings.MIN_BUY_MILES)

    mpg = settings.MPG
    tank_gal = settings.MAX_RANGE_MILES / mpg   # 50 gallons

    # Separate the origin purchase (displayed distinctly)
    start_fuel = None
    if buys and buys[0].get("start"):
        s = buys.pop(0)
        start_fuel = {"gallons": round(s["gallons"], 2), "price_per_gallon": round(s["price"], 3),
                      "cost": round(s["cost"], 2),
                      "note": "Origin has no station data: priced like the first station on the route."}

    # Build auditable per-stop breakdown with fuel state tracking (gallons).
    fuel_gal = start_fuel["gallons"] if start_fuel else 0.0
    purchased_gal = fuel_gal
    prev_mile = 0.0
    detour_fuel_cost = 0.0
    detour_feasibility_ok = True
    fuel_stop_list = []

    for n, s in enumerate(buys, 1):
        leg_dist = s["mile"] - prev_mile
        leg_burn_gal = leg_dist / mpg
        arrival_gal = fuel_gal - leg_burn_gal

        # Detour: one-way crow-fly distance from route to station (miles)
        det_one_way = s.get("detour", 0.0)
        det_round_trip_gal = (det_one_way * 2) / mpg
        det_cost = det_round_trip_gal * s["price"]
        detour_fuel_cost += det_cost

        # Detour feasibility: would the vehicle run dry driving to the station?
        if arrival_gal < det_round_trip_gal + 0.01:
            detour_feasibility_ok = False

        purchase_gal = s["gallons"]
        departure_gal = arrival_gal + purchase_gal
        purchased_gal += purchase_gal

        fuel_stop_list.append({
            "order": n, "name": s["name"], "address": s["address"], "city": s["city"],
            "state": s["state"], "price_per_gallon": round(s["price"], 3),
            "lat": s["lat"], "lon": s["lon"], "mile_marker": round(s["mile"], 1),
            "leg_distance_miles": round(leg_dist, 1),
            "arrival_fuel_gallons": round(arrival_gal, 2),
            "gallons_to_buy": round(purchase_gal, 2), "cost": round(s["cost"], 2),
            "departure_fuel_gallons": round(departure_gal, 2),
            "detour_miles": round(det_one_way, 1),
            "detour_fuel_cost": round(det_cost, 2),
        })

        fuel_gal = departure_gal
        prev_mile = s["mile"]

    # Final leg to destination
    final_leg_dist = miles - prev_mile
    final_arrival_gal = fuel_gal - (final_leg_dist / mpg)

    route_fuel_cost = round(route_cost, 2)
    detour_fuel_cost = round(detour_fuel_cost, 2)
    total_fuel_cost = round(route_cost + detour_fuel_cost, 2)

    # Warnings about model limitations
    warnings = [
        "Station coordinates are city-centre approximations (CSV has no GPS data); "
        "detour distances and mile markers are approximate within ~5-10 miles.",
    ]
    if start_fuel:
        warnings.append(
            "Origin fuel is priced using the first station on the route because "
            "the CSV does not include origin-area pricing."
        )
    if not detour_feasibility_ok:
        warnings.append(
            "One or more stops have estimated detour distances that may exceed "
            "available fuel on arrival. Detour distances are crow-fly approximations."
        )

    return {
        "distance_miles": round(miles, 1),
        "total_gallons": round(miles / mpg, 2),
        "purchased_gallons": round(purchased_gal, 2),
        "route_fuel_cost": route_fuel_cost,
        "detour_fuel_cost": detour_fuel_cost,
        "total_fuel_cost": total_fuel_cost,
        "assumptions": {
            "max_range_miles": settings.MAX_RANGE_MILES,
            "mpg": mpg,
            "tank_capacity_gallons": tank_gal,
            "corridor_miles": settings.CORRIDOR_MILES,
            "start_fuel_level": "empty (0 gallons)",
            "station_position": "city_centre_fallback (CSV has no coordinates)",
            "detour_model": "crow-fly approximation; round-trip fuel is costed but not deducted from tank state",
        },
        "warnings": warnings,
        "start_fuel": start_fuel,
        "fuel_stops": fuel_stop_list,
        "final_leg": {
            "distance_miles": round(final_leg_dist, 1),
            "arrival_fuel_gallons": round(max(final_arrival_gal, 0.0), 2),
        },
        "route": {"type": "LineString", "coordinates": thin},
    }
