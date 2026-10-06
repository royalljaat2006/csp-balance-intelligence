"""Local development only. Never used in the Docker image (see production.py)."""

from .base import *  # noqa: F403
from .base import env

DEBUG = True
ALLOWED_HOSTS = env.list("DJANGO_ALLOWED_HOSTS", default=["127.0.0.1", "localhost"])

# Relaxed for local HTTP dev — production.py locks these down.
SESSION_COOKIE_SECURE = False
CSRF_COOKIE_SECURE = False
SECURE_SSL_REDIRECT = False
