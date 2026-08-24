#!/usr/bin/env bash
# ITAM setup for Ubuntu. Writes .env, prepares the data directory, then builds
# and starts the container. Safe to re-run: it offers to keep existing settings.
#
#   sudo ./setup.sh                      interactive
#   sudo ./setup.sh --yes                accept defaults, generate admin password
#   sudo ./setup.sh --port 8080 --admin-user ops --admin-password 'sekrit...' --yes
#
set -euo pipefail

cd "$(dirname "$0")"

DATA_UID=10001          # must match the USER in the Dockerfile
ENV_FILE=".env"
DATA_DIR="./data"

PORT=""; ADMIN_USER=""; ADMIN_PASSWORD=""; CURRENCY=""; HTTPS=""
ASSUME_YES=0; NO_START=0; RECONFIGURE=0

die()  { printf '\n\033[31mError:\033[0m %s\n' "$1" >&2; exit 1; }
info() { printf '\033[36m%s\033[0m\n' "$1"; }
ok()   { printf '\033[32m%s\033[0m\n' "$1"; }
warn() { printf '\033[33m%s\033[0m\n' "$1"; }

while [ $# -gt 0 ]; do
    case "$1" in
        --port)            PORT="${2:-}"; shift 2 ;;
        --admin-user)      ADMIN_USER="${2:-}"; shift 2 ;;
        --admin-password)  ADMIN_PASSWORD="${2:-}"; shift 2 ;;
        --currency)        CURRENCY="${2:-}"; shift 2 ;;
        --https)           HTTPS="1"; shift ;;
        --no-https)        HTTPS="0"; shift ;;
        --yes|-y)          ASSUME_YES=1; shift ;;
        --no-start)        NO_START=1; shift ;;
        --reconfigure)     RECONFIGURE=1; shift ;;
        -h|--help)         sed -n '2,9p' "$0"; exit 0 ;;
        *)                 die "Unknown option: $1" ;;
    esac
done

ask() {   # ask <prompt> <default> -> echoes answer
    local prompt="$1" default="$2" reply=""
    if [ "$ASSUME_YES" = "1" ]; then echo "$default"; return; fi
    read -r -p "$prompt [$default]: " reply </dev/tty || reply=""
    echo "${reply:-$default}"
}

ask_yn() {  # ask_yn <prompt> <default y|n>
    local prompt="$1" default="$2" reply=""
    if [ "$ASSUME_YES" = "1" ]; then [ "$default" = "y" ]; return; fi
    read -r -p "$prompt [$( [ "$default" = y ] && echo 'Y/n' || echo 'y/N' )]: " reply </dev/tty || reply=""
    reply="${reply:-$default}"
    [[ "$reply" =~ ^[Yy] ]]
}

# Subshell with pipefail off: `head` closing the pipe sends SIGPIPE to the
# reader, which under `set -o pipefail` would fail the whole script.
gen_password() (
    set +o pipefail
    LC_ALL=C tr -dc 'A-Za-z0-9' < /dev/urandom | head -c 20
)

echo
info "=============================================="
info "  ITAM setup"
info "=============================================="
echo

# --- 1. Docker ----------------------------------------------------------
SUDO=""
if [ "$(id -u)" -ne 0 ]; then
    command -v sudo >/dev/null 2>&1 || die "Run this as root, or install sudo."
    SUDO="sudo"
fi

COMPOSE=""
if command -v docker >/dev/null 2>&1; then
    if docker compose version >/dev/null 2>&1; then COMPOSE="docker compose"
    elif command -v docker-compose >/dev/null 2>&1; then COMPOSE="docker-compose"
    fi
fi

if [ -z "$COMPOSE" ]; then
    warn "Docker with the Compose plugin was not found."
    echo "  This installs Ubuntu's own packages: docker.io docker-compose-v2"
    if ask_yn "  Install them now with apt?" y; then
        $SUDO apt-get update
        $SUDO apt-get install -y docker.io docker-compose-v2 \
            || die "apt install failed. Install Docker manually: https://docs.docker.com/engine/install/ubuntu/"
        $SUDO systemctl enable --now docker
        if docker compose version >/dev/null 2>&1; then COMPOSE="docker compose"
        elif command -v docker-compose >/dev/null 2>&1; then COMPOSE="docker-compose"
        else die "Docker installed but Compose is still missing. See https://docs.docker.com/engine/install/ubuntu/"
        fi
    else
        die "Docker is required. See https://docs.docker.com/engine/install/ubuntu/"
    fi
fi
ok "Using: $COMPOSE"

# Reboot survival needs the service enabled, not just running. This is a no-op
# if it is already enabled, and skipped on hosts without systemd.
if command -v systemctl >/dev/null 2>&1 && systemctl list-unit-files docker.service >/dev/null 2>&1; then
    if ! systemctl is-enabled docker >/dev/null 2>&1; then
        $SUDO systemctl enable --now docker >/dev/null 2>&1 \
            && ok "Enabled the docker service so ITAM restarts after a reboot" \
            || warn "Could not enable the docker service; ITAM may not restart after a reboot."
    fi
fi

if ! docker info >/dev/null 2>&1; then
    if [ -n "$SUDO" ]; then
        warn "Your user cannot talk to the Docker daemon; continuing with sudo."
        warn "To fix permanently:  sudo usermod -aG docker $USER   (then log out and back in)"
        DOCKER_PREFIX="$SUDO "
    else
        die "Cannot talk to the Docker daemon. Is it running?  systemctl status docker"
    fi
else
    DOCKER_PREFIX=""
fi

# --- 2. Existing config -------------------------------------------------
get_env() { [ -f "$ENV_FILE" ] && sed -n "s/^$1=//p" "$ENV_FILE" | head -1 || true; }

# get_env_or KEY DEFAULT - an existing-but-empty value must not win.
get_env_or() {
    local v
    v="$(get_env "$1")"
    if [ -n "$v" ]; then echo "$v"; else echo "$2"; fi
}

if [ -f "$ENV_FILE" ] && [ "$RECONFIGURE" = "0" ]; then
    warn "$ENV_FILE already exists."
    if ask_yn "  Keep the existing settings and just rebuild?" y; then
        KEEP_ENV=1
    else
        KEEP_ENV=0
    fi
else
    KEEP_ENV=0
fi

if [ "${KEEP_ENV:-0}" = "0" ]; then
    echo
    info "--- Settings ---"

    [ -n "$PORT" ] || PORT="$(ask 'Host port to serve on' "$(get_env_or ITAM_PORT 8000)")"
    [[ "$PORT" =~ ^[0-9]+$ ]] && [ "$PORT" -ge 1 ] && [ "$PORT" -le 65535 ] \
        || die "Invalid port: $PORT"
    if command -v ss >/dev/null 2>&1 && ss -ltn 2>/dev/null | grep -qE "[:.]${PORT}\b"; then
        warn "Something is already listening on port $PORT."
        ask_yn "  Use it anyway?" n || die "Pick a free port and re-run."
    fi

    [ -n "$ADMIN_USER" ] || ADMIN_USER="$(ask 'Admin sign-in username' "$(get_env_or ITAM_ADMIN_USER admin)")"
    [[ "$ADMIN_USER" =~ ^[A-Za-z0-9._-]+$ ]] || die "Username may only contain letters, digits, dot, dash, underscore."

    if [ -z "$ADMIN_PASSWORD" ]; then
        if [ "$ASSUME_YES" = "1" ]; then
            ADMIN_PASSWORD="$(gen_password)"
            GENERATED=1
        else
            echo "  Admin password - at least 12 characters. Leave blank to generate one."
            while :; do
                read -r -s -p "  Password: " ADMIN_PASSWORD </dev/tty; echo
                if [ -z "$ADMIN_PASSWORD" ]; then
                    ADMIN_PASSWORD="$(gen_password)"; GENERATED=1; break
                fi
                if [ "${#ADMIN_PASSWORD}" -lt 12 ]; then
                    warn "  Too short (${#ADMIN_PASSWORD} chars, need 12)."; continue
                fi
                read -r -s -p "  Confirm:  " CONFIRM </dev/tty; echo
                [ "$ADMIN_PASSWORD" = "$CONFIRM" ] && break
                warn "  Passwords did not match."
            done
        fi
    fi
    [ "${#ADMIN_PASSWORD}" -ge 12 ] || die "Admin password must be at least 12 characters."

    [ -n "$CURRENCY" ] || CURRENCY="$(ask 'Currency label (display only)' "$(get_env_or ITAM_CURRENCY USD)")"

    if [ -z "$HTTPS" ]; then
        if ask_yn "Will this be served over HTTPS (behind a reverse proxy)?" n; then HTTPS=1; else HTTPS=0; fi
    fi

    echo
    info "--- Entra ID (optional - skip to run on demo data) ---"
    TENANT="$(get_env ENTRA_TENANT_ID)"; CLIENT="$(get_env ENTRA_CLIENT_ID)"; SECRET="$(get_env ENTRA_CLIENT_SECRET)"
    if ask_yn "Configure Entra ID user sync now?" n; then
        TENANT="$(ask '  Tenant ID' "$TENANT")"
        CLIENT="$(ask '  Client ID' "$CLIENT")"
        if [ "$ASSUME_YES" = "0" ]; then
            read -r -s -p "  Client secret: " SECRET </dev/tty; echo
        fi
        echo "  Needs Graph application permission User.Read.All with admin consent."
    fi

    umask 077
    cat > "$ENV_FILE" <<ENVEOF
# Written by setup.sh - plain KEY=value, no "export".
ITAM_PORT=$PORT

ITAM_ADMIN_USER=$ADMIN_USER
ITAM_ADMIN_PASSWORD=$ADMIN_PASSWORD
ITAM_SESSION_HOURS=12
ITAM_COOKIE_SECURE=$HTTPS

ENTRA_TENANT_ID=$TENANT
ENTRA_CLIENT_ID=$CLIENT
ENTRA_CLIENT_SECRET=$SECRET
ENTRA_USER_FILTER=

ITAM_CURRENCY=$CURRENCY
ITAM_DB=/data/itam.db
ENVEOF
    chmod 600 "$ENV_FILE"
    ok "Wrote $ENV_FILE (mode 600)"
else
    PORT="$(get_env ITAM_PORT)"; PORT="${PORT:-8000}"
    ADMIN_USER="$(get_env ITAM_ADMIN_USER)"; ADMIN_USER="${ADMIN_USER:-admin}"
fi

# --- 3. Data directory --------------------------------------------------
# The container runs as uid 10001, so the host directory must be writable by
# it. On Linux that means chowning it; some hosts (Docker Desktop, colima)
# remap ownership and need nothing. Try, then verify for real below.
mkdir -p "$DATA_DIR"
CURRENT_UID="$(stat -c '%u' "$DATA_DIR" 2>/dev/null || echo unknown)"
if [ "$CURRENT_UID" != "$DATA_UID" ]; then
    $SUDO chown -R "$DATA_UID:$DATA_UID" "$DATA_DIR" 2>/dev/null \
        && ok "Data directory $DATA_DIR chowned to uid $DATA_UID" \
        || warn "Could not chown $DATA_DIR - will verify whether it is writable anyway."
else
    ok "Data directory $DATA_DIR already owned by uid $DATA_UID"
fi

# --- 4. Build -----------------------------------------------------------
echo
info "Building the image (first time takes a minute)..."
${DOCKER_PREFIX}$COMPOSE build || die "Image build failed. The output above says why."

# Prove the container can actually write to the data directory before starting
# it, so a permission problem is a clear message rather than a crash loop.
info "Checking the container can write to $DATA_DIR..."
if ${DOCKER_PREFIX}docker run --rm -v "$(pwd)/$DATA_DIR:/data" itam:latest \
        python -c "open('/data/.probe','w').close(); import os; os.remove('/data/.probe')" 2>/dev/null; then
    ok "Data directory is writable by the container"
else
    die "The container (uid $DATA_UID) cannot write to $DATA_DIR.
  Fix it with:   sudo chown -R $DATA_UID:$DATA_UID $DATA_DIR
  Then re-run:   ./setup.sh"
fi

# --- 5. Start -----------------------------------------------------------
if [ "$NO_START" = "1" ]; then
    ok "Configuration complete. Start it with:  $DOCKER_PREFIX$COMPOSE up -d"
    exit 0
fi

echo
info "Starting..."
${DOCKER_PREFIX}$COMPOSE up -d

echo
info "Waiting for the app to come up..."
URL="http://127.0.0.1:$PORT"
for _ in $(seq 1 60); do
    if curl -fsS -o /dev/null "$URL/healthz" 2>/dev/null; then
        HEALTHY=1; break
    fi
    sleep 1
done

echo
if [ "${HEALTHY:-0}" = "1" ]; then
    ok "=============================================="
    ok "  ITAM is running at $URL"
    ok "=============================================="
    echo
    echo "  Sign in as:  $ADMIN_USER"
    if [ "${GENERATED:-0}" = "1" ]; then
        echo "  Password:    $ADMIN_PASSWORD"
        warn "  ^ generated for you - it is also in .env. Change it after signing in."
    else
        echo "  Password:    the one you just set"
    fi
    echo
    echo "  Logs:     ${DOCKER_PREFIX}$COMPOSE logs -f"
    echo "  Stop:     ${DOCKER_PREFIX}$COMPOSE down"
    echo "  Update:   git pull && ${DOCKER_PREFIX}$COMPOSE up -d --build"
    echo "  Backup:   ./backup.sh"
    echo
    if command -v ufw >/dev/null 2>&1 && $SUDO ufw status 2>/dev/null | grep -q "Status: active"; then
        warn "  ufw is active. To reach this from other machines:  sudo ufw allow $PORT/tcp"
    fi
    if [ "$(get_env ENTRA_TENANT_ID)" = "" ]; then
        echo "  Entra ID is not configured - the app is running on demo data."
        echo "  Add credentials to .env, then: ${DOCKER_PREFIX}$COMPOSE up -d"
    fi
else
    warn "The app did not answer on $URL/healthz in time."
    echo "  Check the logs:  ${DOCKER_PREFIX}$COMPOSE logs --tail=50"
    exit 1
fi
