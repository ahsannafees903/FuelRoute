import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "dev-only-change-me")
DEBUG = os.environ.get("DJANGO_DEBUG", "1") == "1"
ALLOWED_HOSTS = os.environ.get("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1").split(",")

INSTALLED_APPS = ["routing"]
MIDDLEWARE = ["django.middleware.common.CommonMiddleware"]
ROOT_URLCONF = "config.urls"
TEMPLATES = [{"BACKEND": "django.template.backends.django.DjangoTemplates", "APP_DIRS": True}]
WSGI_APPLICATION = "config.wsgi.application"
DATABASES = {"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": BASE_DIR / "db.sqlite3"}}
CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
USE_TZ = True

# --- fuel planner settings ---
OSRM_URL = os.environ.get("OSRM_URL", "https://router.project-osrm.org")
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
MAX_RANGE_MILES = 500        # full-tank driving range
MPG = 10                     # fuel economy (miles per gallon)
# Implied tank capacity: MAX_RANGE_MILES / MPG = 50 gallons.
CORRIDOR_MILES = 10          # how far from the road a station may be
MIN_BUY_MILES = 0            # no minimum purchase; plan() proven optimal at 0 via DP solver
