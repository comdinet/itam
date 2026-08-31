# Caddy with the Cloudflare DNS module.
#
# The official caddy image has no DNS providers in it - they are Go plugins,
# compiled in. So a real certificate for itam.remedio.io, obtained by writing a
# TXT record instead of by exposing port 80 to the internet, needs this one
# extra build. It runs once and the result is cached.
FROM caddy:2-builder-alpine AS builder
RUN xcaddy build --with github.com/caddy-dns/cloudflare

FROM caddy:2-alpine
COPY --from=builder /usr/bin/caddy /usr/bin/caddy
