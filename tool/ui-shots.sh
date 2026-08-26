#!/usr/bin/env bash
#
# Look at the landing instead of guessing at it: renders / and /es/ across
# viewports and colour schemes, writes PNGs, and reports the things a PNG
# cannot show — console errors, failed requests, horizontal overflow.
#
#   tool/ui-shots.sh                       # both locales, three widths, light
#   SHOTS_VIEWPORTS=mobile SHOTS_SCHEMES=light,dark tool/ui-shots.sh
#   SHOTS_BASE=https://sonadose.com tool/ui-shots.sh   # against production
#
# Env: SHOTS_LOCALES (en,es) SHOTS_VIEWPORTS (mobile,tablet,desktop)
#      SHOTS_SCHEMES (light,dark) SHOTS_OUT (build/ui-shots)
#      SHOTS_FULL_PAGE=1 for the whole scrollable page
#      SHOTS_SELECTOR=".hero" to shoot one component instead of the viewport
#      SHOTS_CHANNEL=chrome (system Chrome; unset falls back to bundled)
#
# Deliberately adds no dependency to this repository: Playwright is borrowed
# from the npx cache. `npx playwright --version` once is enough to fill it.
set -uo pipefail
cd "$(dirname "$0")/.."

PORT="${SHOTS_PORT:-8765}"
BASE="${SHOTS_BASE:-http://127.0.0.1:$PORT}"
server_pid=""

# Only run our own server when shooting our own port and nothing is there yet.
if [ "$BASE" = "http://127.0.0.1:$PORT" ] && ! curl -sf -o /dev/null "$BASE/"; then
    python3 -m http.server "$PORT" --bind 127.0.0.1 >/dev/null 2>&1 &
    server_pid=$!
    trap 'kill "$server_pid" 2>/dev/null' EXIT
    for _ in $(seq 1 40); do
        curl -sf -o /dev/null "$BASE/" && break
        sleep 0.25
    done
fi

# Playwright lives in the npx cache, not in this repository. Newest wins.
pw_dir="$(find "$HOME/.npm/_npx" -maxdepth 4 -type d -path '*/node_modules/playwright' \
          -printf '%T@ %p\n' 2>/dev/null | sort -rn | head -1 | cut -d' ' -f2-)"
if [ -z "$pw_dir" ]; then
    echo "Playwright not in the npx cache. Run once:  npx playwright --version" >&2
    exit 69
fi

echo "Rendering $BASE  (playwright: $pw_dir)"
SHOTS_BASE="$BASE" NODE_PATH="$(dirname "$pw_dir")" node tool/ui-shots.cjs
