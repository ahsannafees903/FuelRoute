import json
from urllib.parse import urlencode

from django.http import JsonResponse
from django.shortcuts import render, redirect
from django.views.decorators.http import require_http_methods

from . import services as sv

STATUS = {sv.LocationError: 400, sv.NoStationInRange: 422, sv.RoutingError: 502}


def _inputs(request):
    if request.method == "POST" and request.content_type == "application/json":
        try:
            data = json.loads(request.body or b"{}")
        except json.JSONDecodeError:
            raise sv.LocationError("Body must be valid JSON.")
        if not isinstance(data, dict):
            raise sv.LocationError("Body must be a JSON object.")
    else:
        data = request.POST if request.method == "POST" else request.GET
    s, f = str(data.get("start", "")).strip(), str(data.get("finish", "")).strip()
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
    try:
        return JsonResponse(_run(request))
    except tuple(STATUS) as e:
        return JsonResponse({"error": str(e)}, status=STATUS[type(e)])


@require_http_methods(["GET"])
def map_page(request):
    """Render the interactive map.  If start/finish are given, pre-load the plan;
    otherwise render the empty search state."""
    s = request.GET.get("start", "").strip()
    f = request.GET.get("finish", "").strip()
    if s and f:
        try:
            return render(request, "routing/map.html", {"data": _run(request)})
        except tuple(STATUS) as e:
            return render(request, "routing/map.html", {"data": None, "error": str(e)})
    return render(request, "routing/map.html", {"data": None})


def home(request):
    """Redirect root URL to the map page."""
    return redirect("/api/route/map/")
