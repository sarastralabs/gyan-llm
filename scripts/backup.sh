#!/usr/bin/env bash
# Backs up everything that cannot be recreated: chats and users (MongoDB), the gateway's API keys
# and usage (SQLite), and the secrets in .env. Run ON the server, from the repo folder:
#
#     bash scripts/backup.sh                       # backups go to ~/backups
#
# The deploy cron job runs it every night. KEEP_DAYS (default 14) controls how long files stay.
# Backups on the same machine do not survive losing the machine: copy them off (see the runbook).
set -euo pipefail
cd "$(dirname "$0")/.."

DEST="${BACKUP_DIR:-$HOME/backups}"
KEEP="${KEEP_DAYS:-14}"
STAMP="$(date +%Y%m%d-%H%M%S)"
mkdir -p "$DEST"
umask 077

RUNNING="$(docker compose --profile ui ps --status running --services)"
case "$RUNNING" in
  *mongodb*)
    docker compose --profile ui exec -T mongodb mongodump --quiet --archive --gzip --db Gyan > "$DEST/mongo-$STAMP.archive.gz"
    echo "chats and users -> mongo-$STAMP.archive.gz" ;;
  *) echo "MongoDB is not running: skipped (the UI is not started?)" ;;
esac

# SQLite: use its online backup so a request in flight cannot leave a half-written copy.
docker compose exec -T gateway python -c "
import sqlite3
src = sqlite3.connect('/data/gateway.db'); dst = sqlite3.connect('/tmp/backup.db')
src.backup(dst); dst.close(); src.close()"
docker compose cp gateway:/tmp/backup.db "$DEST/gateway-$STAMP.db" >/dev/null
docker compose exec -T gateway rm -f /tmp/backup.db
echo "API keys and usage -> gateway-$STAMP.db"

install -m 600 .env "$DEST/env-$STAMP"
echo "secrets -> env-$STAMP"

find "$DEST" -type f -mtime "+$KEEP" -delete
echo "done; $(ls "$DEST" | wc -l) files in $DEST"
