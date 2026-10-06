"""Files are moved, never deleted (Phase 4/10) — processed/failed both land intact."""

from ingestion.file_safety import IngestionPaths, move_to_failed, move_to_processed


def test_ensure_exist_creates_all_three(tmp_path):
    paths = IngestionPaths(root=tmp_path)
    paths.ensure_exist()
    assert paths.incoming.is_dir()
    assert paths.processed.is_dir()
    assert paths.failed.is_dir()


def test_move_to_processed_preserves_content(tmp_path):
    paths = IngestionPaths(root=tmp_path)
    paths.ensure_exist()
    source = paths.incoming / "Transaction Sep'26.xlsx"
    source.write_bytes(b"workbook-bytes")

    dest = move_to_processed(paths, source)

    assert not source.exists()
    assert dest.exists()
    assert dest.parent == paths.processed
    assert dest.read_bytes() == b"workbook-bytes"


def test_move_to_failed_preserves_content(tmp_path):
    paths = IngestionPaths(root=tmp_path)
    paths.ensure_exist()
    source = paths.incoming / "bad.xlsx"
    source.write_bytes(b"garbage")

    dest = move_to_failed(paths, source)

    assert not source.exists()
    assert dest.exists()
    assert dest.parent == paths.failed
    assert dest.read_bytes() == b"garbage"
