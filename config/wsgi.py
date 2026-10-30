import os
from django.core.wsgi import get_wsgi_application

os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
application = get_wsgi_application()

