#!/usr/bin/env bash
# Back up the ITAM database to ./backups/, keeping the last 30 copies.
# Uses SQLite's own backup API so it is safe to run while the app is live.
#
#   ./backup.sh                 write to ./backups
#   ./backup.sh /mnt/nas/itam   write somewhere else
#
# Nightly at 02:30 via cron (run: crontab -e):
#   30 2 * * * cd /opt/itam && ./backup.sh >> /var/log/itam-backup.log 2>&1
set -euo pipefail
cd "$(dirname "$0")"

DEST="${1:-./backups}"
KEEP=30
DB="./data/itam.db"

[ -f "$DB" ] || { echo "No database at $DB" >&2; exit 1; }
mkdir -p "$DEST"

STAMP="$(date +%Y%m%d-%H%M%S)"
OUT="$DEST/itam-$STAMP.db"

# .backup is consistent under concurrent writes; copying the file is not.
if command -v sqlite3 >/dev/null 2>&1; then
    sqlite3 "$DB" ".backup '$OUT'"
else
    # Fall back to the container's Python, which always has sqlite3 built in.
    docker compose exec -T itam python -c "
import sqlite3
src = sqlite3.connect('/data/itam.db')
dst = sqlite3.connect('/data/.backup-tmp.db')
src.backup(dst); dst.close(); src.close()" 2>/dev/null \
        && mv ./data/.backup-tmp.db "$OUT" \
        || { echo "Need either sqlite3 on the host (apt install sqlite3) or a running container." >&2; exit 1; }
fi

gzip -f "$OUT"
echo "Backed up to $OUT.gz ($(du -h "$OUT.gz" | cut -f1))"

# Prune old backups, newest KEEP retained.
ls -1t "$DEST"/itam-*.db.gz 2>/dev/null | tail -n +$((KEEP + 1)) | while read -r old; do
    rm -f -- "$old"
    echo "Pruned $old"
done
