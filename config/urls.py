from django.conf import settings
from django.urls import include, path, re_path
from django.views.static import serve
from routing.views import home

urlpatterns = [
    path("", home),
    path("api/", include("routing.urls")),
    re_path(r"^docs/(?P<path>.*)$", serve, {"document_root": settings.BASE_DIR / "docs"}),
]
