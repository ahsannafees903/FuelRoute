import os
import sys
from pathlib import Path
from django.core.exceptions import ImproperlyConfigured

os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")

BASE_DIR = Path(__file__).resolve().parent.parent

def get_bool_env(name: str, default: bool = False) -> bool:
    """Safely parse boolean environment variable."""
    val = os.environ.get(name)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


def get_int_env(name: str, default: int) -> int:
    """Safely parse integer environment variable with defensive fallback."""
    val = os.environ.get(name)
    if not val or not val.strip().lstrip("-").isdigit():
        return default
    try:
        return int(val.strip())
    except ValueError:
        return default


def get_list_env(name: str, default: str = "") -> list[str]:
    """Parse comma-separated strings into a clean, stripped list."""
    raw = os.environ.get(name, default)
    return [item.strip() for item in raw.split(",") if item.strip()]


# Load local .env file if present
ENV_FILE = BASE_DIR / ".env"
if ENV_FILE.is_file():
    with open(ENV_FILE, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k = k.strip()
            v = v.split("#")[0].strip().strip("'\"")
            if k not in os.environ:
                os.environ[k] = v

SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "dev-only-change-me")
DEBUG = get_bool_env("DJANGO_DEBUG", default=True)

# Validate secret key when running in production
is_running_tests = "test" in sys.argv
if not DEBUG and not is_running_tests:
    if not SECRET_KEY or SECRET_KEY == "dev-only-change-me":  # nosec B105
        raise ImproperlyConfigured(
            "DJANGO_SECRET_KEY must be set to a secure, unpredictable value in production when DEBUG=False."
        )

ALLOWED_HOSTS = get_list_env("DJANGO_ALLOWED_HOSTS", default="localhost,127.0.0.1")

INSTALLED_APPS = ["routing"]
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.middleware.common.CommonMiddleware",
    # "django.middleware.csrf.CsrfViewMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]
ROOT_URLCONF = "config.urls"
TEMPLATES = [{"BACKEND": "django.template.backends.django.DjangoTemplates", "APP_DIRS": True}]
WSGI_APPLICATION = "config.wsgi.application"
DATABASES = {"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": BASE_DIR / "db.sqlite3"}}
CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
USE_TZ = True

# Security Headers & Production Hardening
if not DEBUG:
    SECURE_CONTENT_TYPE_NOSNIFF = True
    X_FRAME_OPTIONS = "DENY"
    if get_bool_env("DJANGO_SECURE_SSL_REDIRECT", default=False):
        SECURE_SSL_REDIRECT = True
    if get_bool_env("DJANGO_SESSION_COOKIE_SECURE", default=False):
        SESSION_COOKIE_SECURE = True
    if get_bool_env("DJANGO_CSRF_COOKIE_SECURE", default=False):
        CSRF_COOKIE_SECURE = True
    hsts_seconds = get_int_env("DJANGO_SECURE_HSTS_SECONDS", default=0)
    if hsts_seconds > 0:
        SECURE_HSTS_SECONDS = hsts_seconds
        SECURE_HSTS_INCLUDE_SUBDOMAINS = True
        SECURE_HSTS_PRELOAD = True

DATA_UPLOAD_MAX_MEMORY_SIZE = 262144  # 256 KB request size threshold

# Logging configuration
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "standard": {
            "format": "[%(asctime)s] %(levelname)s in %(name)s: %(message)s",
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "standard",
        },
    },
    "root": {
        "handlers": ["console"],
        "level": os.environ.get("DJANGO_LOG_LEVEL", "INFO"),
    },
}

# --- Rate limiting controls ---
RATE_LIMIT_ENABLED = get_bool_env("RATE_LIMIT_ENABLED", default=True)
RATE_LIMIT_MAX_REQUESTS = get_int_env("RATE_LIMIT_MAX_REQUESTS", default=60)
RATE_LIMIT_WINDOW_SECONDS = get_int_env("RATE_LIMIT_WINDOW_SECONDS", default=60)

# --- Fuel planner settings ---
OSRM_URL = os.environ.get("OSRM_URL", "https://router.project-osrm.org")
NOMINATIM_URL = os.environ.get("NOMINATIM_URL", "https://nominatim.openstreetmap.org/search")
NOMINATIM_USER_AGENT = os.environ.get(
    "NOMINATIM_USER_AGENT",
    "FuelRoute-Engine/1.0 (ops@fuelroute.internal)"
)
MAX_RANGE_MILES = 500        # full-tank driving range
MPG = 10                     # fuel economy (miles per gallon)
# Implied tank capacity: MAX_RANGE_MILES / MPG = 50 gallons.
CORRIDOR_MILES = 10          # how far from the road a station may be
MIN_BUY_MILES = get_int_env("MIN_BUY_MILES", default=50)  # minimum ~5 gallons (50 miles) to avoid micro-stops

