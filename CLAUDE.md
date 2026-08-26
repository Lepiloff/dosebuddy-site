# Sonadose — site, API, deployment

Orientation lives in [`README.md`](README.md): what is here, where it runs, how
it ships. This file is only the part an agent gets wrong without being told.

This repository is written in English — README, docs, comments, commits. (The
Android repository next door, `~/Projects/own/dosebuddy`, is documented in
Russian. Follow each repository's own convention.)

## Three things in one repository

`index.html`, `es/`, `css/`, `js/`, `img/`, `fonts/` — the landing.
`api/` — the FastAPI backend the Android app syncs against.
`deploy/` — nginx, certbot, compose, and the script that ships all of it.

**A UI task touches the first group only.** The contract in
`docs/v1.1-api-contract.md` is agreed with the app track: changing it is a
negotiation, not an edit.

## The landing is static on purpose

Hand-written HTML, CSS and vanilla JS. No framework, no build step, no
dependencies — served exactly as committed. That is a working end state, not
technical debt, and a visual redesign is not a reason to change it. Introducing
a build step needs a maintenance argument the current setup cannot answer.

`css/site.css` is the design system: tokens in `:root`, `@layer reset, base,
layout, components, utilities`. Read its header comment before adding anything —
it states the visual direction ("Pastillero") and why the colour roles are split
the way they are.

## Working locally

```bash
python3 -m http.server 8765          # / and /es/
tool/ui-shots.sh                     # render both locales, report what a PNG cannot
./tool/prepare-assets.sh             # regenerate img/ and fonts/ from sources
```

`tool/ui-shots.sh` writes PNGs to `build/ui-shots/` (git-ignored) and reports
console errors, failed requests, horizontal overflow and CSP violations. Look at
the PNGs — a successful render is not a good one.

## What breaks quietly

⚠️ **A push to `main` deploys to production.** `.github/workflows/deploy.yml`
fires on any change under `index.html`, `es/`, `css/`, `js/`, `img/`, `fonts/`,
`robots.txt`, `sitemap.xml`, `api/` or `deploy/`. There is no staging step
between the commit and sonadose.com. `deploy/verify.sh` is the acceptance test.

⚠️ **The production CSP forbids inline styles, and the local preview does not.**
`style-src 'self'` in `deploy/nginx/snippets/landing.conf` drops every `<style>`
block and every `style="…"` attribute. `python3 -m http.server` sends no CSP, so
the page looks correct locally and loses that styling in production, silently.
`tool/ui-shots.sh` puts the real header back on the local response — that is
what it is for. Styling belongs in `css/site.css`.

⚠️ **`script-src` allows exactly one inline script, by hash.** That is the
anti-CLS language hint in the `<head>` of each page. Edit it and the hash in
`landing.conf` must be regenerated, or the deploy fails the CSP check (and, if
it did not, the browser would silently drop the script). `tool/ui-shots.sh`
warns about this before the push; `deploy.yml` is the authority.

⚠️ **`deploy/site-include.txt` is an allow-list.** A new top-level directory is
not published until it is named there. The failure mode is "my file is missing",
never "my file is public" — `docs/` was served publicly for a day before this
was inverted. Do not add `tool/` or `build/` to it.

⚠️ **URLs must not change.** Identical paths are what made the move off GitHub
Pages, and later the move to a new domain, cost nothing in search. `/es` must
301 to `/es/`; `/img/` and `/css/` must 404, not 403. All of it is asserted in
`deploy/verify.sh`.

## What must stay true

[`docs/landing.md`](docs/landing.md) is the list, with the reasoning: fonts
self-hosted (never the Google CDN), the brand teal is a surface colour and never
a text colour, analytics loads only after consent and both buttons carry equal
weight, no medical claims in either language, and the FAQ exists twice — visible
`<details>` and `FAQPage` JSON-LD must agree.

Read it before a redesign. Every line of it was paid for.

## UI work

Invoke the `product-ui` skill (`.claude/skills/product-ui/`). It carries the
process; this file carries the facts.
