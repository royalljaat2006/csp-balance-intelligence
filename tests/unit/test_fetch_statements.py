"""scripts/fetch_statements_readonly.py: discovery filters, never-read list, idempotent
download, atomic writes, PII guard. Uses a fake SFTP; nothing touches a network."""

import datetime as dt
import hashlib
import importlib.util
import io
import stat
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

SPEC = importlib.util.spec_from_file_location(
    "fetch_statements_readonly",
    Path(__file__).resolve().parents[2] / "scripts" / "fetch_statements_readonly.py",
)
fetch = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = fetch  # dataclasses need the module registered
SPEC.loader.exec_module(fetch)

DIR, FILE = stat.S_IFDIR | 0o755, stat.S_IFREG | 0o644


@pytest.fixture(autouse=True)
def _no_real_server_settings(monkeypatch):
    """Django settings load the real .env into os.environ: keep the real R730_* values out."""
    import os

    for k in [k for k in os.environ if k.startswith("R730_") or k == "LOCAL_STATEMENT_DIR"]:
        monkeypatch.delenv(k)


class FakeSFTP:
    """A tiny read-only tree. Any attempt to write through it raises."""

    def __init__(self, tree):
        self.tree = tree  # {"dir": {"file": bytes}} or {"file": bytes}
        self.opened = []

    def _node(self, path):
        node = self.tree
        for part in [p for p in path.strip("/").split("/") if p][3:]:  # strip fake base
            node = node[part]
        return node

    def listdir_attr(self, path):
        node = self._node(path)
        return [
            SimpleNamespace(filename=n, st_mode=DIR if isinstance(v, dict) else FILE,
                            st_size=0 if isinstance(v, dict) else len(v), st_mtime=1_760_000_000)
            for n, v in node.items()
        ]

    def open(self, path, mode="rb"):
        assert mode == "rb", "read-only tool must never open for writing"
        self.opened.append(path)
        return io.BytesIO(self._node(path))


BASE = "/a/b/c"  # first three components are the fake base
TREE = {
    "2026-08-31": {"statement_1556_31-08-2026.xls": b"aug", "direct_1556_120000.xls": b"hourly"},
    "2026-10-05": {"statement_1556_05-10-2026.xls": b"oct5"},
    "2026-10-06": {"statement_1556_06-10-2026.xls": b"oct6!"},
    "provisional": {"statement_1556_x.xls": b"never"},
    "credential_health.json": b"SECRET",
    "run_state.json": b"STATE",
}


def test_discovery_takes_only_daily_statements_and_never_credentials():
    items = fetch.discover(FakeSFTP(TREE), BASE, "statement_1556_*", None)
    assert [i.rel for i in items] == [
        "2026-08-31/statement_1556_31-08-2026.xls",
        "2026-10-05/statement_1556_05-10-2026.xls",
        "2026-10-06/statement_1556_06-10-2026.xls",
    ]


def test_since_filters_by_day_folder():
    items = fetch.discover(FakeSFTP(TREE), BASE, "statement_1556_*", dt.date(2026, 10, 1))
    assert [i.rel.split("/")[0] for i in items] == ["2026-10-05", "2026-10-06"]


def test_list_reads_no_file_contents():
    sftp = FakeSFTP(TREE)
    fetch.discover(sftp, BASE, "statement_1556_*", None)
    assert sftp.opened == []


def test_download_is_idempotent_atomic_and_manifested(tmp_path):
    sftp = FakeSFTP(TREE)
    items = fetch.discover(sftp, BASE, "statement_1556_*", None)
    first = fetch.download(sftp, BASE, items, tmp_path)
    assert {m["status"] for m in first} == {"downloaded"}
    assert (tmp_path / "2026-10-06" / "statement_1556_06-10-2026.xls").read_bytes() == b"oct6!"
    assert first[0]["sha256"] == hashlib.sha256(b"aug").hexdigest()
    assert not list(tmp_path.rglob("*.part"))
    opened_before = len(sftp.opened)
    second = fetch.download(sftp, BASE, items, tmp_path)
    assert {m["status"] for m in second} == {"skipped_present"}
    assert len(sftp.opened) == opened_before  # nothing re-read


def test_a_truncated_transfer_is_discarded_not_kept(tmp_path):
    sftp = FakeSFTP(TREE)
    items = [fetch.Remote("2026-10-05/statement_1556_05-10-2026.xls", 999, 1_760_000_000)]
    out = fetch.download(sftp, BASE, items, tmp_path)
    assert out[0]["status"] == "size_mismatch"
    assert not (tmp_path / "2026-10-05" / "statement_1556_05-10-2026.xls").exists()
    assert not list(tmp_path.rglob("*.part"))


def test_destination_inside_the_repository_is_refused():
    assert fetch.inside_repo(fetch.REPO / "data" / "bank_statements")
    assert not fetch.inside_repo(fetch.REPO.parent / "_private" / "bank_statements")


def test_settings_are_read_from_env_file_without_exposing_other_keys(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("R730_SSH_HOST=10.0.0.5\nR730_SSH_USER=kapil\n# R730_SSH_PASSWORD=commented\n"
                   "SECRET_KEY=do-not-load\n", encoding="utf-8")
    monkeypatch.delenv("R730_SSH_PASSWORD", raising=False)
    cfg = fetch.load_settings(env)
    assert cfg == {"R730_SSH_HOST": "10.0.0.5", "R730_SSH_USER": "kapil"}


def test_main_refuses_a_destination_inside_the_repo(monkeypatch):
    monkeypatch.setattr(fetch, "load_settings",
                        lambda *a, **k: {"LOCAL_STATEMENT_DIR": str(fetch.REPO / "data" / "x")})
    with pytest.raises(SystemExit) as e:
        fetch.main(["--list"])
    assert "inside the repository" in str(e.value)


def test_flat_backup_folder_is_supported():
    flat = {"Calling Sheet_20260921_060000.xlsx": b"x", "notes.txt": b"n",
            "Calling Sheet_20260922_060000.xlsx": b"y"}
    items = fetch.discover(FakeSFTP(flat), BASE, "Calling Sheet_*.xlsx", None)
    assert [i.rel for i in items] == ["Calling Sheet_20260921_060000.xlsx",
                                      "Calling Sheet_20260922_060000.xlsx"]


@pytest.mark.parametrize("raw, expected", [
    ("(S3cret!x)", "S3cret!x"),
    ("(   S3cret!x    )  # note", "S3cret!x"),  # padding inside the brackets is not the password
    ("(S3cret!x)  # or R730_SSH_KEY_FILE=/path/to/id_ed25519", "S3cret!x"),
    ("(pa ss(1))", "pa ss(1)"),  # inner brackets belong to the secret; only the wrapper goes
    ("plain#hash", "plain#hash"),
    ("Ab#11@'Te'", "Ab#11@'Te'"),  # a trailing quote belonging to the password is kept
    ("(Ab#11@'Te')  # note", "Ab#11@'Te'"),  # an unwrapped password is never truncated at '#'
    ('"quoted"', "quoted"),
])
def test_password_brackets_and_comments_are_stripped_only_from_the_wrapper(raw, expected):
    assert fetch.clean_value("R730_SSH_PASSWORD", raw) == expected


def test_comment_only_and_trailing_comment_values_for_other_settings():
    assert fetch.clean_value("R730_SSH_HOSTKEY_SHA256", "# optional: pin the host key") == ""
    assert fetch.clean_value("R730_SSH_USER", "kapil   # who") == "kapil"
    assert fetch.clean_value("R730_SSH_PORT", "2222") == "2222"


def test_empty_values_are_dropped_so_defaults_apply(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text(
        "R730_SSH_HOST=h\nR730_SSH_HOSTKEY_SHA256=# optional\nR730_SSH_PASSWORD=(pw)  # note\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("R730_SSH_PASSWORD", raising=False)
    cfg = fetch.load_settings(env)
    assert cfg == {"R730_SSH_HOST": "h", "R730_SSH_PASSWORD": "pw"}
