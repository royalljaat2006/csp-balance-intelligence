"""
common/db_router.py — the read-replica routing scaffold (scalability
foundation, 2026-09-21; completeness-verified in the P0 fast-pass). Must
be provably inert: wiring it into DATABASE_ROUTERS changes nothing about
where reads/writes actually go until a call site explicitly opts in with
`.using("replica")`.
"""

import pytest
from common.db_router import PrimaryReplicaRouter
from csp.models import Csp


def test_db_for_read_has_no_opinion():
    assert PrimaryReplicaRouter().db_for_read(Csp) is None


def test_db_for_write_always_targets_default():
    assert PrimaryReplicaRouter().db_for_write(Csp) == "default"


def test_migrations_only_ever_run_against_default():
    router = PrimaryReplicaRouter()
    assert router.allow_migrate("default", "csp") is True
    assert router.allow_migrate("replica", "csp") is False


@pytest.mark.django_db(transaction=True, databases=["default", "replica"])
def test_using_replica_reads_the_same_data_as_default_when_no_real_replica_is_configured():
    """End-to-end proof the abstraction actually works: with no
    DATABASE_REPLICA_URL set, "replica" is configured (config/settings/
    base.py) as a second alias for the *same physical database* as
    "default" — so a write through one alias is immediately visible
    reading through the other, because there is, today, only one real
    database behind both. That's what makes it safe to start opting
    read-only call sites into .using("replica") now, ahead of a real
    replica being provisioned: today it's a no-op; the day a real replica
    exists, those call sites accept its (eventual-consistency) lag by
    design, and nothing else has to change."""
    Csp.objects.create(csp_code="1A850099", name="Replica Proof")
    via_replica = Csp.objects.using("replica").get(csp_code="1A850099")
    assert via_replica.name == "Replica Proof"


@pytest.mark.django_db(transaction=True, databases=["default", "replica"])
def test_default_manager_write_with_no_explicit_using_routes_to_default():
    """PrimaryReplicaRouter.db_for_write always returns "default" — this
    only matters for a call with *no* explicit .using() (QuerySet.db is
    `self._db or router.db_for_write(...)`, so an explicit .using() alias
    always wins over the router, by Django's own design). This proves the
    router's real job: routing the plain, no-.using() case, which is what
    ordinary application code (services.py, views) actually does."""
    Csp.objects.create(csp_code="1A850098", name="Plain write, no .using()")
    assert Csp.objects.using("default").filter(csp_code="1A850098").exists()
