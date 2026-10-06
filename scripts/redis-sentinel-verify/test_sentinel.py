"""
Real end-to-end proof that config/settings/production.py's Redis Sentinel
branch actually works — not just "the Django config loads without an
exception". Run from *inside* this project's own image (so the exact
installed django-redis/redis-py versions are exercised), attached to the
throwaway Sentinel cluster this directory's docker-compose.yml brings up.

Usage (from the repo root):
    cd scripts/redis-sentinel-verify
    docker compose -p sentinel-verify up -d
    # wait a few seconds for the cluster to elect/settle
    docker run --rm --network sentinel-verify_sentinel-net \
        --entrypoint python \
        -v "$(pwd)/test_sentinel.py:/tmp/test_sentinel.py:ro" \
        csp-balance-tracker:latest /tmp/test_sentinel.py
    docker compose -p sentinel-verify down -v   # tear down when done

Expected output: "SUCCESS: django-redis SentinelClient read/write works
against a real Sentinel cluster."

This exact run (2026-10-06) is what caught two real bugs in production.py's
Sentinel branch before they could reach a real deployment — see that
file's comment for what they were and how they were fixed.
"""

import django
from django.conf import settings

settings.configure(
    CACHES={
        "default": {
            "BACKEND": "django_redis.cache.RedisCache",
            "LOCATION": "redis://mymaster/0",  # hostname part = Sentinel service/master name
            "OPTIONS": {
                "CLIENT_CLASS": "django_redis.client.SentinelClient",
                "CONNECTION_FACTORY": "django_redis.pool.SentinelConnectionFactory",
                "SENTINELS": [("sentinel-1", 26379), ("sentinel-2", 26379), ("sentinel-3", 26379)],
                "CONNECTION_POOL_CLASS": "redis.sentinel.SentinelConnectionPool",
                "SOCKET_CONNECT_TIMEOUT": 2,
                "SOCKET_TIMEOUT": 2,
            },
        }
    }
)
django.setup()

from django.core.cache import cache  # noqa: E402

cache.set("sentinel-verify-key", "hello-from-django-redis-sentinel", timeout=30)
value = cache.get("sentinel-verify-key")
print("GET result:", value)
assert value == "hello-from-django-redis-sentinel", "Sentinel-backed cache read/write FAILED"
print("SUCCESS: django-redis SentinelClient read/write works against a real Sentinel cluster.")
