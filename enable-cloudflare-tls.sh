#!/bin/sh
# Swap the self-signed certificate for a real one from Let's Encrypt, proved
# over Cloudflare DNS.
#
# DNS-01 means Caddy writes a TXT record through the Cloudflare API instead of
# answering a challenge on port 80. Nothing has to be reachable from the
# internet: the app can stay on an internal address and still have a
# certificate every browser trusts.
#
#   sudo ./enable-cloudflare-tls.sh
#   sudo ./enable-cloudflare-tls.sh --hostname itam.remedio.io \
#        --token cf_xxx --email you@example.com
set -eu

HOSTNAME_ARG=""
TOKEN_ARG=""
EMAIL_ARG=""
NO_START=0
DISABLE=0

while [ $# -gt 0 ]; do
    case "$1" in
        --disable)  DISABLE=1; shift ;;
        --hostname) HOSTNAME_ARG="$2"; shift 2 ;;
        --token)    TOKEN_ARG="$2"; shift 2 ;;
        --email)    EMAIL_ARG="$2"; shift 2 ;;
        --no-start) NO_START=1; shift ;;
        -h|--help)
            sed -n '/^#/,/^$/p' "$0" | sed -n '2,12p' | sed 's/^# \{0,1\}//'
            exit 0 ;;
        *) echo "Unknown option: $1" >&2; exit 1 ;;
    esac
done

cd "$(dirname "$0")"
[ -f .env ] || { echo "No .env here. Run ./setup.sh first." >&2; exit 1; }

COMPOSE="docker compose"
docker compose version >/dev/null 2>&1 || COMPOSE="docker-compose"
$COMPOSE version >/dev/null 2>&1 || {
    echo "Neither 'docker compose' nor 'docker-compose' works here." >&2; exit 1; }

read_env() {                       # value of a key in .env, without sourcing it
    sed -n "s/^$1=//p" .env | tail -1
}

if [ "$DISABLE" = "1" ]; then
    # Back to the self-signed certificate. The token and hostname stay in .env,
    # so switching back is one run of this script with no arguments.
    if ! grep -q '^COMPOSE_FILE=' .env; then
        echo "Cloudflare TLS is not enabled here - nothing to undo."
        exit 0
    fi
    old_umask=$(umask); umask 077
    tmp=$(mktemp)
    grep -v '^COMPOSE_FILE=' .env > "$tmp"
    cat "$tmp" > .env && rm -f "$tmp"
    umask "$old_umask"
    chmod 600 .env
    echo "Back to the self-signed certificate. Restarting..."
    $COMPOSE up -d --remove-orphans
    echo "Done. The token is still in .env, so re-run this script to switch back."
    exit 0
fi

ask() {                            # ask $2, default $3, echo the answer
    printf '%s' "$1"
    [ -n "$3" ] && printf ' [%s]' "$3"
    printf ': '
    read -r reply || reply=""
    [ -n "$reply" ] && printf '%s' "$reply" || printf '%s' "$3"
}

[ -n "$HOSTNAME_ARG" ] || HOSTNAME_ARG=$(ask "Public DNS name" "" "$(read_env ITAM_PUBLIC_HOSTNAME)")
[ -n "$HOSTNAME_ARG" ] || {
    echo "A DNS name is required." >&2; exit 1; }
# An IP address has dots in it too, so checking for dots is not enough. A public
# certificate cannot be issued for an IP by this method at all.
if printf '%s' "$HOSTNAME_ARG" | grep -Eq '^[0-9]+(\.[0-9]+){3}$'; then
    echo "'$HOSTNAME_ARG' is an IP address. Let's Encrypt issues for names, not" >&2
    echo "addresses - use the DNS name that points at this server." >&2
    exit 1
fi
case "$HOSTNAME_ARG" in
    *.*) : ;;
    *) echo "'$HOSTNAME_ARG' is not a fully qualified name." >&2; exit 1 ;;
esac

if [ -z "$TOKEN_ARG" ]; then
    echo
    echo "Cloudflare API token. Create it at"
    echo "  https://dash.cloudflare.com/profile/api-tokens  ->  Create Token"
    echo "  ->  Edit zone DNS  ->  Zone Resources: Include -> Specific zone -> your zone"
    echo "It needs exactly two permissions: Zone:DNS:Edit and Zone:Zone:Read."
    echo
    TOKEN_ARG=$(ask "API token" "" "$(read_env CLOUDFLARE_API_TOKEN)")
fi
[ -n "$TOKEN_ARG" ] || { echo "A token is required." >&2; exit 1; }
case "$TOKEN_ARG" in
    \"*|\'*|\{*)
        echo "Paste the token on its own - no quotes, no braces." >&2; exit 1 ;;
esac

[ -n "$EMAIL_ARG" ] || EMAIL_ARG=$(ask "Email for expiry notices (optional)" "" "$(read_env ITAM_ACME_EMAIL)")

echo
echo "Checking the token against the Cloudflare API before changing anything..."
verify=$(docker run --rm curlimages/curl:latest -s \
    -H "Authorization: Bearer $TOKEN_ARG" \
    https://api.cloudflare.com/client/v4/user/tokens/verify 2>/dev/null || true)
case "$verify" in
    *'"success":true'*) echo "  token is valid and active." ;;
    "") echo "  could not reach the API to check. Carrying on; Caddy's log will" ;
        echo "  give the real answer." ;;
    *)  echo "  Cloudflare rejected the token:" >&2
        echo "  $verify" >&2
        echo >&2
        echo "  Nothing has been changed. Fix the token and run this again." >&2
        exit 1 ;;
esac

zone=${HOSTNAME_ARG#*.}
echo "Confirming the token can see the zone $zone..."
zones=$(docker run --rm curlimages/curl:latest -s \
    -H "Authorization: Bearer $TOKEN_ARG" \
    "https://api.cloudflare.com/client/v4/zones?name=$zone" 2>/dev/null || true)
case "$zones" in
    *"\"name\":\"$zone\""*) echo "  $zone is in reach." ;;
    *'"success":true'*)
        echo "  the token works, but it cannot see the zone '$zone'." >&2
        echo "  Give it Zone Resources -> Include -> $zone, then run this again." >&2
        echo "  Nothing has been changed." >&2
        exit 1 ;;
    *) echo "  could not confirm; Caddy's log will say." ;;
esac

set_env() {                        # set or replace KEY=value in .env
    key="$1"; value="$2"
    old_umask=$(umask); umask 077
    if grep -q "^$key=" .env; then
        tmp=$(mktemp)
        grep -v "^$key=" .env > "$tmp"
        printf '%s=%s\n' "$key" "$value" >> "$tmp"
        cat "$tmp" > .env && rm -f "$tmp"
    else
        printf '%s=%s\n' "$key" "$value" >> .env
    fi
    umask "$old_umask"
}

set_env ITAM_PUBLIC_HOSTNAME "$HOSTNAME_ARG"
set_env ITAM_ACME_EMAIL "$EMAIL_ARG"
set_env CLOUDFLARE_API_TOKEN "$TOKEN_ARG"
# Both compose files, named here rather than on the command line, so every
# documented `docker compose ...` keeps working unchanged.
set_env COMPOSE_FILE "docker-compose.yml:docker-compose.cloudflare.yml"
chmod 600 .env
echo
echo ".env updated."

if [ "$NO_START" = "1" ]; then
    echo
    echo "Not starting, as asked. When you are ready:"
    echo "  $COMPOSE up -d --build"
    exit 0
fi

echo
echo "Building Caddy with the Cloudflare DNS module (once) and restarting..."
$COMPOSE up -d --build

echo
echo "Waiting for the certificate. First issuance takes 30-90 seconds while the"
echo "TXT record propagates."
i=0
while [ "$i" -lt 60 ]; do
    if $COMPOSE logs caddy 2>/dev/null | grep -q "certificate obtained successfully"; then
        echo "  certificate obtained."
        break
    fi
    if $COMPOSE logs caddy 2>/dev/null | grep -qi "could not get certificate\|error.*acme"; then
        echo "  Caddy reported a problem:" >&2
        $COMPOSE logs --tail=20 caddy >&2
        exit 1
    fi
    i=$((i + 1))
    sleep 3
done

echo
echo "Done. https://$HOSTNAME_ARG"
echo
echo "The DNS record for $HOSTNAME_ARG must point at this server, and can be"
echo "DNS-only (grey cloud) - the certificate was proved over DNS, not traffic."
echo "Direct access by IP or short name still works, with the old self-signed"
echo "warning."
echo
echo "For SSO, set the base URL to https://$HOSTNAME_ARG under Settings -> SSO."
