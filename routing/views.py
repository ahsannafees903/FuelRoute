import json
import logging
from urllib.parse import urlencode

from django.conf import settings
from django.core.cache import cache
from django.http import JsonResponse
from django.shortcuts import render, redirect
from django.views.decorators.http import require_http_methods

from . import services as sv

logger = logging.getLogger(__name__)

STATUS = {sv.LocationError: 400, sv.NoStationInRange: 422, sv.RoutingError: 502}
MAX_BODY_BYTES = 32 * 1024  # 32 KB threshold for JSON payloads


def _check_rate_limit(request) -> bool:
    """Token-free sliding window rate limiter backed by Django cache."""
    if not getattr(settings, "RATE_LIMIT_ENABLED", True):
        return True
    # In test runner environments, bypass rate limiting
    if getattr(settings, "TESTING", False) or getattr(settings, "is_running_tests", False):
        return True

    forwarded = request.META.get("HTTP_X_FORWARDED_FOR")
    if forwarded:
        ip = forwarded.split(",")[0].strip()
    else:
        ip = request.META.get("REMOTE_ADDR", "unknown")

    window = getattr(settings, "RATE_LIMIT_WINDOW_SECONDS", 60)
    limit = getattr(settings, "RATE_LIMIT_MAX_REQUESTS", 60)
    cache_key = f"rl:{ip}"

    try:
        count = cache.get(cache_key, 0)
        if count >= limit:
            return False
        cache.set(cache_key, count + 1, timeout=window)
    except Exception:
        # If cache fails, fail open to avoid service disruption
        logger.warning("Cache access failed during rate limiting check", exc_info=True)
    return True


def _inputs(request):
    if request.method == "POST" and request.content_type == "application/json":
        if len(request.body) > MAX_BODY_BYTES:
            raise sv.LocationError("Request body exceeds maximum allowed size (32KB).")
        try:
            data = json.loads(request.body or b"{}")
        except json.JSONDecodeError:
            raise sv.LocationError("Body must be valid JSON.")
        if not isinstance(data, dict):
            raise sv.LocationError("Body must be a JSON object.")
    else:
        data = request.POST if request.method == "POST" else request.GET

    s = str(data.get("start", "")).strip()
    f = str(data.get("finish", "")).strip()

    if not s or not f:
        raise sv.LocationError("Both 'start' and 'finish' are required.")
    if len(s) > 100 or len(f) > 100:
        raise sv.LocationError("'start' and 'finish' are limited to 100 characters.")
    if s.lower() == f.lower():
        raise sv.LocationError("Start and finish locations must be different.")
    return s, f


def _run(request):
    s, f = _inputs(request)
    data = sv.build_plan(s, f)
    data["map_url"] = request.build_absolute_uri("/api/route/map/") + "?" + urlencode({"start": s, "finish": f})
    return data


@require_http_methods(["GET", "POST"])
def route_api(request):
    """API endpoint to calculate optimal fuel stops along a driving route.
    Public route optimization endpoint protected against abuse via sliding-window
    IP rate limiting and strict request memory thresholds."""
    if not _check_rate_limit(request):
        return JsonResponse(
            {"error": "Rate limit exceeded. Please wait a minute before sending additional requests."},
            status=429,
        )

    try:
        return JsonResponse(_run(request))
    except tuple(STATUS) as e:
        return JsonResponse({"error": str(e)}, status=STATUS[type(e)])
    except Exception as e:
        logger.exception("Unexpected server error in route_api")
        return JsonResponse(
            {"error": "An unexpected server error occurred while processing the route plan."},
            status=500,
        )


@require_http_methods(["GET"])
def map_page(request):
    """Render the interactive map. If start/finish are given, pre-load the plan;
    otherwise render the empty search state."""
    s = request.GET.get("start", "").strip()
    f = request.GET.get("finish", "").strip()
    if s and f:
        try:
            return render(request, "routing/map.html", {"data": _run(request)})
        except tuple(STATUS) as e:
            return render(request, "routing/map.html", {"data": None, "error": str(e)})
        except Exception:
            logger.exception("Unexpected error in map_page")
            return render(request, "routing/map.html", {"data": None, "error": "An unexpected server error occurred."})
    return render(request, "routing/map.html", {"data": None})


def home(request):
    """Redirect root URL to the interactive map page."""
    return redirect("/api/route/map/")

