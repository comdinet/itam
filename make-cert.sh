#!/usr/bin/env bash
# Generate the self-signed certificate ITAM serves.
#
#   ./make-cert.sh                       names taken from ITAM_SITE_ADDRESS in .env
#   ./make-cert.sh itam.example.com itam 10.0.0.5
#
# Valid for 10 years. Re-run to add names, then: docker compose restart caddy
set -euo pipefail
cd "$(dirname "$0")"

CERT_DIR="./certs"
DAYS=3650

names=("$@")
if [ ${#names[@]} -eq 0 ]; then
    [ -f .env ] || { echo "No .env and no names given." >&2; exit 1; }
    raw="$(sed -n 's/^ITAM_SITE_ADDRESS=//p' .env | head -1)"
    [ -n "$raw" ] || { echo "ITAM_SITE_ADDRESS is not set in .env." >&2; exit 1; }
    IFS=',' read -r -a parts <<< "$raw"
    for p in "${parts[@]}"; do
        p="$(echo "$p" | tr -d '[:space:]')"
        [ -n "$p" ] && names+=("$p")
    done
fi

# Always usable from the box itself.
names+=("localhost" "127.0.0.1")

san=""
primary=""
for n in "${names[@]}"; do
    [ -z "$primary" ] && primary="$n"
    if [[ "$n" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
        entry="IP:$n"
    else
        entry="DNS:$n"
    fi
    case ",$san," in *",$entry,"*) continue ;; esac
    san="${san:+$san,}$entry"
done

mkdir -p "$CERT_DIR"
openssl req -x509 -nodes -newkey rsa:2048 \
    -keyout "$CERT_DIR/itam.key" -out "$CERT_DIR/itam.crt" \
    -days "$DAYS" -subj "/CN=$primary" -addext "subjectAltName=$san" \
    -addext "basicConstraints=CA:FALSE" \
    -addext "keyUsage=digitalSignature,keyEncipherment" \
    -addext "extendedKeyUsage=serverAuth" 2>/dev/null

chmod 644 "$CERT_DIR/itam.crt"
chmod 640 "$CERT_DIR/itam.key"
echo "Wrote $CERT_DIR/itam.crt covering:"
openssl x509 -in "$CERT_DIR/itam.crt" -noout -ext subjectAltName | tail -n +2 | sed 's/^ */  /'
echo "Valid until: $(openssl x509 -in "$CERT_DIR/itam.crt" -noout -enddate | cut -d= -f2)"
