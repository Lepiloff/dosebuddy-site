#!/usr/bin/env bash
#
# Issue the first Let's Encrypt certificate for a hostname on this box.
#
#   ./init-cert.sh new.dosebuddyapp.com                          # rehearsal
#   ./init-cert.sh dosebuddyapp.com www.dosebuddyapp.com         # cutover
#
# Renewals after this are the certbot sidecar's job and need no help.
#
# The awkward part it exists to solve: nginx refuses to start when a config
# references a certificate file that is not there yet, but certbot's HTTP-01
# challenge needs nginx already answering on port 80. The way out is a throwaway
# self-signed certificate at the path nginx expects, just long enough to get the
# real one.
#
# Note "every path", not "the one being issued". The config names more than one
# certificate — production and rehearsal today, the api later — and a single
# missing file keeps nginx down, so all of them get a placeholder before nginx
# is asked to start.
#
#   STAGING=1 ./init-cert.sh ...   use Let's Encrypt staging. Worth doing on the
#                                  first run of a new hostname: production has a
#                                  limit of 5 identical certificates per week,
#                                  and a typo can burn through it.

set -euo pipefail

cd "$(dirname "$0")"

if [ "$#" -lt 1 ]; then
    echo "usage: $0 <primary-domain> [extra-domain ...]" >&2
    exit 64
fi

PRIMARY="$1"
EMAIL="${LETSENCRYPT_EMAIL:-lepiloff82@gmail.com}"
COMPOSE="docker compose -f docker-compose.yml"
CONF_DIR="data/certbot/conf"

CERT_ARGS=()
for d in "$@"; do
    CERT_ARGS+=(-d "$d")
done
[ "${STAGING:-0}" = "1" ] && CERT_ARGS+=(--staging)

in_certbot() {
    docker run --rm -v "$PWD/$CONF_DIR:/etc/letsencrypt" "$@"
}

is_real() {
    [ -f "$CONF_DIR/live/$1/fullchain.pem" ] && [ ! -f "$CONF_DIR/live/$1/.dummy" ]
}

place_dummy() {
    local name="$1"
    mkdir -p "$CONF_DIR/live/$name"
    in_certbot --entrypoint openssl certbot/certbot \
        req -x509 -nodes -newkey rsa:2048 -days 1 \
            -keyout "/etc/letsencrypt/live/$name/privkey.pem" \
            -out "/etc/letsencrypt/live/$name/fullchain.pem" \
            -subj "/CN=$name" 2>/dev/null
    touch "$CONF_DIR/live/$name/.dummy"
    echo "    placeholder for $name"
}

if is_real "$PRIMARY"; then
    echo "A certificate for $PRIMARY already exists. Nothing to do."
    echo "To replace it: $COMPOSE run --rm --entrypoint certbot certbot renew --force-renewal"
    exit 0
fi

mkdir -p "$CONF_DIR" data/certbot/www

echo "==> Making sure nginx can start: placeholders for every certificate it names"
REFERENCED=$(grep -hoE '/etc/letsencrypt/live/[^/]+/' nginx/conf.d/*.conf \
             | sed -E 's#.*/live/([^/]+)/#\1#' | sort -u)
for name in $REFERENCED; do
    if is_real "$name"; then
        echo "    $name already has a real certificate"
    elif [ -f "$CONF_DIR/live/$name/fullchain.pem" ]; then
        echo "    $name already has a placeholder"
    else
        place_dummy "$name"
    fi
done

echo "==> Starting nginx"
$COMPOSE up -d nginx
for _ in $(seq 30); do
    $COMPOSE exec -T nginx nginx -t >/dev/null 2>&1 && break
    sleep 1
done
$COMPOSE exec -T nginx nginx -t

echo "==> Clearing the placeholder for $PRIMARY"
in_certbot --entrypoint sh certbot/certbot -c \
    "rm -rf /etc/letsencrypt/live/$PRIMARY /etc/letsencrypt/archive/$PRIMARY /etc/letsencrypt/renewal/$PRIMARY.conf"

echo "==> Asking Let's Encrypt for the real certificate"
$COMPOSE run --rm --entrypoint certbot certbot certonly \
    --webroot -w /var/www/certbot \
    "${CERT_ARGS[@]}" \
    --email "$EMAIL" \
    --agree-tos --no-eff-email \
    --non-interactive

echo "==> Reloading nginx"
$COMPOSE exec -T nginx nginx -t
$COMPOSE exec -T nginx nginx -s reload

echo
echo "Done. Confirm renewal is wired up before walking away:"
echo "  $COMPOSE run --rm --entrypoint certbot certbot renew --dry-run"
