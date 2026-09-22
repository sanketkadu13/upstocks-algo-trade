#!/usr/bin/env bash
#
# Back up the state that cannot be regenerated: credentials, strategy config
# and trade history. Everything else (instrument master, price cache, scan
# cache) is derived and will rebuild itself.
#
# Suggested cron (daily at 16:30 IST):
#   30 16 * * 1-5 /opt/nifty-engine/deploy/backup.sh >> /var/log/nifty-backup.log 2>&1
#
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/nifty-engine}"
DEST="${BACKUP_DIR:-/var/backups/nifty-engine}"
STAMP="$(date +%Y%m%d-%H%M%S)"
KEEP_DAYS="${KEEP_DAYS:-60}"

mkdir -p "$DEST"
ARCHIVE="$DEST/nifty-engine-$STAMP.tar.gz"

tar -czf "$ARCHIVE" \
  -C "$APP_DIR" \
  .env \
  data/strategies.json \
  data/tap_ledger.jsonl \
  data/activity.jsonl \
  data/ipo_watchlist.json \
  data/ipo_ledger.jsonl \
  data/nifty_daily_range.json \
  2>/dev/null || true

chmod 600 "$ARCHIVE"   # it contains .env
echo "$(date -Is) backed up -> $ARCHIVE ($(du -h "$ARCHIVE" | cut -f1))"

find "$DEST" -name 'nifty-engine-*.tar.gz' -mtime +"$KEEP_DAYS" -delete
