#!/bin/sh
# Run ITAM directly for development, on http://127.0.0.1:8000
# The Docker deployment serves HTTPS via Caddy; this local path does not, so
# Secure cookies are turned off here or sign-in would fail over plain HTTP.
cd "$(dirname "$0")" || exit 1

# Read .env the way Docker Compose does, rather than sourcing it as a shell
# script. Sourcing would choke on any value containing spaces - a hostname
# list, or a Graph filter like: accountEnabled eq true and userType eq 'Member'
if [ -f .env ]; then
    while IFS= read -r line || [ -n "$line" ]; do
        case "$line" in ''|'#'*) continue ;; esac
        case "$line" in *=*) ;; *) continue ;; esac
        key=${line%%=*}
        val=${line#*=}
        case "$key" in *[!A-Za-z0-9_]*) continue ;; esac   # skip malformed keys
        # Strip one layer of surrounding quotes, as Compose does.
        case "$val" in
            \"*\") val=${val#\"}; val=${val%\"} ;;
            \'*\') val=${val#\'}; val=${val%\'} ;;
        esac
        export "$key=$val"
    done < .env
fi

# Local runs keep the database beside the code, not in the container volume.
[ "$ITAM_DB" = "/data/itam.db" ] && unset ITAM_DB
ITAM_COOKIE_SECURE="${ITAM_COOKIE_SECURE_DEV:-0}"
export ITAM_COOKIE_SECURE
exec ./.venv/bin/uvicorn app.main:app --reload --host 127.0.0.1 --port "${ITAM_PORT:-8000}"
