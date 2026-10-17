"""
Bank-statement ingestion: discover -> hash -> parse -> normalise -> reconcile ->
(import | quarantine) -> resolve codes -> rebuild flow aggregates.

Guarantees:
  * Source files are only ever READ. Nothing is moved, renamed or deleted.
  * Idempotent: identical content (SHA-256) is recognised and counted, never
    re-imported; overlapping hourly exports only add the rows not yet stored
    (row identity = account + date + description-hash + debit + credit + running
    balance, so a repeated reference with a different amount/balance is a
    distinct row, while the same ledger row in a later export is not duplicated).
  * A file that fails validation, targets an unexpected account, or CONFLICTS
    with rows/openings already stored is QUARANTINED: it keeps a StatementFile +
    StatementReconciliation record for audit but contributes no transactions.
  * Each file is imported in ONE database transaction: any failure rolls the
    whole file back (no partial imports) and a FAILED audit record is written.
  * Concurrent importers are serialised (PostgreSQL advisory lock) and re-check
    for the same file hash / overlapping rows inside the lock.
  * Nothing here touches csp.DailyBalance or Csp master data.
"""

from __future__ import annotations

import datetime as dt
import fnmatch
import hashlib
from collections import Counter, defaultdict
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path

import structlog
from common.cache import bumps_generation
from csp.models import Csp
from django.conf import settings
from django.db import connection
from django.db import transaction as db_transaction
from django.utils import timezone

from . import flows
from .models import (
    CodeResolution,
    ImportRun,
    StatementFile,
    StatementReconciliation,
    StatementTransaction,
)
from .normalize import normalize_row
from .parser import ParsedStatement, StatementFormatError, parse_statement
from .reconcile import FAILED, reconcile
from .resolution import CodeResolver

logger = structlog.get_logger("ingestion")

#: Extensions are only a pre-filter for a watch folder; real format is sniffed from bytes.
STATEMENT_SUFFIXES = (".xls", ".xlsx", ".txt", ".csv", ".tsv")
_IGNORED_PREFIXES = (".", "~$")
BATCH = 2000
QUERY_CHUNK = 5000  # bounds IN-list size so very large statements stay cheap and safe
MAX_FILE_BYTES = 50 * 1024 * 1024
ADVISORY_LOCK_KEY = 74_500_211  # arbitrary, app-specific; serialises bankflow row writes


@dataclass
class FileOutcome:
    outcome: str  # imported | duplicate | quarantined | failed
    dates: set[dt.date] = field(default_factory=set)
    message: str = ""


def _chunks(items: Sequence, size: int = QUERY_CHUNK) -> Iterator[Sequence]:
    for i in range(0, len(items), size):
        yield items[i : i + size]


def discover_files(
    path: Path, *, recursive: bool = False, pattern: str | None = None
) -> list[Path]:
    """A single file is taken as-is. A folder yields statement-looking files only
    (by suffix; hidden/partial-download files are ignored). Subfolders are searched
    only when `recursive=True`. Discovery is READ-ONLY: nothing is created, moved or
    deleted in the folder. `pattern` is an optional file-name glob (e.g. `statement_*`)."""
    if path.is_file():
        return [path]
    entries = path.rglob("*") if recursive else path.iterdir()
    return sorted(
        p
        for p in entries
        if p.is_file()
        and not any(part.startswith(_IGNORED_PREFIXES) for part in p.relative_to(path).parts)
        and p.suffix.lower() in STATEMENT_SUFFIXES
        and (pattern is None or fnmatch.fnmatch(p.name, pattern))
    )


def variant_rule_confirmed() -> bool:
    """The trailing-character KO rule is applied ONLY if the operator both sets
    the flag AND records where the rule was confirmed. The flag alone is ignored."""
    return bool(
        getattr(settings, "BANKFLOW_CONFIRM_TRAILING_VARIANTS", False)
        and getattr(settings, "BANKFLOW_VARIANT_RULE_CONFIRMATION_REF", "").strip()
    )


def build_resolver() -> CodeResolver:
    if (
        getattr(settings, "BANKFLOW_CONFIRM_TRAILING_VARIANTS", False)
        and not variant_rule_confirmed()
    ):
        logger.warning(
            "bankflow_variant_flag_ignored",
            reason="flag set without BANKFLOW_VARIANT_RULE_CONFIRMATION_REF",
        )
    return CodeResolver(
        set(Csp.objects.values_list("csp_code", flat=True)),
        confirm_trailing_variants=variant_rule_confirmed(),
    )


def _row_key(account_suffix: str, row, desc_hash: str) -> str:
    raw = f"{account_suffix}|{row.txn_date}|{desc_hash}|{row.debit}|{row.credit}|{row.balance}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _mask(account: str) -> str:
    return ("X" * max(0, len(account) - 5)) + account[-5:] if account else ""


def _lock() -> None:
    """Serialise concurrent bankflow writers on PostgreSQL (released at commit)."""
    if connection.vendor == "postgresql":
        with connection.cursor() as cur:
            cur.execute("SELECT pg_advisory_xact_lock(%s)", [ADVISORY_LOCK_KEY])


@bumps_generation
def run_import(
    path: str | Path,
    *,
    min_age_seconds: int = 5,
    retry_quarantined: bool = False,
    now: dt.datetime | None = None,
    max_file_mb: int | None = None,
    recursive: bool = False,
    pattern: str | None = None,
) -> ImportRun:
    target = Path(path)
    run = ImportRun.objects.create(source=str(target))
    if not target.exists():
        run.error_summary = f"Path does not exist or is not readable: {target}"
        run.finished_at = timezone.now()
        run.save()
        return run

    resolver = build_resolver()
    now = now or timezone.now()
    affected_dates: set[dt.date] = set()
    errors: list[str] = []

    for file_path in discover_files(target, recursive=recursive, pattern=pattern):
        run.files_seen += 1
        try:
            age = now.timestamp() - file_path.stat().st_mtime
            if age < min_age_seconds:
                run.files_skipped += 1  # possibly still being written
                _save_counters(run)
                continue
            res = ingest_file(
                file_path,
                run,
                resolver,
                retry_quarantined=retry_quarantined,
                max_bytes=max_file_mb * 1024 * 1024 if max_file_mb else None,
            )
        except Exception as exc:  # noqa: BLE001 -- one bad file must not stop the batch
            logger.error("bankflow_file_error", file=file_path.name, error=type(exc).__name__)
            errors.append(f"{file_path.name}: {type(exc).__name__}")
            run.files_failed += 1
            _save_counters(run)
            continue
        affected_dates |= res.dates
        if res.message:
            errors.append(f"{file_path.name}: {res.message}")
        if res.outcome == "imported":
            run.files_imported += 1
        elif res.outcome == "duplicate":
            run.files_duplicate += 1
        elif res.outcome == "quarantined":
            run.files_quarantined += 1
        elif res.outcome == "failed":
            run.files_failed += 1
        _save_counters(run)  # durable progress: an interrupted run still shows what it did

    if affected_dates:
        try:
            flows.rebuild_aggregates(affected_dates)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"aggregate rebuild failed: {type(exc).__name__}")
            logger.error("bankflow_aggregate_error", error=type(exc).__name__)
    run.error_summary = "\n".join(errors)[:4000]
    run.finished_at = timezone.now()
    run.save()
    logger.info(
        "bankflow_import_finished",
        seen=run.files_seen,
        imported=run.files_imported,
        duplicate=run.files_duplicate,
        quarantined=run.files_quarantined,
        failed=run.files_failed,
    )
    return run


def _save_counters(run: ImportRun) -> None:
    run.save(
        update_fields=[
            "files_seen",
            "files_imported",
            "files_duplicate",
            "files_quarantined",
            "files_failed",
            "files_skipped",
        ]
    )


def ingest_file(
    path: Path,
    run: ImportRun,
    resolver: CodeResolver,
    *,
    retry_quarantined: bool = False,
    max_bytes: int | None = None,
) -> FileOutcome:
    limit = max_bytes or MAX_FILE_BYTES
    stat = path.stat()
    base = dict(
        filename=path.name,
        source_path=str(path),
        byte_size=stat.st_size,
        file_modified_at=dt.datetime.fromtimestamp(stat.st_mtime, tz=dt.UTC),
        import_run=run,
    )
    if stat.st_size > limit:
        file_hash = _hash_stream(path)
        if not StatementFile.objects.filter(file_hash=file_hash).exists():
            StatementFile.objects.create(
                file_hash=file_hash,
                status=StatementFile.Status.FAILED,
                status_reason=f"File exceeds {limit // (1024 * 1024)} MB safety limit.",
                **base,
            )
        return FileOutcome("failed", message="file too large")

    data = path.read_bytes()
    file_hash = hashlib.sha256(data).hexdigest()

    existing = StatementFile.objects.filter(file_hash=file_hash).first()
    if existing is not None:
        if retry_quarantined and existing.status != StatementFile.Status.IMPORTED:
            existing.delete()  # our own audit record, never the source file
        else:
            _touch_seen(existing)
            return FileOutcome("duplicate")

    try:
        parsed = parse_statement(data)
    except StatementFormatError as exc:
        StatementFile.objects.create(
            file_hash=file_hash,
            status=StatementFile.Status.FAILED,
            status_reason=str(exc),
            **base,
        )
        return FileOutcome("failed", message=str(exc))
    del data  # the raw bytes are no longer needed; parsed rows are what we keep

    try:
        return _store(parsed, file_hash, base, resolver)
    except Exception as exc:  # noqa: BLE001 -- whole-file rollback already happened
        logger.error("bankflow_store_failed", file=path.name, error=type(exc).__name__)
        StatementFile.objects.get_or_create(
            file_hash=file_hash,
            defaults=dict(
                status=StatementFile.Status.FAILED,
                status_reason=f"Import rolled back: {type(exc).__name__}",
                **base,
            ),
        )
        return FileOutcome("failed", message=f"import rolled back ({type(exc).__name__})")


def _hash_stream(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _touch_seen(sf: StatementFile) -> None:
    StatementFile.objects.filter(pk=sf.pk).update(
        seen_count=sf.seen_count + 1, last_seen_at=timezone.now()
    )


def _store(
    parsed: ParsedStatement, file_hash: str, base: dict, resolver: CodeResolver
) -> FileOutcome:
    expected = getattr(settings, "BANKFLOW_EXPECTED_ACCOUNT_SUFFIX", "")
    suffix = parsed.account_number[-5:] if parsed.account_number else ""

    normalized = {}
    ref_by_row: dict[int, str] = {}
    reversal_count = 0
    for r in parsed.rows:
        n = normalize_row(r.description, r.ref_column, debit=r.debit, credit=r.credit)
        normalized[r.index] = n
        ref_by_row[r.index] = n.ref_number
        reversal_count += n.is_reversal_candidate

    modified = base.get("file_modified_at")
    export_date = timezone.localtime(modified).date() if modified else None
    recon = reconcile(
        parsed, ref_by_row=ref_by_row, reversal_count=reversal_count, export_date=export_date
    )
    reasons: list[str] = []
    if expected and suffix != expected:
        reasons.append(
            f"Unexpected account (ends ...{suffix or '?'}; expected ...{expected}). "
            "Not the configured pool account."
        )
    keys = {r.index: _row_key(suffix, r, normalized[r.index].description_hash) for r in parsed.rows}
    hashes = sorted({normalized[r.index].description_hash for r in parsed.rows})

    with db_transaction.atomic():
        _lock()
        # Re-check inside the lock: a concurrent importer may have just stored this file.
        raced = StatementFile.objects.filter(file_hash=file_hash).first()
        if raced is not None:
            _touch_seen(raced)
            return FileOutcome("duplicate")

        # ---- overlap / conflict detection against rows already stored ------
        stored_keys: set[str] = set()
        for chunk in _chunks(list(keys.values())):
            stored_keys.update(
                StatementTransaction.objects.filter(row_key__in=chunk).values_list(
                    "row_key", flat=True
                )
            )
        overlap = sum(1 for k in keys.values() if k in stored_keys)

        by_identity: dict[tuple, set] = defaultdict(set)
        for chunk in _chunks(hashes):
            for h, d, deb, cred, bal in StatementTransaction.objects.filter(
                description_hash__in=chunk
            ).values_list("description_hash", "txn_date", "debit", "credit", "balance"):
                by_identity[(h, d, deb, cred)].add(bal)
        conflicts = 0
        for r in parsed.rows:
            ident = (normalized[r.index].description_hash, r.txn_date, r.debit, r.credit)
            balances = by_identity.get(ident)
            if balances and r.balance not in balances:
                conflicts += 1

        if parsed.start_date and parsed.opening_balance is not None:
            clash = StatementFile.objects.filter(
                status=StatementFile.Status.IMPORTED,
                account_suffix=suffix,
                statement_start=parsed.start_date,
                statement_end=parsed.end_date,
            ).exclude(opening_balance=parsed.opening_balance)
            if clash.exists():
                conflicts += 1
                recon.notes.append(
                    "Conflicting statement: same account and period already imported "
                    "with a different opening balance."
                )

        quarantine = recon.status == FAILED or conflicts > 0 or bool(reasons)
        if conflicts:
            reasons.append(f"{conflicts} conflicting row(s) vs already-imported data.")
        if recon.status == FAILED:
            reasons.append("Reconciliation failed: " + "; ".join(recon.notes))

        sf = StatementFile.objects.create(
            file_hash=file_hash,
            detected_format=parsed.detected_format,
            account_masked=_mask(parsed.account_number),
            account_suffix=suffix,
            statement_start=parsed.start_date,
            statement_end=parsed.end_date,
            first_row_date=min((r.txn_date for r in parsed.rows), default=None),
            last_row_date=max((r.txn_date for r in parsed.rows), default=None),
            opening_balance=parsed.opening_balance,
            book_balance=parsed.book_balance,
            available_balance=parsed.available_balance,
            hold_value=parsed.hold_value,
            header={
                k: v
                for k, v in parsed.metadata.items()
                if k
                in ("Description", "Currency", "Branch", "IFS Code", "Rate of Interest (% p.a.)")
            },
            row_count=len(parsed.rows),
            status=(
                StatementFile.Status.QUARANTINED if quarantine else StatementFile.Status.IMPORTED
            ),
            status_reason="; ".join(reasons),
            **base,
        )
        StatementReconciliation.objects.create(
            statement=sf,
            status=recon.status,
            opening_balance=recon.opening_balance,
            computed_closing=recon.computed_closing,
            last_row_balance=recon.last_row_balance,
            header_book_balance=recon.header_book_balance,
            header_gap=recon.header_gap,
            total_debit=recon.total_debit,
            total_credit=recon.total_credit,
            rows_parsed=recon.rows_parsed,
            malformed_rows=recon.malformed_rows,
            running_balance_mismatches=recon.running_balance_mismatches,
            first_mismatch_row=recon.first_mismatch_row,
            opening_date_matches_start=recon.opening_date_matches_start,
            available_equals_book_minus_hold=recon.available_equals_book_minus_hold,
            duplicate_ref_groups=recon.duplicate_ref_groups,
            reversal_candidates=recon.reversal_candidates,
            overlap_rows=overlap,
            conflicting_rows=conflicts,
            notes=recon.notes + [f"malformed: {m}" for m in parsed.malformed[:20]],
        )
        if quarantine:
            return FileOutcome("quarantined")

        new_rows = [r for r in parsed.rows if keys[r.index] not in stored_keys]
        _insert_rows(sf, new_rows, keys, normalized, resolver)
        return FileOutcome("imported", {r.txn_date for r in new_rows})


def _insert_rows(sf, rows, keys, normalized, resolver: CodeResolver) -> None:
    # duplicate-reference flags: same reference + same pool side more than once,
    # counting both this file's new rows and rows already stored.
    refs = sorted({normalized[r.index].ref_number for r in rows if normalized[r.index].ref_number})
    stored_counts: Counter = Counter()
    for chunk in _chunks(refs):
        stored_counts.update(
            StatementTransaction.objects.filter(ref_number__in=chunk).values_list(
                "ref_number", "side"
            )
        )
    new_counts: Counter = Counter(
        (normalized[r.index].ref_number, normalized[r.index].side)
        for r in rows
        if normalized[r.index].ref_number
    )
    dup_pairs = {p for p, c in new_counts.items() if c + stored_counts.get(p, 0) > 1}
    for ref, side in dup_pairs:
        StatementTransaction.objects.filter(ref_number=ref, side=side).update(is_duplicate_ref=True)

    # Stream in batches so memory stays flat however large the statement is.
    for chunk in _chunks(rows, BATCH):
        objs = []
        for r in chunk:
            n = normalized[r.index]
            objs.append(
                StatementTransaction(
                    row_key=keys[r.index],
                    first_statement=sf,
                    row_index=r.index,
                    txn_date=r.txn_date,
                    value_date=r.value_date,
                    branch_code=r.branch_code,
                    debit=r.debit,
                    credit=r.credit,
                    balance=r.balance,
                    description_hash=n.description_hash,
                    description_redacted=n.description_redacted,
                    ref_number=n.ref_number,
                    channel_raw=n.channel_raw,
                    channel_normalized=n.channel_normalized,
                    category=n.category,
                    side=n.side,
                    ko_raw=n.ko_raw,
                    is_duplicate_ref=(n.ref_number, n.side) in dup_pairs,
                    is_reversal_candidate=n.is_reversal_candidate,
                    reversal_reason=n.reversal_reason,
                )
            )
        StatementTransaction.objects.bulk_create(objs, batch_size=BATCH)
        id_by_key = dict(
            StatementTransaction.objects.filter(
                row_key__in=[keys[r.index] for r in chunk]
            ).values_list("row_key", "id")
        )
        resolutions = []
        for r in chunk:
            n = normalized[r.index]
            res = resolver.resolve(n.ko_raw)
            resolutions.append(
                CodeResolution(
                    transaction_id=id_by_key[keys[r.index]],
                    status=res.status,
                    method=res.method,
                    ko_raw=res.ko_raw,
                    resolved_csp_id=res.resolved_code,
                    candidate_codes=res.candidates,
                )
            )
        CodeResolution.objects.bulk_create(resolutions, batch_size=BATCH)


def reclassify_stored() -> int:
    """Bring stored rows in line with the current channel -> category map (for example after
    "initial deposit" became a deposit) and recompute the reversal flag of the rows that moved.
    Idempotent; the original description hash and amounts are never touched."""
    from django.db.models import Q

    from .models import StatementTransaction as Txn
    from .normalize import CHANNEL_MAP, RETIRED_NORMALIZED

    moved = 0
    for old, new in RETIRED_NORMALIZED.items():
        n = Txn.objects.filter(channel_normalized=old).update(
            channel_normalized=new, category="unclassified",
            is_reversal_candidate=False, reversal_reason="")
        moved += n
    for normalized, category in {v for v in CHANNEL_MAP.values()}:
        rows = Txn.objects.filter(channel_normalized=normalized).exclude(category=category)
        n = rows.update(category=category)
        if not n:
            continue
        moved += n
        wrong_way = {"withdrawal": Q(debit__gt=0), "deposit": Q(credit__gt=0)}.get(category)
        reason = {"withdrawal": "withdrawal_channel_debit", "deposit": "deposit_channel_credit"}
        same = Txn.objects.filter(channel_normalized=normalized)
        same.update(is_reversal_candidate=False, reversal_reason="")
        if wrong_way is not None:
            same.filter(wrong_way).update(is_reversal_candidate=True,
                                          reversal_reason=reason[category])
    return moved


@bumps_generation
def reresolve_all() -> dict[str, int]:
    """Re-run code resolution over every stored transaction (e.g. after new CSPs
    appear, or after the variant rule is confirmed) and rebuild aggregates.
    Original ko_raw values are never altered."""
    reclassified = reclassify_stored()
    resolver = build_resolver()
    changed = 0
    for res in CodeResolution.objects.iterator(chunk_size=BATCH):
        new = resolver.resolve(res.ko_raw)
        if (new.status, new.resolved_code, new.candidates) != (
            res.status,
            res.resolved_csp_id,
            res.candidate_codes,
        ):
            res.status = new.status
            res.method = new.method
            res.resolved_csp_id = new.resolved_code
            res.candidate_codes = new.candidates
            res.save(
                update_fields=["status", "method", "resolved_csp", "candidate_codes", "resolved_at"]
            )
            changed += 1
    rebuilt = flows.rebuild_aggregates()
    return {"reclassified": reclassified, "resolutions_changed": changed, **rebuilt}
