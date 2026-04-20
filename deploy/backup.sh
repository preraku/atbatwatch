#!/usr/bin/env bash
# Nightly pg_dump → gzip → rsync to Hetzner Storage Box.
# Called by cron; set STORAGE_BOX_HOST and STORAGE_BOX_USER in /opt/atbatwatch/.env.
set -euo pipefail

APP_DIR="/opt/atbatwatch"
BACKUP_DIR="$APP_DIR/backups"
RETENTION_DAYS=7
DATE=$(date +%Y%m%d_%H%M%S)
FILENAME="atbatwatch_${DATE}.sql.gz"

# Load env (for POSTGRES_USER, POSTGRES_DB, STORAGE_BOX_*)
set -a
# shellcheck source=/dev/null
source "$APP_DIR/.env"
set +a

POSTGRES_USER="${POSTGRES_USER:-atbatwatch}"
POSTGRES_DB="${POSTGRES_DB:-atbatwatch}"
STORAGE_BOX_HOST="${STORAGE_BOX_HOST:-}"
STORAGE_BOX_USER="${STORAGE_BOX_USER:-}"

mkdir -p "$BACKUP_DIR"

echo "[backup] dumping $POSTGRES_DB at $DATE"
docker compose -f "$APP_DIR/docker-compose.prod.yml" exec -T postgres \
    pg_dump -U "$POSTGRES_USER" "$POSTGRES_DB" \
    | gzip > "$BACKUP_DIR/$FILENAME"

echo "[backup] wrote $BACKUP_DIR/$FILENAME ($(du -sh "$BACKUP_DIR/$FILENAME" | cut -f1))"

# Upload to Hetzner Storage Box if configured
if [[ -n "$STORAGE_BOX_HOST" && -n "$STORAGE_BOX_USER" ]]; then
    echo "[backup] syncing to storage box"
    rsync -az --delete \
        -e "ssh -p 23 -o StrictHostKeyChecking=accept-new -o BatchMode=yes" \
        "$BACKUP_DIR/" \
        "${STORAGE_BOX_USER}@${STORAGE_BOX_HOST}:/atbatwatch/backups/"
    echo "[backup] sync complete"
else
    echo "[backup] STORAGE_BOX_HOST/USER not set — skipping remote sync"
fi

# Prune local backups older than RETENTION_DAYS
find "$BACKUP_DIR" -name "*.sql.gz" -mtime "+$RETENTION_DAYS" -delete
echo "[backup] done"
