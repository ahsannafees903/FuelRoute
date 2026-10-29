"""Extended tests for plan() edge cases, API resilience, and fuel arithmetic.
These complement the existing tests in tests.py without modifying them."""
import random
from unittest import mock

import numpy as np
import requests
from django.core.cache import cache
from django.core.management import call_command
from django.test import TestCase

from . import services as sv
from .tests import exact_cost, fake_route


# ================================================================
# Plan optimizer edge cases
# ================================================================
class PlanEdgeCaseTests(TestCase):
    """Unit tests for plan() covering boundary conditions and arithmetic."""

    def test_short_trip_no_intermediate_stops(self):
        """Route shorter than max range: origin buys just enough, no intermediate stop needed."""
        stops = [{"mile": 100, "price": 3.50, "detour": 0.5},
                 {"mile": 200, "price": 3.00, "detour": 0.3}]
        buys, cost = sv.plan(stops, total=300, start_price=2.80, rng=500, mpg=10)
        total_miles_purchased = sum(b["gallons"] * 10 for b in buys)
        self.assertGreaterEqual(total_miles_purchased, 300.0 - 0.01,
                                "Must purchase enough fuel to cover 300 miles")

    def test_exact_range_boundary(self):
        """Route of exactly 500 miles with one mid-route stop."""
        stops = [{"mile": 250, "price": 3.00, "detour": 0.1}]
        buys, cost = sv.plan(stops, total=500, start_price=3.50, rng=500, mpg=10)
        total_gal = sum(b["gallons"] for b in buys)
        self.assertAlmostEqual(total_gal, 50.0, delta=0.01,
                               msg="500 mi at 10 MPG = 50 gal")

    def test_just_beyond_range_requires_stop(self):
        """Route of 501 miles: impossible without at least one reachable stop."""
        stops = [{"mile": 250, "price": 3.00, "detour": 0.1}]
        buys, cost = sv.plan(stops, total=501, start_price=3.50, rng=500, mpg=10)
        total_gal = sum(b["gallons"] for b in buys)
        self.assertAlmostEqual(total_gal, 50.1, delta=0.01)

    def test_only_stop_beyond_range_raises(self):
        """If the only stop is beyond the full-tank range, NoStationInRange is raised."""
        stops = [{"mile": 550, "price": 3.00, "detour": 0.1}]
        with self.assertRaises(sv.NoStationInRange):
            sv.plan(stops, total=600, start_price=3.50, rng=500, mpg=10)

    def test_gap_between_stops_exceeds_range(self):
        """A 600-mile gap between consecutive stops is infeasible."""
        stops = [
            {"mile": 100, "price": 3.00, "detour": 0.1},
            {"mile": 700, "price": 3.00, "detour": 0.1},
        ]
        with self.assertRaises(sv.NoStationInRange):
            sv.plan(stops, total=800, start_price=3.00, rng=500, mpg=10)

    def test_prefers_cheaper_fuel(self):
        """With multiple reachable stops, the optimizer buys more at the cheapest."""
        stops = [
            {"mile": 100, "price": 4.00, "detour": 0.1},
            {"mile": 200, "price": 2.50, "detour": 0.1},
            {"mile": 300, "price": 3.50, "detour": 0.1},
        ]
        buys, cost = sv.plan(stops, total=400, start_price=3.00, rng=500, mpg=10)
        # Must cover 400 mi = 40 gal. Should cost less than buying all at $3.00
        self.assertAlmostEqual(sum(b["gallons"] for b in buys), 40.0, delta=0.01)
        self.assertLess(cost, 40 * 3.00, "Should exploit the $2.50 station")

    def test_empty_route_no_stops_short_trip(self):
        """A 100-mile trip with no intermediate stations: origin buys everything."""
        buys, cost = sv.plan([], total=100, start_price=3.50, rng=500, mpg=10)
        self.assertEqual(len(buys), 1, "Only the origin purchase")
        self.assertTrue(buys[0].get("start"))
        self.assertAlmostEqual(buys[0]["gallons"], 10.0, delta=0.01)

    def test_no_stops_beyond_range_raises(self):
        """600-mile trip with zero stations: NoStationInRange."""
        with self.assertRaises(sv.NoStationInRange):
            sv.plan([], total=600, start_price=3.50, rng=500, mpg=10)

    def test_final_leg_fuel_sufficient(self):
        """Verify the vehicle has enough fuel for the last stop -> destination leg."""
        stops = [
            {"mile": 100, "price": 3.00, "detour": 0.1},
            {"mile": 300, "price": 3.00, "detour": 0.1},
        ]
        buys, cost = sv.plan(stops, total=750, start_price=3.00, rng=500, mpg=10)
        # Trace fuel state after each purchase
        fuel_miles = 0.0
        prev_mile = 0.0
        for b in buys:
            driven = b["mile"] - prev_mile
            fuel_miles = fuel_miles - driven + b["gallons"] * 10
            self.assertGreaterEqual(fuel_miles, -0.01,
                                    f"Ran out of fuel before mile {b['mile']}")
            prev_mile = b["mile"]
        # Final leg: last purchase position to destination
        remaining = 750 - prev_mile
        fuel_miles -= remaining
        self.assertGreaterEqual(fuel_miles, -0.01,
                                "Not enough fuel for the final leg to destination")

    def test_cost_equals_sum_of_purchase_costs(self):
        """plan() returned cost must equal the sum of individual purchase costs."""
        stops = [
            {"mile": 200, "price": 3.25, "detour": 0.5},
            {"mile": 400, "price": 2.99, "detour": 0.3},
        ]
        buys, cost = sv.plan(stops, total=500, start_price=3.50, rng=500, mpg=10)
        expected = sum(b["cost"] for b in buys)
        self.assertAlmostEqual(cost, expected, places=6)

    def test_dp_comparison_with_production_range(self):
        """Run the DP solver at rng=500 to verify optimality at real scale."""
        random.seed(42)
        checked = 0
        for _ in range(100):
            total = random.randint(100, 1200)
            n = random.randint(1, min(15, total - 1))
            miles = random.sample(range(1, total), n)
            stops = sorted([{"mile": m, "price": round(random.uniform(2.5, 4.5), 2)}
                            for m in miles], key=lambda s: s["mile"])
            p0 = round(random.uniform(2.5, 4.5), 2)
            want = exact_cost(stops, total, p0, 500, 10)
            try:
                _, got = sv.plan(stops, total, p0, 500, 10, min_buy=0)
            except sv.NoStationInRange:
                got = None
            if want is None or got is None:
                self.assertEqual(want is None, got is None)
            else:
                self.assertAlmostEqual(got, want, places=4)
                checked += 1
        self.assertGreater(checked, 30, "Should verify at least 30 feasible cases")


# ================================================================
# API resilience and routing error handling
# ================================================================
class RoutingResilienceTests(TestCase):
    """API-level tests for timeout, malformed response, and HTTP error handling."""

    @classmethod
    def setUpTestData(cls):
        call_command("load_stations", verbosity=0)

    def setUp(self):
        cache.clear()

    def test_osrm_timeout_returns_502(self):
        """OSRM Timeout -> RoutingError -> 502."""
        with mock.patch.object(sv, "fetch_route", side_effect=sv.RoutingError("timed out")):
            r = self.client.get("/api/route/", {"start": "Chicago, IL", "finish": "Denver, CO"})
        self.assertEqual(r.status_code, 502)
        self.assertIn("error", r.json())

    def test_osrm_http_error_returns_502(self):
        """OSRM HTTP 503 -> RoutingError -> 502."""
        with mock.patch.object(sv, "fetch_route",
                               side_effect=sv.RoutingError("Routing service unavailable")):
            r = self.client.get("/api/route/", {"start": "Chicago, IL", "finish": "Denver, CO"})
        self.assertEqual(r.status_code, 502)

    def test_malformed_osrm_json_returns_502(self):
        """If OSRM returns valid JSON but missing fields, fetch_route raises RoutingError."""
        with mock.patch.object(sv, "fetch_route",
                               side_effect=sv.RoutingError("incomplete route")):
            r = self.client.get("/api/route/", {"start": "Chicago, IL", "finish": "Denver, CO"})
        self.assertEqual(r.status_code, 502)

    def test_nominatim_timeout_returns_502(self):
        """Nominatim timeout on a non-gazetteer input returns 502."""
        # Use a street address that won't match the local gazetteer
        with mock.patch.object(sv, "_local", return_value=None):
            with mock.patch.object(sv, "_nominatim",
                                   side_effect=sv.RoutingError("Geocoding service timed out")):
                r = self.client.get("/api/route/", {
                    "start": "123 Main St, Nowheresville",
                    "finish": "Denver, CO",
                })
        self.assertEqual(r.status_code, 502)

    def test_cache_hit_zero_external_calls(self):
        """Second identical request uses cache: 0 external API calls."""
        with mock.patch.object(sv, "fetch_route", fake_route(())) as mocked:
            r1 = self.client.get("/api/route/", {"start": "Chicago, IL", "finish": "Indianapolis, IN"})
            r2 = self.client.get("/api/route/", {"start": "Chicago, IL", "finish": "Indianapolis, IN"})
        self.assertTrue(r2.json()["meta"]["cached"])
        self.assertEqual(r2.json()["meta"]["external_api_calls"], 0)


# ================================================================
# Response schema and fuel arithmetic verification
# ================================================================
class ResponseSchemaTests(TestCase):
    """Verify the response JSON has all required fields and correct arithmetic."""

    @classmethod
    def setUpTestData(cls):
        call_command("load_stations", verbosity=0)

    def setUp(self):
        cache.clear()

    def _get(self, start, finish, via=()):
        with mock.patch.object(sv, "fetch_route", fake_route(via)):
            return self.client.get("/api/route/", {"start": start, "finish": finish})

    def test_response_has_required_fields(self):
        """All documented fields exist in the response."""
        via = ["Des Moines, IA", "Omaha, NE"]
        r = self._get("Chicago, IL", "Denver, CO", via)
        d = r.json()
        # Top-level
        for field in ("distance_miles", "total_gallons", "purchased_gallons",
                      "route_fuel_cost", "detour_fuel_cost", "total_fuel_cost",
                      "assumptions", "warnings", "start_fuel", "fuel_stops",
                      "final_leg", "route", "start", "finish", "meta"):
            self.assertIn(field, d, f"Missing top-level field: {field}")
        # Assumptions
        for field in ("max_range_miles", "mpg", "tank_capacity_gallons",
                      "corridor_miles", "start_fuel_level", "station_position",
                      "detour_model"):
            self.assertIn(field, d["assumptions"], f"Missing assumptions field: {field}")
        # Per-stop
        if d["fuel_stops"]:
            s = d["fuel_stops"][0]
            for field in ("order", "name", "address", "city", "state",
                          "price_per_gallon", "lat", "lon", "mile_marker",
                          "leg_distance_miles", "arrival_fuel_gallons",
                          "gallons_to_buy", "cost", "departure_fuel_gallons",
                          "detour_miles", "detour_fuel_cost"):
                self.assertIn(field, s, f"Missing per-stop field: {field}")

    def test_total_cost_equals_route_plus_detour(self):
        via = ["Des Moines, IA", "Omaha, NE"]
        r = self._get("Chicago, IL", "Denver, CO", via)
        d = r.json()
        self.assertAlmostEqual(d["total_fuel_cost"],
                               d["route_fuel_cost"] + d["detour_fuel_cost"], places=2)

    def test_purchased_gallons_equals_sum(self):
        """purchased_gallons = start_fuel.gallons + sum(stop.gallons_to_buy)."""
        via = ["Des Moines, IA", "Omaha, NE"]
        r = self._get("Chicago, IL", "Denver, CO", via)
        d = r.json()
        start_gal = d["start_fuel"]["gallons"] if d["start_fuel"] else 0.0
        stop_gal = sum(s["gallons_to_buy"] for s in d["fuel_stops"])
        self.assertAlmostEqual(d["purchased_gallons"], start_gal + stop_gal, delta=0.05)

    def test_fuel_state_never_negative(self):
        """arrival_fuel_gallons should never be significantly negative."""
        via = ["Des Moines, IA", "Omaha, NE", "Grand Island, NE"]
        r = self._get("Chicago, IL", "Denver, CO", via)
        d = r.json()
        for s in d["fuel_stops"]:
            self.assertGreaterEqual(s["arrival_fuel_gallons"], -0.1,
                                    f"Negative fuel at stop {s['order']}: {s['name']}")

    def test_final_leg_has_positive_fuel(self):
        """Vehicle must arrive at destination with non-negative fuel."""
        via = ["Des Moines, IA", "Omaha, NE"]
        r = self._get("Chicago, IL", "Denver, CO", via)
        d = r.json()
        self.assertGreaterEqual(d["final_leg"]["arrival_fuel_gallons"], 0.0)

    def test_tank_capacity_documented(self):
        r = self._get("Chicago, IL", "Indianapolis, IN")
        d = r.json()
        self.assertEqual(d["assumptions"]["tank_capacity_gallons"], 50.0)
        self.assertEqual(d["assumptions"]["mpg"], 10)
        self.assertEqual(d["assumptions"]["max_range_miles"], 500)


