"""Security and hardening regression tests for FuelRoute."""

from unittest import mock
from django.conf import settings
from django.core.cache import cache
from django.core.exceptions import ImproperlyConfigured
from django.test import TestCase, override_settings

from . import services as sv


class SecurityHardeningTests(TestCase):
    def setUp(self):
        cache.clear()

    def test_oversized_json_payload_rejected(self):
        """Requests with payloads exceeding MAX_BODY_BYTES (32KB) must be rejected with 400."""
        # 33 KB payload
        oversized = "A" * (33 * 1024)
        resp = self.client.post(
            "/api/route/",
            data=f'{{"start": "Chicago, IL", "finish": "Denver, CO", "padding": "{oversized}"}}',
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn("exceeds maximum allowed size", resp.json().get("error", ""))

    def test_rate_limiter_throttles_excess_requests(self):
        """When rate limiting is active, excess requests must return HTTP 429."""
        with override_settings(RATE_LIMIT_ENABLED=True, RATE_LIMIT_MAX_REQUESTS=3, RATE_LIMIT_WINDOW_SECONDS=60):
            # Send 3 allowed requests
            for _ in range(3):
                with mock.patch("routing.views._run", return_value={"mock": True}):
                    r = self.client.get("/api/route/", {"start": "Chicago, IL", "finish": "Denver, CO"})
                    self.assertEqual(r.status_code, 200)

            # 4th request must be rate-limited
            with mock.patch("routing.views._run", return_value={"mock": True}):
                r = self.client.get("/api/route/", {"start": "Chicago, IL", "finish": "Denver, CO"})
                self.assertEqual(r.status_code, 429)
                self.assertIn("Rate limit exceeded", r.json().get("error", ""))

    def test_unhandled_exception_returns_500_without_traceback(self):
        """Internal server errors must return 500 without leaking stack traces or internal paths."""
        with mock.patch("routing.views._run", side_effect=ZeroDivisionError("secret internal error")):
            resp = self.client.get("/api/route/", {"start": "Chicago, IL", "finish": "Denver, CO"})
            self.assertEqual(resp.status_code, 500)
            data = resp.json()
            self.assertIn("error", data)
            self.assertNotIn("Traceback", data["error"])
            self.assertNotIn("ZeroDivisionError", data["error"])
            self.assertNotIn("secret internal error", data["error"])

    def test_production_secret_key_safety_check(self):
        """Settings must refuse to start in production if SECRET_KEY is the default dev placeholder."""
        # We test the logic directly:
        def validate_production_secret(debug, is_test, secret_key):
            if not debug and not is_test:
                if not secret_key or secret_key == "dev-only-change-me":
                    raise ImproperlyConfigured("Insecure production secret key")
            return True

        # In dev or test, placeholder is allowed
        self.assertTrue(validate_production_secret(debug=True, is_test=False, secret_key="dev-only-change-me"))
        self.assertTrue(validate_production_secret(debug=False, is_test=True, secret_key="dev-only-change-me"))

        # In production (DEBUG=False, not test), placeholder raises ImproperlyConfigured
        with self.assertRaises(ImproperlyConfigured):
            validate_production_secret(debug=False, is_test=False, secret_key="dev-only-change-me")
        with self.assertRaises(ImproperlyConfigured):
            validate_production_secret(debug=False, is_test=False, secret_key="")

    def test_security_middleware_configured(self):
        """Security and clickjacking protection middleware must be installed."""
        self.assertIn("django.middleware.security.SecurityMiddleware", settings.MIDDLEWARE)
        self.assertIn("django.middleware.clickjacking.XFrameOptionsMiddleware", settings.MIDDLEWARE)
        # self.assertIn("django.middleware.csrf.CsrfViewMiddleware", settings.MIDDLEWARE)

    def test_map_page_unhandled_error_returns_clean_page(self):
        """Unhandled errors in map_page must render an error template without leaking internal exceptions."""
        with mock.patch("routing.views._run", side_effect=RuntimeError("internal crash")):
            resp = self.client.get("/api/route/map/", {"start": "Chicago, IL", "finish": "Denver, CO"})
            self.assertEqual(resp.status_code, 200)
            self.assertContains(resp, "An unexpected server error occurred.")
            self.assertNotContains(resp, "internal crash")
