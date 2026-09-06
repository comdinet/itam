#!/bin/sh
# Run the Entra ID and Intune syncs. Safe to re-run; nothing is deleted.
#
#   ./sync.sh                  everything, in dependency order
#   ./sync.sh licences         just one job
#   ./sync.sh --install-cron   run it nightly from now on
#   ./sync.sh --show-cron      what is scheduled, if anything
cd "$(dirname "$0")" || exit 1

HERE=$(pwd -P)
CRON_LINE="0 3 * * * cd $HERE && ./sync.sh >> /var/log/itam-sync.log 2>&1"

case "${1:-}" in
    --show-cron)
        if crontab -l 2>/dev/null | grep -Fq "$HERE/sync.sh" ||
           crontab -l 2>/dev/null | grep -F "cd $HERE" | grep -q sync.sh; then
            echo "Scheduled:"
            crontab -l 2>/dev/null | grep sync.sh
        else
            echo "Nothing scheduled. ITAM will only sync when somebody presses a"
            echo "button. To fix that:  ./sync.sh --install-cron"
        fi
        exit 0 ;;
    --install-cron)
        # Idempotent: an existing ITAM line is replaced, not duplicated.
        existing=$(crontab -l 2>/dev/null || true)
        cleaned=$(printf '%s\n' "$existing" | grep -v "$HERE/sync.sh" | grep -v "cd $HERE && ./sync.sh" || true)
        if ! printf '%s\n%s\n' "$cleaned" "$CRON_LINE" | grep -v '^$' | crontab - 2>/dev/null; then
            echo "Could not write the crontab." >&2
            echo "On macOS this usually means the terminal needs Full Disk Access." >&2
            echo "Add it by hand with 'crontab -e':" >&2
            echo "  $CRON_LINE" >&2
            exit 1
        fi
        # Say it is scheduled only once it demonstrably is.
        if ! crontab -l 2>/dev/null | grep -Fq "cd $HERE"; then
            echo "The crontab write reported success but the line is not there." >&2
            echo "Add it by hand with 'crontab -e':" >&2
            echo "  $CRON_LINE" >&2
            exit 1
        fi
        echo "Scheduled nightly at 03:00:"
        echo "  $CRON_LINE"
        echo
        echo "Log: /var/log/itam-sync.log  (create it writable if it does not exist:"
        echo "  sudo touch /var/log/itam-sync.log && sudo chown \$USER /var/log/itam-sync.log)"
        exit 0 ;;
    -h|--help)
        sed -n '2,7p' "$0" | sed 's/^# \{0,1\}//'
        exit 0 ;;
esac
if docker compose version >/dev/null 2>&1; then
    COMPOSE="docker compose"
else
    COMPOSE="docker-compose"
fi
exec $COMPOSE exec -T itam python -m app.jobs "$@"
