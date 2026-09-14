#!/bin/sh
# Put ITAM behind a Cloudflare tunnel.
#
# cloudflared dials out to Cloudflare and holds the connection open, so this
# machine needs no inbound port, no firewall hole and no public IP. Cloudflare
# terminates TLS at the edge and the DNS record it creates is proxied - the
# orange cloud - so the origin address is never published.
#
#   sudo ./enable-cloudflare-tunnel.sh
#   sudo ./enable-cloudflare-tunnel.sh --token eyJhIjoi...
#   sudo ./enable-cloudflare-tunnel.sh --disable
set -eu

TOKEN_ARG=""
NO_START=0
DISABLE=0
KEEP_PORTS=0

while [ $# -gt 0 ]; do
    case "$1" in
        --disable)    DISABLE=1; shift ;;
        --token)      TOKEN_ARG="$2"; shift 2 ;;
        --keep-ports) KEEP_PORTS=1; shift ;;
        --no-start)   NO_START=1; shift ;;
        -h|--help)
            sed -n '2,10p' "$0" | sed 's/^# \{0,1\}//'
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

read_env() { sed -n "s/^$1=//p" .env | tail -1; }

set_env() {                        # set_env KEY VALUE, atomically, mode 600
    old_umask=$(umask); umask 077
    tmp=$(mktemp)
    grep -v "^$1=" .env > "$tmp" || true
    printf '%s=%s\n' "$1" "$2" >> "$tmp"
    cat "$tmp" > .env && rm -f "$tmp"
    umask "$old_umask"
    chmod 600 .env
}

drop_env() {
    old_umask=$(umask); umask 077
    tmp=$(mktemp)
    grep -v "^$1=" .env > "$tmp" || true
    cat "$tmp" > .env && rm -f "$tmp"
    umask "$old_umask"
    chmod 600 .env
}

# COMPOSE_FILE may already name the TLS overlay. Adding and removing this one
# has to leave that alone, or turning the tunnel off would silently take the
# certificate with it.
compose_files() { read_env COMPOSE_FILE; }

without_tunnel() {
    compose_files | tr ':' '\n' | grep -v '^docker-compose.cloudflared.yml$' \
        | grep -v '^$' | paste -sd: -
}

if [ "$DISABLE" = "1" ]; then
    if ! compose_files | grep -q 'cloudflared'; then
        echo "The tunnel is not enabled here - nothing to undo."
        exit 0
    fi
    rest=$(without_tunnel)
    if [ -n "$rest" ]; then set_env COMPOSE_FILE "$rest"; else drop_env COMPOSE_FILE; fi
    echo "Stopping the tunnel..."
    $COMPOSE stop cloudflared 2>/dev/null || true
    $COMPOSE rm -f cloudflared 2>/dev/null || true
    $COMPOSE up -d --remove-orphans
    echo
    echo "The tunnel is off. The token is still in .env, so re-running this"
    echo "script switches it back on."
    if [ "$(read_env ITAM_HTTPS_PORT)" != "${ITAM_HTTPS_PORT:-}" ]; then
        case "$(read_env ITAM_HTTPS_PORT)" in
            127.0.0.1:*)
                echo
                echo "NOTE: ITAM_HTTPS_PORT is still bound to loopback, so nothing"
                echo "can reach this box directly. Set it back to 443 in .env if"
                echo "you want the old way in:"
                echo "  ITAM_HTTPS_PORT=443" ;;
        esac
    fi
    exit 0
fi

if [ -z "$TOKEN_ARG" ]; then
    cat <<'HOWTO'

Create the tunnel first, in the dashboard:

  1. https://one.dash.cloudflare.com  ->  Networks  ->  Tunnels
  2. Create a tunnel  ->  Cloudflared  ->  name it (itam, say)
  3. Ignore the install instructions - this script is the install. Copy the
     token out of the command it shows you: the long string after --token,
     starting eyJ.

Then come back here and paste it. The dashboard will not let you add a
public hostname yet: "Connection Status" says no connection detected, and
Next stays greyed out until something connects. Running this script is what
connects it.

Once it is running, go back to that tunnel in the dashboard, press Next, and
add the public hostname:

  Subdomain: itam          Domain: your zone
  Service URL: http://itam:8000

Include the http:// - the newer dialog rejects a bare host. An older
dashboard splits that into a Type dropdown (HTTP) and a URL box (itam:8000);
either way it is plain http to port 8000, because the tunnel reaches the app
inside this machine's Docker network and Cloudflare does TLS at the edge.

Saving the route also creates the DNS record for you - proxied, pointing at
the tunnel rather than at this machine's address.

HOWTO
    printf 'Tunnel token: '
    read -r TOKEN_ARG || TOKEN_ARG=""
fi
[ -n "$TOKEN_ARG" ] || { echo "A token is required." >&2; exit 1; }
case "$TOKEN_ARG" in
    \"*|\'*) echo "Paste the token on its own - no quotes." >&2; exit 1 ;;
    eyJ*) : ;;
    *) echo "That does not look like a tunnel token (they start 'eyJ')." >&2
       echo "It is the string after --token in the dashboard's install command," >&2
       echo "not an API token." >&2
       exit 1 ;;
esac

echo
echo "Trying the token against Cloudflare before changing anything..."
# Run the real connector for a few seconds and watch for a registered
# connection. Writing .env first and finding out later is how you end up with
# a half-configured machine and no idea which half.
log=$(mktemp)
docker run --rm --name itam-tunnel-check \
    -e TUNNEL_TOKEN="$TOKEN_ARG" \
    cloudflare/cloudflared:latest tunnel --no-autoupdate run >"$log" 2>&1 &
checker=$!
ok=0
i=0
while [ "$i" -lt 25 ]; do
    if grep -qi "Registered tunnel connection" "$log" 2>/dev/null; then ok=1; break; fi
    if grep -qi "provided tunnel token is not valid\|Unauthorized\|failed to parse" "$log" 2>/dev/null; then
        break
    fi
    kill -0 "$checker" 2>/dev/null || break
    sleep 1
    i=$((i + 1))
done
docker stop itam-tunnel-check >/dev/null 2>&1 || true
wait "$checker" 2>/dev/null || true

# One last look. The connector can write its success line and exit between two
# passes of the loop above, and giving up on a connection that did happen is a
# worse answer than waiting another second for one that did not.
if [ "$ok" != "1" ] && grep -qi "Registered tunnel connection" "$log" 2>/dev/null; then
    ok=1
fi

if [ "$ok" != "1" ]; then
    echo "The tunnel did not connect. Nothing has been changed." >&2
    echo >&2
    sed -n '1,20p' "$log" >&2
    rm -f "$log"
    exit 1
fi
rm -f "$log"
echo "Connected. Writing .env..."

set_env CLOUDFLARE_TUNNEL_TOKEN "$TOKEN_ARG"

files=$(without_tunnel)
if [ -z "$files" ]; then files="docker-compose.yml"; fi
case "$files" in
    *docker-compose.cloudflared.yml*) : ;;
    *) files="$files:docker-compose.cloudflared.yml" ;;
esac
set_env COMPOSE_FILE "$files"

# With a tunnel there is no reason for this machine to answer on 80 or 443.
# Bound to loopback rather than removed, so `curl -k https://localhost` from
# the box itself still works when something needs checking.
if [ "$KEEP_PORTS" = "0" ]; then
    set_env ITAM_HTTP_PORT "127.0.0.1:8080"
    set_env ITAM_HTTPS_PORT "127.0.0.1:8443"
fi

if [ "$NO_START" = "1" ]; then
    echo "Not starting, as asked. Bring it up with:  $COMPOSE up -d"
    exit 0
fi

hostname_guess=$(read_env ITAM_PUBLIC_HOSTNAME | cut -d. -f1)
echo "Starting..."
$COMPOSE up -d --remove-orphans

echo
# Report what is true, read back, rather than what was intended.
if $COMPOSE ps cloudflared 2>/dev/null | grep -qi "up\|running"; then
    echo "The tunnel is running."
else
    echo "The tunnel container is not running. Check:  $COMPOSE logs cloudflared" >&2
    exit 1
fi
if [ "$KEEP_PORTS" = "0" ]; then
    echo "Ports 80 and 443 are no longer published on this machine - the only"
    echo "way in is through Cloudflare. Close them at the firewall too."
    echo "To undo just that part:  ./enable-cloudflare-tunnel.sh --keep-ports"
fi
echo
echo "NEXT: the tunnel is connected but not yet routed anywhere. Back in the"
echo "dashboard, that tunnel's Next button is live now. Press it and add a"
echo "public hostname:"
echo "    Subdomain: ${hostname_guess:-itam}   Domain: your zone"
echo "    Service URL: http://itam:8000    (the http:// is required)"
echo "Saving that creates the proxied DNS record too."
echo
echo "Then check it from somewhere else:  curl -I https://$(read_env ITAM_PUBLIC_HOSTNAME)"
echo "Logs:                          $COMPOSE logs -f cloudflared"
echo "Off again:                     ./enable-cloudflare-tunnel.sh --disable"
