"""
READ-ONLY pull of SBI statement files from the R730 into a PRIVATE local folder.

The remote folder belongs to another team. This script only lists, stats and reads files over
SFTP: it never writes, renames, deletes, chmods or runs a remote command, and it never uses sudo.
Nothing is imported anywhere; run `manage.py ingest_bank_statements` on the local copy afterwards.

Statements contain customer names and account numbers, so the local copy must stay out of git:
the script refuses a destination inside this repository.

Settings (environment variables, or the repo's .env -- never printed):
  R730_SSH_HOST            host or IP
  R730_SSH_PORT            default 22
  R730_SSH_USER            an account allowed to read the folder
  R730_SSH_PASSWORD        or R730_SSH_KEY_FILE (path to a private key)
  R730_STATEMENT_DIR       default /home/adarsh/login_automation/yonosbi-automation/downloads
  R730_SSH_HOSTKEY_SHA256  optional; if set, the server's host key must match (base64, no padding)
  LOCAL_STATEMENT_DIR      default <repo>/../_private/bank_statements

Usage:
  python scripts/fetch_statements_readonly.py --list                      # look first, copy nothing
  python scripts/fetch_statements_readonly.py --download --since 2026-08-01
  # Calling Sheet backups (flat folder, if this account may read it):
  python scripts/fetch_statements_readonly.py --list --pattern 'Calling Sheet_*.xlsx' \
      --remote-dir /path/to/the/backups/folder      # add --download to copy
Requires paramiko (`uv pip install paramiko`; not a project dependency).
"""

from __future__ import annotations

import argparse
import base64
import csv
import datetime as dt
import fnmatch
import hashlib
import os
import re
import stat as statmod
import sys
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DEFAULT_REMOTE = "/home/adarsh/login_automation/yonosbi-automation/downloads"
DEFAULT_PATTERN = "statement_1556_*"
# Never read these, even if a pattern matches (they hold the other team's credentials/state).
NEVER_READ = {"credential_health.json", "credential_latch.json", "run_state.json"}
SKIP_DIRS = {"provisional"}  # hourly rolling windows, not ledgers
MAX_FILE_BYTES = 100 * 1024 * 1024
DATE_DIR = re.compile(r"^\d{4}-\d{2}-\d{2}$")


@dataclass(frozen=True)
class Remote:
    rel: str  # path relative to the statement dir, '/'-separated
    size: int
    mtime: int


def _unquote(v: str) -> str:
    """Remove ONE matching pair of surrounding quotes, never a lone leading/trailing quote:
    a password like  Ab#1@'Te'  ends with a quote that belongs to it."""
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        return v[1:-1]
    return v


def clean_value(key: str, raw: str) -> str:
    """One .env value. The password may be written as `(secret)`, optionally followed by a
    trailing `# comment`: the brackets, spaces inside them and the comment are not part of it.
    Any other setting ignores a comment-only value or a trailing ` # comment`."""
    v = raw.strip()
    if key == "R730_SSH_PASSWORD":
        wrapped = re.match(r"^\((.*)\)\s*(?:#.*)?$", v)
        return wrapped.group(1).strip() if wrapped else _unquote(v)  # `( secret )` -> secret
    if v.startswith("#"):
        return ""
    return _unquote(re.sub(r"\s+#.*$", "", v))


def load_settings(env_file: Path | None = REPO / ".env") -> dict[str, str]:
    cfg: dict[str, str] = {}
    if env_file and env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            m = re.match(r"^\s*(R730_[A-Z0-9_]+|LOCAL_STATEMENT_DIR)\s*=\s*(.*?)\s*$", line)
            if m and not line.lstrip().startswith("#"):
                cfg[m.group(1)] = clean_value(m.group(1), m.group(2))
    for k in list(os.environ):
        if k.startswith("R730_") or k == "LOCAL_STATEMENT_DIR":
            cfg[k] = clean_value(k, os.environ[k])
    return {k: v for k, v in cfg.items() if v != ""}


def discover(sftp, base: str, pattern: str, since: dt.date | None) -> list[Remote]:
    """Walk <base>/<YYYY-MM-DD>/ one level, files matching `pattern` only. Read-only."""
    found: list[Remote] = []
    for top in sftp.listdir_attr(base):
        name = top.filename
        if name in NEVER_READ or name in SKIP_DIRS:
            continue
        if not statmod.S_ISDIR(top.st_mode or 0):  # flat folder (e.g. Calling Sheet backups)
            if fnmatch.fnmatch(name, pattern):
                found.append(Remote(name, int(top.st_size or 0), int(top.st_mtime or 0)))
            continue
        if DATE_DIR.match(name) and since and dt.date.fromisoformat(name) < since:
            continue
        for f in sftp.listdir_attr(f"{base}/{name}"):
            if f.filename in NEVER_READ or statmod.S_ISDIR(f.st_mode or 0):
                continue
            if fnmatch.fnmatch(f.filename, pattern):
                found.append(
                    Remote(f"{name}/{f.filename}", int(f.st_size or 0), int(f.st_mtime or 0))
                )
    return sorted(found, key=lambda r: r.rel)


def inside_repo(path: Path) -> bool:
    try:
        path.resolve().relative_to(REPO)
        return True
    except ValueError:
        return False


def download(sftp, base: str, items: list[Remote], dest: Path) -> list[dict]:
    """Copy missing files (same size = already have it). Atomic: .part then rename (local only)."""
    manifest = []
    for it in items:
        target = dest / it.rel
        target.parent.mkdir(parents=True, exist_ok=True)
        status = "skipped_present"
        if it.size > MAX_FILE_BYTES:
            status = "skipped_too_large"
        elif not (target.exists() and target.stat().st_size == it.size):
            part = target.with_suffix(target.suffix + ".part")
            with sftp.open(f"{base}/{it.rel}", "rb") as src, open(part, "wb") as out:
                while chunk := src.read(1 << 20):
                    out.write(chunk)
            if part.stat().st_size != it.size:
                part.unlink()
                status = "size_mismatch"
            else:
                part.replace(target)
                status = "downloaded"
        sha = hashlib.sha256(target.read_bytes()).hexdigest() if target.exists() else ""
        manifest.append({"path": it.rel, "size": it.size,
                         "remote_mtime": dt.datetime.fromtimestamp(it.mtime, dt.UTC).isoformat(),
                         "sha256": sha, "status": status})
    return manifest


def connect(cfg: dict[str, str]):
    try:
        import paramiko
    except ImportError:
        sys.exit("paramiko is required: uv pip install paramiko")
    missing = [k for k in ("R730_SSH_HOST", "R730_SSH_USER") if not cfg.get(k)]
    if missing or not (cfg.get("R730_SSH_PASSWORD") or cfg.get("R730_SSH_KEY_FILE")):
        need = missing or ["R730_SSH_PASSWORD or R730_SSH_KEY_FILE"]
        sys.exit(f"Missing settings in .env / environment: {need}")
    client = paramiko.SSHClient()
    expected = cfg.get("R730_SSH_HOSTKEY_SHA256")

    class Check(paramiko.MissingHostKeyPolicy):
        def missing_host_key(self, _client, hostname, key):
            fp = base64.b64encode(hashlib.sha256(key.asbytes()).digest()).decode().rstrip("=")
            if expected and fp != expected:
                raise paramiko.SSHException(f"host key mismatch for {hostname}: {fp}")
            note = "" if expected else "  (NOT verified: set R730_SSH_HOSTKEY_SHA256)"
            print(f"host key SHA256:{fp}{note}")

    client.set_missing_host_key_policy(Check())
    client.connect(cfg["R730_SSH_HOST"], port=int(cfg.get("R730_SSH_PORT", "22")),
                   username=cfg["R730_SSH_USER"], password=cfg.get("R730_SSH_PASSWORD") or None,
                   key_filename=cfg.get("R730_SSH_KEY_FILE") or None,
                   timeout=20, look_for_keys=False, allow_agent=False)
    return client, client.open_sftp()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--list", action="store_true", help="list only; copy nothing")
    mode.add_argument("--download", action="store_true", help="copy missing files locally")
    ap.add_argument("--since", help="only date folders on/after YYYY-MM-DD")
    ap.add_argument("--pattern", default=DEFAULT_PATTERN)
    ap.add_argument("--remote-dir", help="override R730_STATEMENT_DIR (e.g. a flat backups folder)")
    ap.add_argument("--local-dir", help="override LOCAL_STATEMENT_DIR")
    ap.add_argument("--allow-inside-repo", action="store_true", help="NOT recommended (PII)")
    args = ap.parse_args(argv)

    cfg = load_settings()
    base = (args.remote_dir or cfg.get("R730_STATEMENT_DIR") or DEFAULT_REMOTE).rstrip("/")
    dest = Path(args.local_dir or cfg.get("LOCAL_STATEMENT_DIR")
                or REPO.parent / "_private" / "bank_statements")
    if inside_repo(dest) and not args.allow_inside_repo:
        sys.exit(f"Refusing {dest}: inside the repository (customer data must not be committable).")
    since = dt.date.fromisoformat(args.since) if args.since else None

    client, sftp = connect(cfg)
    try:
        items = discover(sftp, base, args.pattern, since)
        days = sorted({i.rel.split("/")[0] for i in items})
        total = sum(i.size for i in items)
        print(f"{len(items)} file(s), {total / 1e6:.1f} MB, {len(days)} day folder(s)"
              + (f": {days[0]} .. {days[-1]}" if days else ""))
        for i in items:
            when = dt.datetime.fromtimestamp(i.mtime, dt.UTC)
            print(f"  {when:%Y-%m-%d %H:%M}  {i.size:>10,}  {i.rel}")
        if args.download:
            dest.mkdir(parents=True, exist_ok=True)
            manifest = download(sftp, base, items, dest)
            with open(dest / "manifest.csv", "w", newline="", encoding="utf-8") as fh:
                w = csv.DictWriter(fh, fieldnames=list(manifest[0]) if manifest else ["path"])
                w.writeheader()
                w.writerows(manifest)
            counts: dict[str, int] = {}
            for m in manifest:
                counts[m["status"]] = counts.get(m["status"], 0) + 1
            print("download:", counts, "->", dest)
    finally:
        sftp.close()
        client.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
