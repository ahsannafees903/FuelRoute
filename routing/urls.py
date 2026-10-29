from django.urls import path
from . import views

urlpatterns = [
    path("route/", views.route_api),
    path("route/map/", views.map_page),
]
