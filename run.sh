#!/bin/sh
# Run ITAM directly (no Docker) on http://127.0.0.1:8000
cd "$(dirname "$0")" || exit 1
if [ -f .env ]; then
    set -a; . ./.env; set +a
fi
# Local runs keep the database next to the code, not in the container's /data.
[ "$ITAM_DB" = "/data/itam.db" ] && unset ITAM_DB
exec ./.venv/bin/uvicorn app.main:app --reload --host 127.0.0.1 --port "${ITAM_PORT:-8000}"
