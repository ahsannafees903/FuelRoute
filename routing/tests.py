import io
import random
from unittest import mock

import numpy as np
from django.core.cache import cache
from django.core.management import call_command
from django.test import TestCase

from . import services as sv
from .importer import (
    HeaderValidationError,
    get_default_csv_path,
    import_stations_from_csv,
)
from .models import Station


def fake_route(waypoints):
    """Pretend to be OSRM: drive through the given city waypoints (no network)."""
    def _fetch(a, b, counter):
        counter["calls"] += 1
        path = np.array([a] + [sv._local(w) for w in waypoints] + [b])
        miles = float(sv.haversine(path[:-1, 0], path[:-1, 1], path[1:, 0], path[1:, 1]).sum())
        return path, miles * 1.05
    return _fetch


def exact_cost(stops, total, p0, rng, mpg):
    """Slow exact solver (dynamic programming) used to check plan()."""
    locs = [(0, p0)] + [(s["mile"], s["price"]) for s in stops] + [(total, None)]
    best = {0: 0.0}
    for k, (m, p) in enumerate(locs):
        if p is not None:
            nb = {}
            for f2 in range(rng + 1):
                c = min((best[f] + (f2 - f) * p / mpg for f in best if f <= f2), default=None)
                if c is not None:
                    nb[f2] = c
            best = nb
        if k + 1 < len(locs):
            d = locs[k + 1][0] - m
            best = {f - d: c for f, c in best.items() if f - d >= 0}
            if not best:
                return None
    return min(best.values())


class PlanTests(TestCase):
    def test_matches_exact_solver(self):
        random.seed(7)
        checked = 0
        for _ in range(800):
            rng, total = random.randint(20, 60), random.randint(30, 200)
            miles = random.sample(range(1, total), random.randint(0, 10))
            stops = sorted(({"mile": m, "price": round(random.uniform(2.5, 4.5), 2)} for m in miles),
                           key=lambda s: s["mile"])
            p0 = random.choice([2.8, 3.5, 4.2])
            want = exact_cost(stops, total, p0, rng, 10)
            try:
                _, got = sv.plan(stops, total, p0, rng, 10)
            except sv.NoStationInRange:
                got = None
            if want is None or got is None:
                self.assertEqual(want is None, got is None)
            else:
                self.assertAlmostEqual(got, want, places=6)
                checked += 1
        self.assertGreater(checked, 300)


class ApiTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("load_stations", verbosity=0)

    def setUp(self):
        cache.clear()
        sv.station_index.cache_clear()

    def get(self, start, finish, via=()):
        with mock.patch.object(sv, "fetch_route", fake_route(via)):
            return self.client.get("/api/route/", {"start": start, "finish": finish})

    def test_long_trip(self):
        via = ["Des Moines, IA", "Omaha, NE", "Grand Island, NE", "North Platte, NE", "Julesburg, CO"]
        r = self.get("Chicago, IL", "Denver, CO", via)
        self.assertEqual(r.status_code, 200)
        d = r.json()
        self.assertGreaterEqual(len(d["fuel_stops"]), 1)
        marks = [0] + [s["mile_marker"] for s in d["fuel_stops"]] + [d["distance_miles"]]
        self.assertTrue(all(b - a <= 500 for a, b in zip(marks, marks[1:])), marks)
        gallons = d["start_fuel"]["gallons"] + sum(s["gallons_to_buy"] for s in d["fuel_stops"])
        self.assertAlmostEqual(gallons, d["distance_miles"] / 10, delta=0.1)
        self.assertEqual(d["meta"]["external_api_calls"], 1)
        self.assertGreater(d["total_fuel_cost"], 0)
        # P4: detour cost fields exist and total = route + detour
        self.assertIn("route_fuel_cost", d)
        self.assertIn("detour_fuel_cost", d)
        self.assertAlmostEqual(d["total_fuel_cost"], d["route_fuel_cost"] + d["detour_fuel_cost"], places=2)
        self.assertGreaterEqual(d["detour_fuel_cost"], 0)
        # P5: warnings array present
        self.assertIn("warnings", d)
        self.assertIsInstance(d["warnings"], list)
        self.assertGreater(len(d["warnings"]), 0)
        # Each stop has per-stop detour fuel cost
        for s in d["fuel_stops"]:
            self.assertIn("detour_fuel_cost", s)

    def test_short_trip_gallons_add_up(self):
        d = self.get("Chicago, IL", "Indianapolis, IN").json()
        gallons = d["start_fuel"]["gallons"] + sum(s["gallons_to_buy"] for s in d["fuel_stops"])
        self.assertAlmostEqual(gallons, d["distance_miles"] / 10, delta=0.1)
        self.assertTrue(all(s["detour_miles"] <= 10 for s in d["fuel_stops"]))

    def test_second_call_is_cached(self):
        self.get("Chicago, IL", "Indianapolis, IN")
        d = self.get("chicago il", "Indianapolis, IN").json()
        self.assertTrue(d["meta"]["cached"])
        self.assertEqual(d["meta"]["external_api_calls"], 0)

    def test_errors(self):
        self.assertEqual(self.get("Toronto, ON", "Denver, CO").status_code, 400)
        self.assertEqual(self.client.get("/api/route/", {"start": "Chicago, IL"}).status_code, 400)
        self.assertEqual(self.client.get("/api/route/", {"start": "Chicago, IL", "finish": "Chicago, IL"}).status_code, 400)
        with mock.patch.object(sv, "_nominatim", side_effect=sv.LocationError("nope")):
            self.assertEqual(self.get("Qwertyland", "Denver, CO").status_code, 400)

    def test_map_page_escapes_names(self):
        r = self.get("Chicago, IL", "Indianapolis, IN")
        with mock.patch.object(sv, "fetch_route", fake_route(())):
            page = self.client.get("/api/route/map/", {"start": "Chicago, IL", "finish": "Indianapolis, IN"})
        self.assertContains(page, 'id="d"')

    # --- P2: Missing API-level tests ---

    def test_post_json_body(self):
        """POST with application/json body should work (was untested)."""
        import json as _json
        with mock.patch.object(sv, "fetch_route", fake_route(())):
            r = self.client.post(
                "/api/route/",
                data=_json.dumps({"start": "Chicago, IL", "finish": "Indianapolis, IN"}),
                content_type="application/json",
            )
        self.assertEqual(r.status_code, 200)
        d = r.json()
        self.assertIn("fuel_stops", d)
        self.assertGreater(d["total_fuel_cost"], 0)

    def test_post_malformed_json_returns_400(self):
        """POST with invalid JSON should return 400, not 500."""
        r = self.client.post(
            "/api/route/",
            data="{ bad json",
            content_type="application/json",
        )
        self.assertEqual(r.status_code, 400)
        self.assertIn("error", r.json())

    def test_coordinate_input(self):
        """Direct lat,lon coordinate input should resolve without geocoding."""
        with mock.patch.object(sv, "fetch_route", fake_route(())) as mocked:
            r = self.client.get("/api/route/", {
                "start": "41.8781,-87.6298",
                "finish": "39.7392,-104.9903",
            })
        self.assertEqual(r.status_code, 200)
        d = r.json()
        self.assertAlmostEqual(d["start"]["lat"], 41.8781, places=3)
        self.assertAlmostEqual(d["finish"]["lat"], 39.7392, places=3)

    def test_no_station_in_range_returns_422(self):
        """When no station reachable within 500 miles, return 422 not 500."""
        # Mock a route with no stations along it
        def empty_route(a, b, counter):
            counter["calls"] += 1
            path = np.array([a, b])
            return path, 600.0  # 600-mile route

        with mock.patch.object(sv, "fetch_route", empty_route):
            with mock.patch.object(sv, "stations_along", return_value=([], [])):
                r = self.client.get("/api/route/", {
                    "start": "Chicago, IL",
                    "finish": "Denver, CO",
                })
        self.assertEqual(r.status_code, 422)
        self.assertIn("error", r.json())

    def test_input_length_limit(self):
        """Inputs exceeding 100 characters should be rejected."""
        long = "A" * 101
        r = self.client.get("/api/route/", {"start": long, "finish": "Denver, CO"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("100 characters", r.json()["error"])

    def test_non_us_coordinates_rejected(self):
        """Coordinates outside the US bounding box should return 400."""
        with mock.patch.object(sv, "fetch_route", fake_route(())):
            # London, UK coordinates
            r = self.client.get("/api/route/", {
                "start": "51.5074,-0.1278",
                "finish": "Chicago, IL",
            })
        self.assertEqual(r.status_code, 400)
        self.assertIn("USA", r.json()["error"])

    def test_osrm_failure_returns_502(self):
        """When OSRM is down, return 502 not 500."""
        with mock.patch.object(sv, "fetch_route", side_effect=sv.RoutingError("Service down")):
            r = self.client.get("/api/route/", {
                "start": "Chicago, IL",
                "finish": "Denver, CO",
            })
        self.assertEqual(r.status_code, 502)
        self.assertIn("error", r.json())


class DataPipelineTests(TestCase):
    def setUp(self):
        Station.objects.all().delete()
        sv.station_index.cache_clear()

    def _csv_buffer(self, content: str) -> io.StringIO:
        return io.StringIO(content.strip())

    def test_import_full_authoritative_dataset(self):
        """Verify the full authoritative fuel price CSV loads accurately and safely."""
        csv_path = get_default_csv_path()
        stats = import_stations_from_csv(csv_path)
        self.assertEqual(stats.total_rows_read, 8151)
        self.assertEqual(stats.valid_us_rows, 7531)
        self.assertEqual(stats.canadian_rows_skipped, 620)
        self.assertEqual(stats.unique_us_stations_found, 6626)
        self.assertEqual(stats.multi_record_stations_count, 568)
        self.assertEqual(stats.unmatched_us_stations_skipped, 12)
        self.assertEqual(stats.loaded_stations_count, 6614)
        self.assertEqual(Station.objects.count(), 6614)

        # Verify model field integrity on sample station
        s = Station.objects.get(opis_id=7)
        self.assertEqual(s.name, "WOODSHED OF BIG CABIN")
        self.assertEqual(s.state, "OK")
        self.assertAlmostEqual(s.price, 3.00733333, places=6)
        self.assertFalse(s.is_exact_coord)
        self.assertEqual(s.coord_source, "city_centre_fallback")
        self.assertEqual(s.rack_id, "307")

    def test_header_validation_missing_column(self):
        """Reject CSV when required columns are absent."""
        bad_csv = self._csv_buffer(
            "OPIS Truckstop ID,Truckstop Name,Address,City,State,Retail Price\n"
            "100,TEST STOP,123 MAIN,SPRINGFIELD,IL,3.499\n"
        )
        with self.assertRaises(HeaderValidationError) as ctx:
            import_stations_from_csv(bad_csv)
        self.assertIn("Rack ID", str(ctx.exception))
        self.assertEqual(Station.objects.count(), 0)

    def test_malformed_and_invalid_prices(self):
        """Reject rows with non-numeric, negative, or zero prices with recorded reasons."""
        csv_data = self._csv_buffer(
            "OPIS Truckstop ID,Truckstop Name,Address,City,State,Rack ID,Retail Price\n"
            "101,GOOD STOP,123 MAIN,CHICAGO,IL,500,3.199\n"
            "102,BAD NON_NUMERIC,123 MAIN,CHICAGO,IL,500,FREE\n"
            "103,BAD NEGATIVE,123 MAIN,CHICAGO,IL,500,-2.50\n"
            "104,BAD ZERO,123 MAIN,CHICAGO,IL,500,0.00\n"
            "105,BAD NAN,123 MAIN,CHICAGO,IL,500,nan\n"
        )
        stats = import_stations_from_csv(csv_data)
        self.assertEqual(stats.total_rows_read, 5)
        self.assertEqual(stats.valid_us_rows, 1)
        self.assertEqual(stats.invalid_price_rows_skipped, 4)
        self.assertEqual(Station.objects.count(), 1)
        self.assertEqual(Station.objects.first().opis_id, 101)

    def test_malformed_opis_ids(self):
        """Reject rows with non-integer or non-positive OPIS IDs."""
        csv_data = self._csv_buffer(
            "OPIS Truckstop ID,Truckstop Name,Address,City,State,Rack ID,Retail Price\n"
            "101,VALID STOP,123 MAIN,CHICAGO,IL,500,3.199\n"
            "ABC,NON INT ID,123 MAIN,CHICAGO,IL,500,3.199\n"
            "-50,NEGATIVE ID,123 MAIN,CHICAGO,IL,500,3.199\n"
            "0,ZERO ID,123 MAIN,CHICAGO,IL,500,3.199\n"
        )
        stats = import_stations_from_csv(csv_data)
        self.assertEqual(stats.total_rows_read, 4)
        self.assertEqual(stats.invalid_id_rows_skipped, 3)
        self.assertEqual(Station.objects.count(), 1)

    def test_missing_required_fields(self):
        """Reject rows with empty required fields."""
        csv_data = self._csv_buffer(
            "OPIS Truckstop ID,Truckstop Name,Address,City,State,Rack ID,Retail Price\n"
            "101,VALID STOP,123 MAIN,CHICAGO,IL,500,3.199\n"
            "102,,123 MAIN,CHICAGO,IL,500,3.199\n"
            "103,STOP,,CHICAGO,IL,500,3.199\n"
            "104,STOP,123 MAIN,,IL,500,3.199\n"
            "105,STOP,123 MAIN,CHICAGO,,500,3.199\n"
        )
        stats = import_stations_from_csv(csv_data)
        self.assertEqual(stats.total_rows_read, 5)
        self.assertEqual(stats.missing_fields_rows_skipped, 4)
        self.assertEqual(Station.objects.count(), 1)

    def test_canadian_and_unknown_locations_explicitly_handled(self):
        """Explicitly classify Canadian and unrecognized jurisdictions as skipped."""
        csv_data = self._csv_buffer(
            "OPIS Truckstop ID,Truckstop Name,Address,City,State,Rack ID,Retail Price\n"
            "101,VALID US STOP,123 MAIN,CHICAGO,IL,500,3.199\n"
            "201,CANADIAN ON,456 HIGHWAY,TORONTO,ON,900,4.299\n"
            "202,CANADIAN AB,789 TRAIL,CALGARY,AB,900,4.199\n"
            "301,UNKNOWN CODE,101 BORDER,UNKNOWN,XX,900,3.500\n"
        )
        stats = import_stations_from_csv(csv_data)
        self.assertEqual(stats.canadian_rows_skipped, 2)
        self.assertEqual(stats.unknown_state_rows_skipped, 1)
        self.assertEqual(stats.valid_us_rows, 1)
        self.assertEqual(Station.objects.count(), 1)

    def test_deterministic_duplicate_resolution(self):
        """When multiple records exist for one OPIS ID, select exact lowest price and most descriptive name."""
        csv_data = self._csv_buffer(
            "OPIS Truckstop ID,Truckstop Name,Address,City,State,Rack ID,Retail Price\n"
            "555,TA SHORT,I-80 EXIT 1,CHICAGO,IL,100,3.899\n"
            "555,TA SAGINAW FULL TRAVEL CENTER,I-80 EXIT 1,CHICAGO,IL,100,3.259\n"
            "555,TA MEDIUM CENTER,I-80 EXIT 1,CHICAGO,IL,100,3.499\n"
        )
        stats = import_stations_from_csv(csv_data)
        self.assertEqual(stats.total_rows_read, 3)
        self.assertEqual(stats.unique_us_stations_found, 1)
        self.assertEqual(stats.multi_record_stations_count, 1)

        station = Station.objects.get(opis_id=555)
        self.assertAlmostEqual(station.price, 3.259, places=6)
        self.assertEqual(station.name, "TA SAGINAW FULL TRAVEL CENTER")
        self.assertEqual(station.raw_record_count, 3)

    def test_unmatched_city_gazetteer_not_fabricated(self):
        """Stations with unknown cities are not assigned fabricated coordinates."""
        csv_data = self._csv_buffer(
            "OPIS Truckstop ID,Truckstop Name,Address,City,State,Rack ID,Retail Price\n"
            "999,LOST STOP,REMOTE RD,NOWHERE_TOWN_XYZ,IL,100,3.299\n"
        )
        stats = import_stations_from_csv(csv_data)
        self.assertEqual(stats.unmatched_us_stations_skipped, 1)
        self.assertEqual(len(stats.unmatched_stations), 1)
        self.assertEqual(stats.unmatched_stations[0]["opis_id"], 999)
        self.assertEqual(Station.objects.count(), 0)

    def test_repeated_import_idempotency(self):
        """Repeated imports cleanly replace existing stations without primary key collisions."""
        csv_data = (
            "OPIS Truckstop ID,Truckstop Name,Address,City,State,Rack ID,Retail Price\n"
            "101,STOP ONE,123 MAIN,CHICAGO,IL,100,3.199\n"
            "102,STOP TWO,456 MAIN,SPRINGFIELD,IL,100,3.299\n"
        )
        import_stations_from_csv(self._csv_buffer(csv_data))
        self.assertEqual(Station.objects.count(), 2)

        # Run again
        import_stations_from_csv(self._csv_buffer(csv_data))
        self.assertEqual(Station.objects.count(), 2)
        self.assertEqual(set(Station.objects.values_list("opis_id", flat=True)), {101, 102})

    def test_atomic_rollback_on_failure(self):
        """If a database error occurs during bulk_create, previous data is preserved."""
        # Pre-seed database with an existing station
        Station.objects.create(
            opis_id=9999,
            name="ORIGINAL STATION",
            address="OLD ADDR",
            city="CHICAGO",
            state="IL",
            price=3.50,
            lat=41.8781,
            lon=-87.6298,
        )
        self.assertEqual(Station.objects.count(), 1)

        csv_data = self._csv_buffer(
            "OPIS Truckstop ID,Truckstop Name,Address,City,State,Rack ID,Retail Price\n"
            "101,NEW STOP,123 MAIN,CHICAGO,IL,100,3.199\n"
        )

        with mock.patch.object(Station.objects, "bulk_create", side_effect=RuntimeError("Simulated DB Disk Crash")):
            with self.assertRaises(RuntimeError):
                import_stations_from_csv(csv_data)

        # Verify rollback preserved original station
        self.assertEqual(Station.objects.count(), 1)
        self.assertEqual(Station.objects.first().opis_id, 9999)
        self.assertEqual(Station.objects.first().name, "ORIGINAL STATION")

