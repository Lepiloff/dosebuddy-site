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
#
#   PREPARE_ONLY=1 ./init-cert.sh ...   place the placeholders, start nginx, stop
#                                  there. Lets the site be proved on the box over
#                                  localhost before anything depends on DNS, a
#                                  security group, or a Let's Encrypt request.

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

# Every look at /etc/letsencrypt goes through here, as root inside a container.
#
# Not fussiness. Once certbot has issued a certificate, live/<name>/*.pem are
# symlinks into archive/, and archive/ is root-only 0700. A `test -f` run as
# ubuntu then fails to traverse it and reports "no certificate" for one that
# plainly exists — after which this script would helpfully write a placeholder
# straight through those symlinks and destroy the real thing. It did exactly
# that once before this was fixed.
as_root() {
    in_certbot --entrypoint sh certbot/certbot -c "$1" >/dev/null 2>&1
}

cert_exists() { as_root "test -f /etc/letsencrypt/live/$1/fullchain.pem"; }
is_placeholder() { as_root "test -f /etc/letsencrypt/live/$1/.dummy"; }

# "Real" means trusted by browsers, which rules out two impostors: the
# placeholder this script writes, and a staging certificate. Staging is the
# dangerous one — it is a complete, valid-looking certificate that no browser
# trusts, and without this check the documented "staging first, then for real"
# sequence would end with the second run reporting nothing to do and leaving
# the untrusted certificate serving.
is_staging() { as_root "grep -q acme-staging /etc/letsencrypt/renewal/$1.conf"; }

is_real() {
    cert_exists "$1" || return 1
    is_placeholder "$1" && return 1
    is_staging "$1" && return 1
    return 0
}

place_dummy() {
    local name="$1"
    as_root "mkdir -p /etc/letsencrypt/live/$name \
             && openssl req -x509 -nodes -newkey rsa:2048 -days 1 \
                  -keyout /etc/letsencrypt/live/$name/privkey.pem \
                  -out /etc/letsencrypt/live/$name/fullchain.pem \
                  -subj /CN=$name \
             && touch /etc/letsencrypt/live/$name/.dummy"
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
    elif cert_exists "$name"; then
        echo "    $name already has something nginx can load"
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

if [ "${PREPARE_ONLY:-0}" = "1" ]; then
    echo
    echo "PREPARE_ONLY: nginx is up on placeholder certificates and nothing was"
    echo "requested. The site can be checked on the box now:"
    echo "  curl -sk --resolve dosebuddyapp.com:443:127.0.0.1 https://dosebuddyapp.com/ | head"
    echo "Re-run without PREPARE_ONLY once DNS and the security group are ready."
    exit 0
fi

if is_staging "$PRIMARY" && [ "${STAGING:-0}" != "1" ]; then
    echo "==> Discarding the staging certificate for $PRIMARY"
fi

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
echo "  $COMPOSE run --rm -T --entrypoint certbot certbot renew --dry-run --no-random-sleep-on-renew"
