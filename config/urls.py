from django.urls import include, path
from routing.views import home

urlpatterns = [
    path("", home),
    path("api/", include("routing.urls")),
]
