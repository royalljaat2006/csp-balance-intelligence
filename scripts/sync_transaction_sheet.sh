#!/bin/bash
# Periodic READ-ONLY sync of the live rack-server transaction workbook into
# this app's own watched incoming/ folder.
#
# The real transaction data on the R730 lives as a continuously-updated
# monthly master workbook (e.g. "Transaction Oct'26.xlsx") in another
# team's directory, not as discrete drop-in files — the opposite shape from
# what ingestion/file_ingest.py expects (files dropped into incoming/, then
# moved to processed/ or failed/, requiring write access to the source).
# This script bridges the two without ever writing to the source directory:
# it only reads the current month's file and copies it into our own
# incoming/. The existing ingest_transactions job then picks it up exactly
# as it always has — parsing, upserting (idempotent — safe to re-ingest an
# updated version of the same month repeatedly), and filing it away.
#
# Run via cron, e.g.: 0 */4 * * * TRANSACTION_DATA_DIR_HOST=/path/to/data
#   /path/to/scripts/sync_transaction_sheet.sh >> /path/to/sync.log 2>&1
set -euo pipefail

SOURCE_DIR="${TRANSACTION_SOURCE_DIR:-/home/shumit/sbikisok}"
: "${TRANSACTION_DATA_DIR_HOST:?set TRANSACTION_DATA_DIR_HOST to the app's data dir}"
DEST_DIR="${TRANSACTION_DATA_DIR_HOST}/incoming"
PROCESSED_DIR="${TRANSACTION_DATA_DIR_HOST}/processed"
RETENTION_DAYS="${TRANSACTION_SYNC_RETENTION_DAYS:-14}"
SYNC_BASENAME="transaction_sync"

MONTH_FILE="Transaction $(date +%b)'$(date +%y).xlsx"
SRC="${SOURCE_DIR}/${MONTH_FILE}"

if [ ! -f "$SRC" ]; then
  echo "$(date -Is) [sync_transaction_sheet] source not found: $SRC" >&2
  exit 1
fi

mkdir -p "$DEST_DIR"
cp "$SRC" "${DEST_DIR}/${SYNC_BASENAME}.xlsx"
echo "$(date -Is) [sync_transaction_sheet] copied $SRC -> ${DEST_DIR}/${SYNC_BASENAME}.xlsx"

# Bound disk growth: every cycle re-syncs the whole month's file, so
# processed/ gains one full copy per run. ingestion/file_safety.py never
# auto-deletes processed/failed files by design (a genuine upload's
# processed copy is kept as an audit trail) -- but these are redundant
# snapshots of a re-synced live source, not distinct uploads, so only
# files this script itself produced (matched by name prefix) are pruned.
if [ -d "$PROCESSED_DIR" ]; then
  find "$PROCESSED_DIR" -maxdepth 1 -name "${SYNC_BASENAME}.*" -mtime "+${RETENTION_DAYS}" -delete
fi
