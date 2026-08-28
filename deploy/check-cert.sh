#!/usr/bin/env bash
#
# Days left on a host's certificate — and a failure while there is still time to
# do something about it.
#
#   ./check-cert.sh api.sonadose.com
#   ./check-cert.sh api.sonadose.com 14
#
# Why this exists next to verify.sh, which already checks a certificate: the
# monitor runs verify.sh against the landing, so that host gets about nine days
# of warning. The api host had none. The only thing watching it was a curl of
# /health, which starts failing the moment the certificate expires rather than
# before — no notice on the host that carries sync and caregiver alerts.
#
# What an expired certificate here does and does not do, because the difference
# decides how fast anyone needs to move: sync and caregiver alerts stop; the
# reminders on the phone do not, they are local and never touch this host.
#
# The threshold is duplicated from verify.sh rather than shared. Collapsing them
# into one definition means editing the acceptance test the migration was signed
# off with, which is not a thing to do in the same week as a release. Two
# numbers that must agree is a small debt, and it is written down here because
# the next person to change one of them will read this file.

set -uo pipefail

HOST="${1:-}"

# certbot renews at 30 days remaining. Failing at 21 leaves three weeks to
# notice a renewal that has quietly stopped working — a certificate nobody is
# renewing verifies perfectly right up until the hour it expires.
THRESHOLD="${2:-21}"

if [ -z "$HOST" ]; then
    echo "usage: $0 <host> [days]" >&2
    exit 64
fi

if ! chain=$(echo | openssl s_client -connect "$HOST:443" -servername "$HOST" 2>/dev/null); then
    printf 'FAIL  could not open a TLS connection to %s\n' "$HOST" >&2
    exit 1
fi

end=$(printf '%s' "$chain" | openssl x509 -noout -enddate 2>/dev/null | cut -d= -f2)
if [ -z "$end" ]; then
    printf 'FAIL  could not read an expiry date from %s\n' "$HOST" >&2
    exit 1
fi

left=$(( ( $(date -d "$end" +%s) - $(date +%s) ) / 86400 ))
if [ "$left" -lt "$THRESHOLD" ]; then
    printf 'FAIL  %s has %s days left — renewal has not run\n' "$HOST" "$left" >&2
    exit 1
fi

printf 'ok    %s, %s days left\n' "$HOST" "$left"
