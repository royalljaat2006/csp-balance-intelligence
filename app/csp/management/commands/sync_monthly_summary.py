"""
manage.py sync_monthly_summary — the last step of PRD FR4.

Reads the dbt-materialized `monthly_summary` mart (deterministic fields:
mtd_mab, slab, gaps, MoM, trend_7d_pct — built by `dbt run`, see
dbt/models/marts/monthly_summary.sql), computes the projection fields via
csp/projection.py, and upserts the combined row into the Django-owned
csp_monthlysummary table — the table api/v1/*.py actually reads.

Run order (see docs/DEPLOYMENT.md "Scheduled jobs"): ingest -> `dbt run` ->
this command. Safe to re-run; every write is an upsert keyed on (csp, month).
"""

from __future__ import annotations

import decimal

import structlog
from common.cache import bump_generation
from django.core.management.base import BaseCommand
from django.db import connection
from django.db.models import Q

from csp.models import HISTORICAL_ONLY_STATUS, Csp, MonthlySummary
from csp.projection import build_projection

logger = structlog.get_logger("ingestion")

_MART_QUERY = """
    select
        ms.csp_code, ms.month, ms.days_in_month, ms.days_with_data, ms.mtd_mab,
        ms.slab, ms.incentive_rate_pa, ms.gap_to_min, ms.gap_to_next_slab,
        ms.prev_month_mab, ms.mom_change_abs, ms.mom_change_pct, ms.trend_7d_pct,
        ms.is_eligible, ms.account_count,
        lb.daily_avg_balance as last_known_balance
    from monthly_summary ms
    left join lateral (
        select db.daily_avg_balance
        from csp_dailybalance db
        where db.csp_id = ms.csp_code
          and to_char(db.balance_date, 'YYYY-MM') = ms.month
        order by db.balance_date desc
        limit 1
    ) lb on true
"""

_UPDATE_FIELDS = [
    "days_in_month",
    "days_with_data",
    "mtd_mab",
    "slab",
    "incentive_rate_pa",
    "gap_to_min",
    "gap_to_next_slab",
    "prev_month_mab",
    "mom_change_abs",
    "mom_change_pct",
    "trend_7d_pct",
    "is_eligible",
    "projected_mab",
    "projected_slab",
    "projected_incentive_annual",
    "trend_flag",
]


class Command(BaseCommand):
    help = "Sync the dbt monthly_summary mart + projection into csp_monthlysummary."

    def handle(self, *args, **options):
        logger.info("started", stage="PROJECTION")

        with connection.cursor() as cursor:
            cursor.execute(_MART_QUERY)
            columns = [c[0] for c in cursor.description]
            rows = [dict(zip(columns, row, strict=True)) for row in cursor.fetchall()]

        if not rows:
            logger.warning(
                "no_mart_rows",
                stage="PROJECTION",
                reason="monthly_summary mart is empty — run dbt run first",
            )
            self.stdout.write("No rows in the dbt monthly_summary mart — nothing to sync.")
            return

        # Every csp_code the mart references must exist in Django's Csp table
        # (it always will if ingest_transactions/ingest_calling_sheet ran
        # first) — bulk_create(ignore_conflicts=True) is a no-op otherwise.
        # status="historical_only" keeps a stub created only to satisfy this
        # FK out of "CSPs tracked" (see csp/services.get_overview).
        Csp.objects.bulk_create(
            [Csp(csp_code=r["csp_code"], status=HISTORICAL_ONLY_STATUS) for r in rows],
            ignore_conflicts=True,
        )

        objs = []
        for r in rows:
            projection = build_projection(
                mtd_mab=r["mtd_mab"],
                days_with_data=r["days_with_data"],
                days_in_month=r["days_in_month"],
                last_known_balance=r["last_known_balance"] or decimal.Decimal("0"),
                trend_7d_pct=r["trend_7d_pct"],
                account_count=r["account_count"],
            )
            objs.append(
                MonthlySummary(
                    csp_id=r["csp_code"],
                    month=r["month"],
                    days_in_month=r["days_in_month"],
                    days_with_data=r["days_with_data"],
                    mtd_mab=r["mtd_mab"],
                    slab=r["slab"],
                    incentive_rate_pa=r["incentive_rate_pa"],
                    gap_to_min=r["gap_to_min"],
                    gap_to_next_slab=r["gap_to_next_slab"],
                    prev_month_mab=r["prev_month_mab"],
                    mom_change_abs=r["mom_change_abs"],
                    mom_change_pct=r["mom_change_pct"],
                    trend_7d_pct=r["trend_7d_pct"],
                    is_eligible=r["is_eligible"],
                    **projection,
                )
            )

        MonthlySummary.objects.bulk_create(
            objs,
            update_conflicts=True,
            unique_fields=["csp", "month"],
            update_fields=_UPDATE_FIELDS,
            batch_size=1000,
        )

        # A real sync, not just an upsert: a (csp, month) Django still has a
        # row for but the dbt mart no longer does -- because its source
        # DailyBalance data was removed (a historical backfill undone, bad
        # data cleaned up, ...) and dbt has already been re-run -- must be
        # deleted here too, or it lingers forever showing stale numbers for
        # data that no longer exists anywhere upstream. Skipped entirely
        # when the mart came back empty (see the early return above) so a
        # transient dbt failure can never look like "everyone's gone" and
        # wipe the whole table.
        current_keys = {(r["csp_code"], r["month"]) for r in rows}
        existing_keys = set(MonthlySummary.objects.values_list("csp_id", "month"))
        stale_keys = existing_keys - current_keys
        stale_deleted = 0
        if stale_keys:
            stale_filter = Q()
            for csp_code, month in stale_keys:
                stale_filter |= Q(csp_id=csp_code, month=month)
            stale_deleted, _ = MonthlySummary.objects.filter(stale_filter).delete()
            logger.info(
                "stale_rows_removed", stage="PROJECTION", count=stale_deleted,
                keys=sorted(stale_keys)[:20],  # cap -- this is a log line, not a dump
            )

        bump_generation()  # MonthlySummary changed -> invalidate cached Overview/Trends rollups
        logger.info(
            "finished", stage="PROJECTION", rows_synced=len(objs), stale_removed=stale_deleted
        )
        self.stdout.write(
            self.style.SUCCESS(
                f"Synced {len(objs)} monthly_summary row(s), removed {stale_deleted} stale row(s)."
            )
        )
