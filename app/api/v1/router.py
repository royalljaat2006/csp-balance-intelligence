"""
API v1 — PRD §9, docs/API-VERSIONING.md.

Mounted at /api/v1/ (config/urls.py). This module is the only place that
assembles the NinjaAPI instance + its error handlers + its resource
routers — individual resource files (csps.py, balances.py, ...) never
import each other.
"""

from django.conf import settings
from ninja import NinjaAPI
from ninja.throttling import AnonRateThrottle, AuthRateThrottle

from api import errors

from .balances import router as balances_router
from .comparisons import network_router as comparisons_network_router
from .comparisons import router as comparisons_router
from .csps import router as csps_router
from .incentives import router as incentives_router
from .projections import router as projections_router
from .reports import router as reports_router
from .transactions import router as transactions_router

api = NinjaAPI(
    title="CSP Average Balance Tracker API",
    version="1.0.0",
    urls_namespace="v1",
    description=(
        "Source-of-truth API for CSP average balance, incentive slab, and trend data. "
        "See docs/API-VERSIONING.md and docs/API-CATALOG.md."
    ),
    # Rate limiting (scalability foundation, 2026-09-21) — backed by
    # CACHES["default"] (config/settings/base.py), so this is per-process
    # under LocMemCache (dev/test) and shared across every replica once
    # Redis is configured in production. AuthRateThrottle keys on the
    # caller's own API key (every endpoint here requires RequireScope, so
    # this is the one that matters); AnonRateThrottle bounds pre-auth-
    # rejection request floods by IP. Per-consumer overrides aren't needed
    # yet — every issued key today is an internal Eko consumer — so a
    # single env-tunable ceiling per class is enough for now.
    throttle=[
        AuthRateThrottle(rate=settings.API_AUTH_THROTTLE_RATE),
        AnonRateThrottle(rate=settings.API_ANON_THROTTLE_RATE),
    ],
)

errors.register(api)

api.add_router("/csps", csps_router)
api.add_router("/csps", balances_router)
api.add_router("/csps", incentives_router)
api.add_router("/csps", projections_router)
api.add_router("/csps", transactions_router)
api.add_router("/csps", comparisons_router)
api.add_router("/reports", reports_router)
api.add_router("/comparisons", comparisons_network_router)
