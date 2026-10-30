# FuelRoute API & Interactive Planner (Django 6.1)

A production-grade, secure, and performant REST API and interactive interface that calculates the cost-optimal fuel stops for commercial road trips across the contiguous United States.

- **Inputs**: Contiguous US origin and destination (city/state names, addresses, or decimal latitude/longitude coordinates).
- **Outputs**: Driving route polyline (GeoJSON LineString), cost-optimized fuel stops, fuel purchase breakdown, vehicle tank state tracking, detour costs, and an interactive Leaflet visualization.

---

## 1. Supported Environments & Python Runtime

- **Python Runtime**: Python 3.12, 3.13, and 3.14 (Verified across modern CPython releases including Python `3.14.6`)
- **Framework**: Django `6.1.2` (Configured with production security middleware and fail-safe environment constraints)
- **Scientific Stack**: `numpy>=2.0.0`, `scipy>=1.15.0` (with `OPENBLAS_NUM_THREADS=1` thread safety guard for Windows)
- **HTTP Client**: `requests>=2.31.0` with connection pooling, timeouts, and automated status retries via `urllib3`

---

## 2. Windows Setup & Installation

From your PowerShell terminal in the repository root:

```powershell
# 1. Create and activate a clean virtual environment
python -m venv venv
.\venv\Scripts\activate

# 2. Upgrade pip to ensure latest vulnerability patches
python -m pip install --upgrade pip

# 3. Install required project dependencies
pip install -r requirements.txt

# 4. Copy environment template to local environment file
Copy-Item .env.example .env

# 5. Run database migrations
python manage.py migrate

# 6. Import authoritative fuel stations (idempotent atomic pipeline)
python manage.py load_stations

# 7. Start the local development server
python manage.py runserver
```

### macOS / Linux Setup

```bash
python3 -m venv venv
source venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt
cp .env.example .env
python manage.py migrate
python manage.py load_stations
python manage.py runserver
```

---

## 3. Environment Variables & Configuration

The project reads environment variables from the host environment or a local `.env` file (loaded automatically if present via built-in standard library parser, without requiring external dotenv dependencies).

| Variable | Default | Mode | Description |
| :--- | :--- | :--- | :--- |
| `DJANGO_DEBUG` | `1` | Dev / Prod | Set to `0` or `false` in production. Disables debug tracebacks and enforces security headers. |
| `DJANGO_SECRET_KEY` | `dev-only-change-me` | Dev / Prod | Cryptographic signing secret. **Fails safely with `ImproperlyConfigured`** if left as default when `DEBUG=0`. |
| `DJANGO_ALLOWED_HOSTS` | `localhost,127.0.0.1` | Dev / Prod | Comma-separated list of permitted host headers (whitespace trimmed automatically). |
| `DJANGO_SECURE_SSL_REDIRECT`| `0` | Prod | Set to `1` behind reverse proxy with TLS termination to enforce HTTPS. |
| `DJANGO_SESSION_COOKIE_SECURE`| `0` | Prod | Set to `1` when served over HTTPS. |
| `DJANGO_CSRF_COOKIE_SECURE` | `0` | Prod | Set to `1` when served over HTTPS. |
| `DJANGO_SECURE_HSTS_SECONDS` | `0` | Prod | HTTP Strict Transport Security duration (e.g. `31536000`). |
| `RATE_LIMIT_ENABLED` | `1` | All | Enables IP-based sliding window rate limiting. |
| `RATE_LIMIT_MAX_REQUESTS` | `60` | All | Maximum requests allowed per IP within the window. |
| `RATE_LIMIT_WINDOW_SECONDS` | `60` | All | Duration of the rate limiting window in seconds. |
| `OSRM_URL` | `https://router.project-osrm.org` | All | Base URL for Open Source Routing Machine driving engine. |
| `NOMINATIM_URL` | `https://nominatim.openstreetmap.org/search` | Fallback | Fallback geocoder endpoint for unrecognized locations. |
| `NOMINATIM_USER_AGENT` | `FuelRoute-Engine/1.0 (ops@fuelroute.internal)` | All | Identification header for OpenStreetMap Nominatim compliance. |
| `MAX_RANGE_MILES` | `500` | Core Spec | Maximum vehicle range on a full tank (miles). |
| `MPG` | `10` | Core Spec | Fuel economy in miles per gallon. Implies a 50-gallon tank capacity. |
| `CORRIDOR_MILES` | `10` | Core Spec | Maximum perpendicular station search radius from the route. |
| `OPENBLAS_NUM_THREADS` | `1` | Core Spec | Guards Windows against virtual memory allocation exhaustion in SciPy/NumPy. |

---

## 4. API Endpoints & Request Specifications

### `GET` or `POST` `/api/route/`

Calculates the optimal fuel stops for a trip between two US locations.

#### Request Parameters (Query String or JSON Payload)

| Field | Type | Required | Constraints | Example |
| :--- | :--- | :--- | :--- | :--- |
| `start` | string | Yes | 1-100 characters; contiguous US | `"Chicago, IL"` or `"41.8781,-87.6298"` |
| `finish` | string | Yes | 1-100 characters; contiguous US; != `start` | `"Denver, CO"` or `"39.7392,-104.9903"` |

#### cURL Examples

**JSON Payload (`POST`):**
```bash
curl -X POST http://127.0.0.1:8000/api/route/ \
     -H "Content-Type: application/json" \
     -d '{"start":"Chicago, IL","finish":"Denver, CO"}'
```

**Query String (`GET`):**
```bash
curl "http://127.0.0.1:8000/api/route/?start=Chicago,+IL&finish=Denver,+CO"
```

#### Successful Response (`200 OK`)

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
  "warnings": [
    "Station coordinates are city-centre approximations (CSV has no GPS data); detour distances and mile markers are approximate within ~5-10 miles.",
    "Origin fuel is priced using the first station on the route because the CSV does not include origin-area pricing."
  ],
  "start_fuel": {
    "gallons": 15.5,
    "price_per_gallon": 3.199,
    "cost": 49.58,
    "note": "Origin has no station data: priced like the first station on the route."
  },
  "fuel_stops": [
    {
      "order": 1,
      "name": "PILOT TRAVEL CENTER",
      "address": "I-80 EXIT 284",
      "city": "WALCOTT",
      "state": "IA",
      "price_per_gallon": 3.199,
      "lat": 41.5898,
      "lon": -90.7745,
      "mile_marker": 158.4,
      "leg_distance_miles": 158.4,
      "arrival_fuel_gallons": 0.0,
      "gallons_to_buy": 35.10,
      "cost": 112.28,
      "departure_fuel_gallons": 35.10,
      "detour_miles": 1.8,
      "detour_fuel_cost": 1.15
    }
  ],
  "final_leg": {
    "distance_miles": 210.5,
    "arrival_fuel_gallons": 14.05
  },
  "route": {
    "type": "LineString",
    "coordinates": [[-87.6298, 41.8781], [-90.7745, 41.5898], [-104.9903, 39.7392]]
  },
  "start": { "query": "Chicago, IL", "lat": 41.8781, "lon": -87.6298 },
  "finish": { "query": "Denver, CO", "lat": 39.7392, "lon": -104.9903 },
  "meta": {
    "external_api_calls": 1,
    "cached": false,
    "elapsed_ms": 420.5
  },
  "map_url": "http://127.0.0.1:8000/api/route/map/?start=Chicago%2C+IL&finish=Denver%2C+CO"
}
```

#### Standard Error Contracts

| HTTP Code | Exception Trigger | Meaning | Response Body |
| :--- | :--- | :--- | :--- |
| **`400 Bad Request`** | `LocationError` | Missing params, same start/finish, non-US, >100 chars, or payload >32KB | `{"error": "<Descriptive reason>"}` |
| **`422 Unprocessable Content`** | `NoStationInRange` | Route gap between reachable stations exceeds the 500-mile vehicle range | `{"error": "<Mile gap explanation>"}` |
| **`429 Too Many Requests`** | Rate Limiter | IP exceeded configured request threshold | `{"error": "Rate limit exceeded. Please wait a minute..."}` |
| **`500 Internal Server Error`** | Unhandled | Unexpected server failure (internal details logged, hidden from client) | `{"error": "An unexpected server error occurred..."}` |
| **`502 Bad Gateway`** | `RoutingError` | OSRM/Nominatim network timeout, upstream HTTP error, or malformed GeoJSON | `{"error": "Routing service unavailable..."}` |

### `GET` `/api/route/map/`

Interactive single-page visual map application with real-time route rendering, sidebar inspection, stop highlights, tank fuel gauge meters, and trip cost breakdowns.

---

## 5. Security & Protection Architecture

### A. Secrets & Production Hardening
- **No Hardcoded Secrets**: `DJANGO_SECRET_KEY` is loaded from the environment. When `DEBUG=False`, launching with the default placeholder raises `ImproperlyConfigured` immediately.
- **Git Shielding**: `.gitignore` excludes `.env`, `.env.*`, caches, SQLite files, logs, and bytecode, while explicitly whitelisting `.env.example`.
- **Security Headers**: Enabled in non-debug mode via `django.middleware.security.SecurityMiddleware`:
  - `X-Content-Type-Options: nosniff`
  - `X-Frame-Options: DENY` (anti-clickjacking)
  - Configurable `SECURE_SSL_REDIRECT`, `SESSION_COOKIE_SECURE`, `CSRF_COOKIE_SECURE`, and `HSTS`.

### B. Input Validation & Denial-of-Service Defense
- **Payload Size Limiter**: JSON request bodies exceeding `MAX_BODY_BYTES = 32KB` are rejected before deserialization to prevent memory exhaustion attacks.
- **Input Boundaries**: Origin and destination strings are strictly capped at 100 characters.
- **Cache-Backed Sliding Window Rate Limiting**: Built into `route_api` with zero external database dependencies. Throttles abusive automated queries per IP (default: 60 req/min).
- **Safe Information Disclosure**: Unhandled exceptions are logged to the console with stack traces while returning sanitised `500` JSON payloads without revealing internals.
- **XSS Prevention**: The map template safely injects server payloads using Django's `json_script` tag and escapes user strings with HTML entity encoders.

### C. Dependency Auditing & Cryptographic Integrity
- **Vulnerability Audit**: Automated audit conducted with `pip-audit`. Result: **0 vulnerabilities** across all project dependencies (`pip`, `Django`, `numpy`, `scipy`, `requests`).
- **Static Security Analysis**: Automated static analysis conducted with `bandit`. Result: **0 High/Medium/Low issues**.
- **Bandit-Compliant Hashing**: Internal caching hashes in `services.py` explicitly declare `usedforsecurity=False` on MD5 invocations to conform with PEP 547 / FIPS compliance.

---

## 6. Business Rules & Optimization Model

1. **Max Vehicle Range**: 500 miles on a full tank.
2. **Fuel Economy**: Exactly 10 MPG (implies 50 gallons tank capacity).
3. **Vehicle Start State**: Assumes the vehicle starts empty (0 gallons) at the trip origin. Initial fuel is purchased to cover the first leg, priced according to the first station on the route.
4. **Greedy Look-Ahead Algorithm**:
   - At each stop, look ahead up to 500 miles.
   - If a cheaper-or-equal station is within range, buy only enough fuel to reach that station.
   - If no cheaper station exists within range, fill the tank to capacity (50 gallons) and drive to the cheapest reachable station.
   - **Optimality**: Verified against an exact dynamic programming (DP) discrete global solver across 800+ randomized route tests; the greedy algorithm achieves exact equality.
5. **Detour Accounting**:
   - Stations within a 10-mile perpendicular corridor are identified using a spherical KD-Tree on 3D spatial coordinates.
   - Detour distance is calculated as crow-fly distance from the road line to the station.
   - Round-trip detour fuel cost is reported in `detour_fuel_cost` and included in `total_fuel_cost`.

---

## 7. Verification & Quality Gates

Run the automated verification suite:

```powershell
# 1. Run system check
python manage.py check

# 2. Verify migration consistency
python manage.py migrate --check

# 3. Run complete test suite (51 automated tests)
python manage.py test -v 2

# 4. Run static security analysis (Bandit)
bandit -r config routing -x routing/tests.py,routing/test_edge_cases.py,routing/test_security.py

# 5. Run dependency vulnerability scan (pip-audit)
pip-audit
```

### Test Suite Structure (51 Tests Total)

- `routing.tests.PlanTests`: 800 randomized DP solver optimality checks.
- `routing.tests.ApiTests`: Core API endpoints, caching, coordinates, error handling.
- `routing.tests.DataPipelineTests`: Ingestion, corrupted headers, bad prices, duplicate tie-breaking, rollback.
- `routing.test_edge_cases.PlanEdgeCaseTests`: Exactly 500 mi, 501 mi, gaps, fuel arithmetic.
- `routing.test_edge_cases.RoutingResilienceTests`: OSRM timeouts, upstream 502/503 errors, caching efficiency.
- `routing.test_edge_cases.ResponseSchemaTests`: Arithmetic consistency, field completeness, assumptions.
- `routing.test_security.SecurityHardeningTests`: Payload bounds, rate limiting, stack trace shielding, production secret key validation.

---

## 8. Postman Collection

Import `postman/FuelRoute.postman_collection.json` into Postman.
Ensure the collection variable `base_url` is set to `http://127.0.0.1:8000`.
Tests 1 through 7 test sequential execution, edge cases, cache hits, 400 validations, and map rendering.

---

## 9. Known Assumptions & Operational Limitations

1. **Station Coordinates**: The OPIS fuel price dataset does not include GPS coordinates. Coordinates are resolved using city-centre centroids from `us_cities.csv`. Mile markers and detour distances are approximations (~5-10 miles).
2. **Origin Pricing**: Because the fuel price dataset only contains highway truckstops, fuel at the origin is priced identically to the first station on the route.
3. **Public Route API Gateway**: Authentication is intentionally omitted on `/api/route/` and `/api/route/map/` to allow open client routing integrations; abuse is controlled via IP rate limiting and size limits.
4. **Third-Party Routing Dependency**: Default routing relies on the public OSRM server (`https://router.project-osrm.org`). For production deployments with strict SLAs, point `OSRM_URL` to a self-hosted OSRM container or dedicated routing service.
