"""
Per-consumer API credentials — Phase 8.

Replaces the old flat API_KEYS_READ/API_KEYS_ADMIN env-var lists with a real
consumer identity: each `ApiConsumer` (a dashboard, a reporting system, a
mobile app, ...) gets one or more `ApiKey` rows, each scoped to only the
resources that consumer needs. Keys are never stored in plaintext — only a
salted hash (see `api/security.py`) — so a DB read alone can't leak a usable
credential, and rotation/revocation are just row edits, not redeploys.
"""

from __future__ import annotations

from django.db import models

SCOPE_CHOICES = [
    ("csp:read", "csp:read"),
    ("balance:read", "balance:read"),
    ("incentive:read", "incentive:read"),
    ("projection:read", "projection:read"),
    ("transaction:read", "transaction:read"),
    ("report:read", "report:read"),
    ("comparison:read", "comparison:read"),
]
ALL_SCOPES = [value for value, _ in SCOPE_CHOICES]


class ApiConsumer(models.Model):
    """One row per calling application (a dashboard, a reporting tool, ...)."""

    name = models.CharField(max_length=100, unique=True)
    description = models.CharField(max_length=255, blank=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name


class ApiKey(models.Model):
    """
    One issued credential. `key_hash` is the only representation ever stored
    — the raw key is shown exactly once, at creation time (see
    `manage.py create_api_key`), and never again, including in the admin.
    """

    consumer = models.ForeignKey(ApiConsumer, on_delete=models.CASCADE, related_name="keys")
    label = models.CharField(max_length=100, blank=True, help_text="e.g. 'prod', 'staging'")
    key_prefix = models.CharField(
        max_length=12, help_text="First few characters of the raw key, for identification only."
    )
    key_hash = models.CharField(max_length=128, unique=True)
    scopes = models.CharField(
        max_length=255,
        help_text="Comma-separated, from: " + ", ".join(ALL_SCOPES),
    )
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    last_used_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [models.Index(fields=["key_hash"])]
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.consumer.name}:{self.label or self.key_prefix}"

    def scope_list(self) -> list[str]:
        return [s.strip() for s in self.scopes.split(",") if s.strip()]

    def has_scope(self, scope: str) -> bool:
        return scope in self.scope_list()
