# FuelRoute API (Django 6.1)

A REST API that plans the cheapest fuel stops for a road trip across the United States.

**Input:** US start and finish locations (city names, state codes, or coordinates).
**Output:** Driving route (GeoJSON), cost-optimized fuel stops, total fuel cost, interactive map.

---

## Quick Start (Windows)

```powershell
cd fuelroute
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
python manage.py migrate
python manage.py load_stations          # imports fuel_prices.csv (once, idempotent)
python manage.py runserver
```

## Quick Start (macOS / Linux)

```bash
cd fuelroute
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
python manage.py migrate
python manage.py load_stations
python manage.py runserver
```

---

## API Usage

### POST or GET `/api/route/`

**Query Parameters or JSON Body:**
| Parameter | Required | Description |
|-----------|----------|-------------|
| `start`   | Yes      | US location: `"Chicago, IL"`, `"41.8781,-87.6298"`, or `"123 Main St, Springfield IL"` |
| `finish`  | Yes      | US location (same formats) |

**Examples:**

```bash
# GET
curl "http://127.0.0.1:8000/api/route/?start=Chicago,+IL&finish=Denver,+CO"

# POST
curl -X POST http://127.0.0.1:8000/api/route/ \
     -H "Content-Type: application/json" \
     -d '{"start":"Chicago, IL","finish":"Denver, CO"}'
```

**Response (200 OK):**
```json
{
  "distance_miles": 1003.2,
  "total_gallons": 100.32,
  "purchased_gallons": 100.35,
  "route_fuel_cost": 324.18,
  "detour_fuel_cost": 2.47,
  "total_fuel_cost": 326.65,
  "assumptions": {
    "max_range_miles": 500,
    "mpg": 10,
    "tank_capacity_gallons": 50.0,
    "corridor_miles": 10,
    "start_fuel_level": "empty (0 gallons)",
    "station_position": "city_centre_fallback (CSV has no coordinates)",
    "detour_model": "crow-fly approximation; round-trip fuel is costed but not deducted from tank state"
  },
  "warnings": ["..."],
  "start_fuel": { "gallons": 15.5, "price_per_gallon": 3.199, "cost": 49.58, "note": "..." },
  "fuel_stops": [
    {
      "order": 1,
      "name": "PILOT TRAVEL CENTER",
      "city": "DAVENPORT", "state": "IA",
      "price_per_gallon": 3.199,
      "mile_marker": 182.3,
      "leg_distance_miles": 182.3,
      "arrival_fuel_gallons": 5.0,
      "gallons_to_buy": 31.77,
      "cost": 101.63,
      "departure_fuel_gallons": 36.77,
      "detour_miles": 2.1,
      "detour_fuel_cost": 1.34
    }
  ],
  "final_leg": { "distance_miles": 120.5, "arrival_fuel_gallons": 3.72 },
  "route": { "type": "LineString", "coordinates": [...] },
  "start": { "query": "Chicago, IL", "lat": 41.8781, "lon": -87.6298 },
  "finish": { "query": "Denver, CO", "lat": 39.7392, "lon": -104.9903 },
  "meta": { "external_api_calls": 1, "cached": false, "elapsed_ms": 1245.3 },
  "map_url": "http://127.0.0.1:8000/api/route/map/?start=Chicago%2C+IL&finish=Denver%2C+CO"
}
```

**Error Responses:**
| Code | When |
|------|------|
| 400  | Missing/invalid input, non-US location |
| 422  | No fuel station within 500-mile range |
| 502  | OSRM or Nominatim unavailable |

### GET `/api/route/map/`

Interactive Leaflet map showing the route, fuel stops, and cost summary. Open in a browser.

---

## Architecture


### Performance Design (Why It Is Fast)

1. **Local gazetteer** resolves `"City, ST"` from a bundled CSV — **0 geocoding API calls** for standard inputs.
2. **ONE OSRM call** per new route (overview=full, polyline). Repeat requests are served from in-memory cache (**0 calls**).
3. **Stations loaded once** into numpy arrays. A KD-tree finds every station within 10 miles of the road in **one query**.
4. **Greedy look-ahead optimizer** produces cost-optimal fuel plans (proven against exact DP solver across 800+ random scenarios).

### Algorithm

- **Model**: Vehicle starts empty (0 gallons). Range = 500 miles. MPG = 10. Tank = 50 gallons.
- **Optimizer**: At each position, look ahead up to 500 miles. If a cheaper-or-equal stop is reachable, buy just enough to reach it. Otherwise fill up and drive to the cheapest reachable stop.
- **Optimality proof**: Unit tests compare the greedy output against an exact dynamic-programming solver across hundreds of randomized scenarios (ranges 20-500, routes 30-1200 miles). The greedy result matches the DP optimum to 6 decimal places.
- **Detour handling**: Station detour distances (crow-fly from route to station) are computed but NOT deducted from the tank state. Round-trip detour fuel cost is calculated separately. This is documented in the `assumptions.detour_model` field and `warnings` array.

### Station Data Pipeline

- Source: `routing/data/fuel_prices.csv` (8,151 rows).
- 620 Canadian rows skipped. 12 unmatched US cities skipped.
- Multiple records per OPIS ID: lowest price kept (deterministic tie-breaking by name length, then row order).
- Result: 6,614 US stations loaded.
- Coordinates: city-centre fallback from `us_cities.csv` gazetteer (CSV has no GPS data).
- Import is atomic (transaction) and idempotent (safe to re-run).

---

## Testing

```powershell
python manage.py test routing -v 2
python manage.py check
python manage.py migrate --check
```

### Test Coverage

| Category | Tests | Description |
|----------|-------|-------------|
| Optimizer (PlanTests) | 1 | 800 random DP comparisons |
| Optimizer edge cases | 12 | Range boundaries, gaps, cheap unreachable, final leg, arithmetic |
| API integration | 8 | GET, POST, JSON body, coordinates, errors, cache, map |
| API resilience | 5 | OSRM timeout, HTTP error, malformed JSON, Nominatim timeout, cache |
| Response schema | 6 | All fields present, cost arithmetic, fuel state, capacity |
| Data pipeline | 11 | Full CSV import, header validation, price validation, duplicates, idempotency, rollback |

### Postman

Import `postman/FuelRoute.postman_collection.json`. Set variable `base_url` to `http://127.0.0.1:8000`.

---

## Configuration

All settings via environment variables or `config/settings.py`:

| Setting | Default | Description |
|---------|---------|-------------|
| `DJANGO_SECRET_KEY` | dev-only placeholder | Change for production |
| `DJANGO_DEBUG` | `1` | Set to `0` for production |
| `OSRM_URL` | `https://router.project-osrm.org` | Self-host for reliability |
| `MAX_RANGE_MILES` | 500 | Vehicle full-tank range |
| `MPG` | 10 | Fuel economy |
| `CORRIDOR_MILES` | 10 | Station search radius from route |

---

## Assumptions and Known Limitations

1. **No station GPS data**: The CSV lacks coordinates. Station positions use city-centre fallback from a gazetteer, so detour distances are approximate (~5-10 miles).
2. **Origin pricing**: The origin has no station data and is priced like the first station on the route. This is documented in the `start_fuel.note` and `warnings` fields.
3. **Detour fuel approximation**: Round-trip detour fuel is costed (in `detour_fuel_cost`) but not deducted from the tank state. Within the 10-mile corridor, maximum detour round-trip is ~20 miles (~4% of range).
4. **OSRM public server**: No uptime guarantee. Set `OSRM_URL` to a self-hosted instance for production.
5. **No authentication**: The API has no auth or rate limiting. Not production-ready without these.
6. **Canadian data excluded**: 620 Canadian station records are explicitly skipped.
