---
name: copywriting
description: Writing for sonadose.com: guides and articles (/guides/, /es/guias/), landing copy, FAQ answers, meta titles and descriptions. Load for any task that writes, rewrites or translates text on this site, SEO copy, an article, a guide or a blog post.
---

# Copy for sonadose.com

**The rules live in the app repository:**
`~/Projects/own/dosebuddy/.claude/skills/copywriting/SKILL.md` (in Russian).
Read it first. It covers voice ("the Sonadose team", we / nosotros), what every
text needs (the answer first, one first-hand detail, an honest limit), the
claims rules (`~/Projects/own/dosebuddy/docs/12-landing-claims.md`), the
blacklist of synthetic-text markers (no em dash at all), rhythm, SEO and the
review order. One copy of the rules, so the store listing and the site cannot
drift apart.

This file adds only what is specific to this repository.

## Where articles live

- Spanish: `es/guias/<slug>/index.html`, hub `es/guias/index.html`.
- English: `guides/<slug>/index.html`, hub `guides/index.html`. A new top-level
  directory, so it must be in `deploy/site-include.txt` and in the `paths` of
  `.github/workflows/deploy.yml`, or it is silently not published.
- Folder URLs with a trailing slash; nginx 301s `/x` to `/x/`. A published URL
  never changes.
- `hreflang` only between a true pair (the same article in both languages);
  otherwise a self-canonical and nothing else.

## Page contract

- Same header, footer, disclaimer and consent banner as the landing; no
  language hint bar and **no inline `<script>`**, so the CSP hash stays as it is.
  JSON-LD (`application/ld+json`) is data, not script, and is fine.
- `Article` JSON-LD with `datePublished`/`dateModified`; the visible eyebrow
  carries the same date.
- Every Play link carries UTM: `utm_source=sonadose.com`, `utm_medium=article`,
  `utm_campaign=<lang>_<slug-ish>` (scheme: `~/Projects/own/dosebuddy/docs/growth/utm-links.md`).
- A short note before the call to action: the guide is not medical advice.
- Styling only through the `.article*` and `.guide-list` classes in
  `css/site.css`; no inline styles (the production CSP drops them).
- Add the URL to `sitemap.xml` and to the hub page.

## Checks

```bash
SHOTS_PAGES="g=/es/guias/<slug>/" SHOTS_LOCALES=g \
  SHOTS_VIEWPORTS=mobile,desktop SHOTS_SCHEMES=light,dark tool/ui-shots.sh
grep -nE '—|cabe destacar|en el mundo actual|sin duda|in today|dive in|seamless' es/guias/<slug>/index.html
```

Open the PNGs. Spanish text is reviewed by a native speaker before it goes to
`main`; a push to `main` deploys.
