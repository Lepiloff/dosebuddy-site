#!/usr/bin/env bash
#
# The migration's acceptance test: does this host behave the way GitHub Pages
# behaved? Baseline captured from the live Pages edge on 2026-08-09.
#
#   ./verify.sh new.dosebuddyapp.com 1.2.3.4   rehearsal, before the DNS cutover
#   ./verify.sh dosebuddyapp.com                after the cutover
#
# The second argument pins DNS for the run, so the production hostnames can be
# exercised against the new box while the world still resolves them to Pages.
#
# Exit code is the answer: 0 means the move is invisible from outside.
#
# Run against Pages today and two checks fail, both on purpose: Pages serves
# /README.md and /tool/prepare-assets.sh with a 200, because it publishes the
# whole repository. The new host serves only the site. See deploy/README.md.
#
#   INSECURE=1 ./verify.sh ... — accept a self-signed certificate and skip the
# chain check. For trying the config on a laptop before there is an EC2 box; it
# must never be how the rehearsal or the cutover is signed off.

set -uo pipefail

HOST="${1:-}"
PIN="${2:-}"

if [ -z "$HOST" ]; then
    echo "usage: $0 <host> [ip-to-pin]" >&2
    exit 64
fi

APEX="dosebuddyapp.com"
WWW="www.dosebuddyapp.com"

CURL_TLS=()
[ "${INSECURE:-0}" = "1" ] && CURL_TLS=(-k)

pass=0
fail=0

# curl args that pin a hostname to the box under test. Without this the
# production names would resolve to whatever DNS currently says.
resolve() {
    [ -n "$PIN" ] && printf -- '--resolve\n%s:%s:%s\n' "$1" "$2" "$PIN"
}

# expect <url> <status> [location] [content-type-substring]
expect() {
    local url="$1" want_status="$2" want_loc="${3:-}" want_ct="${4:-}"
    local host port out got_status got_ct got_loc problems=""

    host=$(printf '%s' "$url" | sed -E 's#^https?://([^/]+).*#\1#')
    case "$url" in https://*) port=443 ;; *) port=80 ;; esac

    mapfile -t extra < <(resolve "$host" "$port")

    out=$(curl -sS "${CURL_TLS[@]}" "${extra[@]}" -o /dev/null \
              -w '%{http_code}|%{content_type}|%{redirect_url}' \
              --max-time 20 "$url" 2>&1) || {
        printf '  FAIL  %-58s curl: %s\n' "$url" "$out"
        fail=$((fail + 1))
        return
    }

    got_status=${out%%|*}
    got_ct=$(printf '%s' "$out" | cut -d'|' -f2)
    got_loc=$(printf '%s' "$out" | cut -d'|' -f3)

    [ "$got_status" = "$want_status" ] || problems+=" status=$got_status(want $want_status)"
    [ -n "$want_loc" ] && [ "$got_loc" != "$want_loc" ] && problems+=" location=$got_loc(want $want_loc)"
    [ -n "$want_ct" ] && case "$got_ct" in *"$want_ct"*) ;; *) problems+=" type=$got_ct(want $want_ct)" ;; esac

    if [ -z "$problems" ]; then
        printf '  ok    %-58s %s\n' "$url" "$want_status"
        pass=$((pass + 1))
    else
        printf '  FAIL  %-58s%s\n' "$url" "$problems"
        fail=$((fail + 1))
    fi
}

echo
echo "Verifying https://$HOST${PIN:+  (pinned to $PIN)}"

echo
echo "Pages, both locales"
expect "https://$HOST/"            200 "" "text/html"
expect "https://$HOST/index.html"  200 "" "text/html"
expect "https://$HOST/es/"         200 "" "text/html"

# The one that catches a `try_files $uri $uri/` in the config: that would answer
# 200 here and leave /es and /es/ both serving the page.
echo
echo "Directory handling"
expect "https://$HOST/es"          301 "https://$HOST/es/"
expect "https://$HOST/img/"        404
expect "https://$HOST/css/"        404

echo
echo "Not found"
expect "https://$HOST/nope"        404 "" "text/html"
expect "https://$HOST/es/nope"     404 "" "text/html"
expect "https://$HOST/404.html"    200 "" "text/html"

echo
echo "Crawling"
expect "https://$HOST/robots.txt"  200 "" "text/plain"
expect "https://$HOST/sitemap.xml" 200 "" "xml"

echo
echo "Assets"
expect "https://$HOST/css/site.css"                            200 "" "text/css"
expect "https://$HOST/js/consent.js"                           200 "" "javascript"
expect "https://$HOST/js/ui.js"                                200 "" "javascript"
expect "https://$HOST/fonts/atkinson-hyperlegible-next-latin.woff2" 200
expect "https://$HOST/img/mark.svg"                            200 "" "svg"
expect "https://$HOST/img/og-en.jpg"                           200 "" "image/jpeg"

# Nothing from the repository that is not the site. These are excluded from the
# web root by deploy/site-exclude.txt, not blocked in nginx, so this is the test
# that the exclude list actually took effect.
echo
echo "Repository internals must not be served"
expect "https://$HOST/deploy/docker-compose.yml" 404
expect "https://$HOST/deploy/verify.sh"          404
expect "https://$HOST/README.md"                 404
expect "https://$HOST/tool/prepare-assets.sh"    404
expect "https://$HOST/.git/config"               404
# Internal design notes. These were served from the live domain for a day
# because the web root was built from a blocklist and nobody extended it.
expect "https://$HOST/docs/v1.1-api-contract.md"      404
expect "https://$HOST/docs/v1.1-contract-decisions.md" 404

# http -> https, and www -> apex. GitHub collapsed http://www straight to the
# https apex in one hop; two hops would still work but would spend a redirect
# that used to be free.
#
# Over plain http these can be checked before the cutover, because no
# certificate is involved: the request is pinned to the box by IP and routed by
# the Host header alone.
echo
echo "Redirects"
expect "http://$APEX/"  301 "https://$APEX/"
expect "http://$WWW/"   301 "https://$APEX/"
expect "http://$APEX/es/faq" 301 "https://$APEX/es/faq"

if [ "$HOST" = "$APEX" ]; then
    # Needs the production certificate, so it only runs post-cutover: pinning
    # www to the box before then would present the rehearsal certificate and
    # fail validation for the wrong reason.
    expect "https://$WWW/" 301 "https://$APEX/"
else
    echo "  skip  https://$WWW/ -> apex           (needs the production certificate)"
fi

echo
echo "Certificate"
if [ "${INSECURE:-0}" = "1" ]; then
    echo "  skip  chain verification                                 (INSECURE=1)"
elif chain=$(echo | openssl s_client -connect "${PIN:-$HOST}:443" -servername "$HOST" 2>/dev/null); then
    if printf '%s' "$chain" | grep -q "Verify return code: 0 (ok)"; then
        printf '  ok    %-58s %s\n' "chain verifies" \
            "$(printf '%s' "$chain" | sed -n 's/^subject=.*CN *= *//p' | head -1)"
        pass=$((pass + 1))
    else
        printf '  FAIL  %-58s %s\n' "chain does not verify" \
            "$(printf '%s' "$chain" | grep 'Verify return code' | head -1)"
        fail=$((fail + 1))
    fi
else
    printf '  FAIL  %s\n' "could not open a TLS connection"
    fail=$((fail + 1))
fi

# Served bytes against the bytes in the repository. Catches a stale rsync, a
# half-finished deploy, or nginx pointed at the wrong root.
if [ -f index.html ]; then
    echo
    echo "Content matches the repository"
    for pair in "/:index.html" "/es/:es/index.html" "/404.html:404.html"; do
        url_path=${pair%%:*}
        file=${pair##*:}
        mapfile -t extra < <(resolve "$HOST" 443)
        if curl -sS "${CURL_TLS[@]}" "${extra[@]}" --max-time 20 "https://$HOST$url_path" | diff -q - "$file" >/dev/null 2>&1; then
            printf '  ok    %-58s identical\n' "$url_path"
            pass=$((pass + 1))
        else
            printf '  FAIL  %-58s differs from %s\n' "$url_path" "$file"
            fail=$((fail + 1))
        fi
    done
fi

echo
echo "----------------------------------------------------------------"
printf 'passed %d, failed %d\n' "$pass" "$fail"
[ "$fail" -eq 0 ] || exit 1
