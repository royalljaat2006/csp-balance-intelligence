"""Folder discovery + the read-only inventory command: nothing in the scanned
folder is ever created, moved, renamed or deleted, and format comes from bytes."""

import csv
import os
import time

import pytest
from bankflow.ingest import discover_files, run_import
from bankflow.models import StatementFile
from csp.models import Csp
from django.core.management import call_command
from django.core.management.base import CommandError

from tests.bankflow_fixtures import make_statement, sample_rows

pytestmark = pytest.mark.django_db


def _write(path, text, age=60):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    os.utime(path, (time.time() - age,) * 2)
    return path


def _snapshot(folder):
    return sorted(
        (str(p.relative_to(folder)), p.stat().st_mtime_ns, p.stat().st_size)
        for p in folder.rglob("*")
    )


@pytest.fixture
def tree(tmp_path):
    root = tmp_path / "downloads"
    _write(root / "top.xls", make_statement(sample_rows(), opening="1000.00", start="12 Oct 2026"))
    _write(
        root / "2026-08" / "aug.xls",
        make_statement(sample_rows(), opening="500.00", start="01 Aug 2026", txn_date="01/08/2026"),
    )
    (root / "2026-08" / "notes.pdf").parent.mkdir(exist_ok=True)
    (root / "2026-08" / "notes.pdf").write_bytes(b"%PDF-1.4")
    _write(root / ".cache" / "hidden.xls", "x")
    return root


def test_discovery_is_not_recursive_unless_asked(tree):
    assert [p.name for p in discover_files(tree)] == ["top.xls"]
    assert [p.name for p in discover_files(tree, recursive=True)] == ["aug.xls", "top.xls"]


def test_hidden_subfolders_are_skipped_even_when_recursive(tree):
    assert "hidden.xls" not in [p.name for p in discover_files(tree, recursive=True)]


def test_import_never_modifies_the_source_folder(tree):
    Csp.objects.create(csp_code="1A852455", account_count=250)
    before = _snapshot(tree)
    run = run_import(tree, recursive=True)
    assert run.files_imported == 2
    assert _snapshot(tree) == before  # no file created, touched, renamed or removed


def test_inventory_reports_real_format_and_period_without_touching_the_folder(tree, capsys):
    before = _snapshot(tree)
    call_command("inventory_bank_statements", path=str(tree), recursive=True, no_db=True)
    out = capsys.readouterr().out
    assert "2 statement-looking file(s)" in out
    assert "tsv_text" in out and "2026-08-01..2026-08-01" in out and "acct=...71556" in out
    assert _snapshot(tree) == before
    assert StatementFile.objects.count() == 0  # inventory writes nothing to the database either


def test_inventory_shows_processing_status_from_the_database(tree, capsys):
    Csp.objects.create(csp_code="1A852455", account_count=250)
    run_import(tree)
    call_command("inventory_bank_statements", path=str(tree), recursive=True)
    out = capsys.readouterr().out
    assert "imported" in out and "not_processed" in out  # top.xls imported, aug.xls not yet


def test_inventory_trusts_bytes_not_extensions(tmp_path, capsys):
    root = tmp_path / "d"
    root.mkdir()
    (root / "disguised.xls").write_bytes(b"<html><body>not a statement</body></html>")
    call_command("inventory_bank_statements", path=str(root), no_db=True)
    assert "html" in capsys.readouterr().out


def test_inventory_csv_must_be_written_outside_the_scanned_folder(tree, tmp_path):
    with pytest.raises(CommandError, match="inside the scanned folder"):
        call_command(
            "inventory_bank_statements", path=str(tree), csv=str(tree / "inv.csv"), no_db=True
        )
    out = tmp_path / "inventory.csv"
    call_command("inventory_bank_statements", path=str(tree), csv=str(out), no_db=True)
    rows = list(csv.DictReader(open(out, encoding="utf-8")))
    assert rows and rows[0]["sha256"] and rows[0]["format"] == "tsv_text"
    assert not (tree / "inv.csv").exists()


def test_inventory_missing_path_is_a_clear_error(tmp_path):
    with pytest.raises(CommandError, match="does not exist"):
        call_command("inventory_bank_statements", path=str(tmp_path / "nope"), no_db=True)


def test_name_pattern_selects_only_daily_statements_not_hourly_direct_files(tmp_path):
    root = tmp_path / "downloads" / "2026-10-02"
    for name in (
        "statement_1556_02-10-2026.xls",
        "direct_1556_073049.xls",
        "credential_latch.json",
    ):
        _write(root / name, "x")
    (root / "provisional").mkdir()
    _write(root / "provisional" / "direct_1556_090335.xls", "x")
    found = discover_files(tmp_path / "downloads", recursive=True, pattern="statement_1556_*")
    assert [p.name for p in found] == ["statement_1556_02-10-2026.xls"]
    # .json (credential / run-state files) is never a statement suffix, with or without a pattern
    assert "credential_latch.json" not in [
        p.name for p in discover_files(tmp_path / "downloads", recursive=True)
    ]
