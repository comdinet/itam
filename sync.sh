#!/bin/sh
# Run the Entra ID and Intune syncs. Safe to re-run; nothing is deleted.
#
#   ./sync.sh              everything, in dependency order
#   ./sync.sh licences     just one job
#
# Nightly at 03:00 (crontab -e):
#   0 3 * * * cd /opt/itam && ./sync.sh >> /var/log/itam-sync.log 2>&1
cd "$(dirname "$0")" || exit 1
if docker compose version >/dev/null 2>&1; then
    COMPOSE="docker compose"
else
    COMPOSE="docker-compose"
fi
exec $COMPOSE exec -T itam python -m app.jobs "$@"
