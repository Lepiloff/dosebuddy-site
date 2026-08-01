#!/usr/bin/env bash
#
# Regenerates every derived asset in this repo from its source of truth.
# Sources live in the app repo (screenshots) or on the open web (font, Play badge).
# Everything it writes is committed, so the site stays a pure static checkout.
#
# Requires: imagemagick (convert, identify), curl, google-chrome (for OG images).
set -euo pipefail
cd "$(dirname "$0")/.."

APP_REPO=${APP_REPO:-../dosebuddy}
UA='Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36'

# --- 1. Self-hosted font ------------------------------------------------------
# Atkinson Hyperlegible Next (SIL OFL 1.1), variable 400..700, latin subset only.
# Self-hosted on purpose: loading it from the Google Fonts CDN would leak visitor
# IPs to Google before consent, which is not lawful for EU visitors under GDPR.
fetch_font() {
  curl -sS -o fonts/atkinson-hyperlegible-next-latin.woff2 \
    "https://fonts.gstatic.com/s/atkinsonhyperlegiblenext/v7/NaPNcYPdHfdVxJw0IfIP0lvYFqijb-UxCtm5_wdGseiJn3o.woff2"
  curl -sS -o fonts/OFL.txt \
    "https://raw.githubusercontent.com/googlefonts/atkinson-hyperlegible-next/main/OFL.txt"
}

# --- 2. Official Google Play badges -------------------------------------------
# Must be Google's own artwork; the transparent margin is the mandated clear space.
fetch_badges() {
  curl -sSL -A "$UA" -o img/badges/en.png \
    "https://play.google.com/intl/en_us/badges/static/images/badges/en_badge_web_generic.png"
  curl -sSL -A "$UA" -o img/badges/es.png \
    "https://play.google.com/intl/es_es/badges/static/images/badges/es_badge_web_generic.png"
  # Re-encoded to WebP at 2x of the 248px render box. Only the container format
  # and resolution change, the artwork itself is never recoloured, cropped or
  # re-proportioned, which is what Google's badge guidelines forbid.
  for lang in en es; do
    convert "img/badges/$lang.png" -resize 512x -strip -define webp:alpha-quality=100 \
      -quality 90 "img/badges/$lang.webp"
  done
}

# --- 3. Screenshots -----------------------------------------------------------
# Source: 1080x2120 PNG from the app repo. Emitted at 640w (2x) and 320w (1x) WebP.
build_screens() {
  local src="$APP_REPO/docs/screenshots"
  [ -d "$src" ] || { echo "Screenshots not found at $src (set APP_REPO)"; exit 1; }
  for lang in en es; do
    for name in today elder calendar medications wizard; do
      convert "$src/$lang/$name.png" -resize 640x -strip -quality 82 \
        "img/screens/$lang-$name-640.webp"
      convert "$src/$lang/$name.png" -resize 320x -strip -quality 80 \
        "img/screens/$lang-$name-320.webp"
    done
  done
}

# --- 4. Touch icon ------------------------------------------------------------
# favicon.svg is authored by hand; the PNG is only for iOS home screens.
build_icons() {
  convert -background none img/mark.svg -resize 180x180 -strip img/apple-touch-icon.png
}

# --- 5. Open Graph images -----------------------------------------------------
# Rendered from tool/og-template.html through headless Chrome so the card uses the
# same self-hosted typeface as the site instead of a substitute.
build_og() {
  for lang in en es; do
    google-chrome --headless=new --disable-gpu --hide-scrollbars \
      --window-size=1200,630 --screenshot="img/og-$lang.png" \
      --default-background-color=00000000 \
      "file://$PWD/tool/og-template.html?lang=$lang" >/dev/null 2>&1
    convert "img/og-$lang.png" -strip -quality 90 "img/og-$lang.jpg"
    rm -f "img/og-$lang.png"
  done
}

case "${1:-all}" in
  font)    fetch_font ;;
  badges)  fetch_badges ;;
  screens) build_screens ;;
  icons)   build_icons ;;
  og)      build_og ;;
  all)     fetch_font; fetch_badges; build_screens; build_icons; build_og ;;
  *)       echo "usage: $0 [all|font|badges|screens|icons|og]"; exit 1 ;;
esac
echo "done: ${1:-all}"
