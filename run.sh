#!/bin/sh
# Run ITAM directly for development, on http://127.0.0.1:8000
# The Docker deployment serves HTTPS via Caddy; this local path does not, so
# Secure cookies are turned off here or sign-in would fail over plain HTTP.
cd "$(dirname "$0")" || exit 1
if [ -f .env ]; then
    set -a; . ./.env; set +a
fi
# Local runs keep the database beside the code, not in the container volume.
[ "$ITAM_DB" = "/data/itam.db" ] && unset ITAM_DB
ITAM_COOKIE_SECURE="${ITAM_COOKIE_SECURE_DEV:-0}"
export ITAM_COOKIE_SECURE
exec ./.venv/bin/uvicorn app.main:app --reload --host 127.0.0.1 --port "${ITAM_PORT:-8000}"
