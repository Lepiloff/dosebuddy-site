---
name: product-ui
description: The Sonadose landing page — visual design, layout, typography, colour, responsive behaviour, accessibility, hero and section work on index.html / es/index.html / css/site.css. Load for any UI, design, styling, layout, CSS, dark-mode, mobile-width or redesign task on this site.
---

# Landing UI workflow

One process across two repositories: the landing here, the Flutter app in
`~/Projects/own/dosebuddy` (its own `CLAUDE.md`, its own copy of this skill).
The brand is shared; the implementation is native to each platform. **Shared
direction, not shared files.**

## Rule one

**Look at the result, not at the diff.** A page that loads is not a page that
works. The task is not finished until the render has been opened and criticised.

## The loop

1. **Read what you are changing.** The section in `index.html`, its Spanish twin
   in `es/index.html`, and the relevant `@layer` in `css/site.css`. The CSS
   header comment states the visual direction — understand it before replacing
   it.
2. **Shoot a baseline** with `tool/ui-shots.sh` before editing. Without "before"
   there is no "better", only "different".
3. **Choose one direction**, justified by the product: the reader is often an
   adult child deciding for an elderly parent, frequently on a phone, and the
   page is set in a typeface drawn for low-vision readers. Small and elegant is
   a defect here.
4. **Make the smallest coherent change** that achieves it. Styling goes in
   `css/site.css` — never inline (the production CSP drops it).
5. **Shoot again and look.** Clipping, cramped rhythm, broken hierarchy,
   unreadable contrast, a CTA below the fold, the consent banner covering
   something that matters.
6. **Fix and repeat.** One critique pass is not a loop.
7. **Both languages, every time.** Spanish strings are longer; `/es/` breaks
   first, and it is a separate file that does not inherit English edits.
8. **Report evidence**: commands run, what the renders show, what is still
   unverified.

## How to look

```bash
tool/ui-shots.sh                                      # en + es, three widths, light
SHOTS_VIEWPORTS=mobile SHOTS_SCHEMES=light,dark tool/ui-shots.sh
SHOTS_SELECTOR=".hero" tool/ui-shots.sh               # one component, large
SHOTS_FULL_PAGE=1 SHOTS_VIEWPORTS=desktop tool/ui-shots.sh   # whole composition
SHOTS_BASE=https://sonadose.com tool/ui-shots.sh      # what is live right now
```

PNGs land in `build/ui-shots/` (git-ignored) — open them, do not just generate
them. The script also reports what a PNG cannot show: console errors, failed
requests, horizontal overflow, and CSP violations that the local preview would
otherwise hide.

For anything interactive — the consent banner, the language hint, the FAQ
`<details>`, the dormant video facade — drive a real browser (`playwright-skill`
or `claude-in-chrome`; Chrome and Playwright are both on this machine). A
screenshot cannot tell you a click works.

## Minimum checks before calling it done

- Desktop **and** 360px-wide mobile, both `/` and `/es/`.
- Light and dark (`prefers-color-scheme` is honoured by the tokens).
- No horizontal overflow at any width.
- No new console errors, no CSP violations.
- Consent banner still appears, both buttons still equally weighted, analytics
  still does not load before Accept.
- Play Store links and both language links still work.
- Real assets in place — no placeholder screenshots left in `img/`.

## Architecture decisions

| Observation | What to do |
|---|---|
| The change fits in `css/site.css` and the two HTML files | Do that. It is the default and usually the answer |
| The CSS has become hard to follow during the redesign | Reorganise within `@layer`. Keep zero build complexity |
| Content or components now genuinely repeat | Only then is a static-site generator worth arguing for — with the decision record below |
| A framework (React / Next / Astro) feels nicer | Not a reason. The current stack is a working end state |
| The change touches `api/` or `deploy/` | Out of scope for a UI task. Stop and say so |

Never introduce a framework or a build step to fix styling or spacing. If one is
genuinely warranted, write the record and ask the owner:

```
Decision: <what is proposed>
Current state: <what the repository actually does>
Problem that cannot be solved cleanly as-is: <concrete, not preference>
Smallest option: <in the current files>
Alternative with new architecture: <what is added>
Benefit: <measurable>
Cost: <build, deployment, CSP, verify.sh, regressions>
Decision: <keep current architecture unless benefit clearly exceeds cost>
Verification: <renders, routes, locales, consent, CSP>
```

## Never, in a visual task

Read `docs/landing.md` for the reasoning; this is the short form.

- **No inline `<style>` or `style="…"`.** The production CSP drops them and the
  local preview does not. All styling in `css/site.css`.
- **No editing the inline `<script>`** without regenerating its hash in
  `deploy/nginx/snippets/landing.conf` — the deploy fails, or the browser
  silently drops it.
- **No font from a CDN.** Self-hosted `.woff2` only; `fonts.googleapis.com`
  sends every EU visitor's IP to Google before they have consented.
- **The brand teal `#2A9D8F` is never a text colour** (3.2:1). Text, links and
  filled buttons use `--accent-ink` (`#12655C`, 6.6:1). Keep the split.
- **No URL changes.** Paths are the one edit that genuinely costs rankings, and
  `deploy/verify.sh` asserts every one of them.
- **No medical claims**, in either language: no dosage guidance, no interaction
  checks, no promises about outcomes. The footer disclaimer matches the approved
  store listing word for word.
- **No weakening of consent**: analytics stays uninjected until Accept, and
  Decline keeps equal visual weight. A quieter Decline is not freely given
  consent.
- **The FAQ exists twice** — visible `<details>` and `FAQPage` JSON-LD in the
  `<head>`. Edit one and not the other and the page publishes structured data
  that contradicts itself.
- **Do not add `tool/` or `build/` to `deploy/site-include.txt`.**

⚠️ **A push to `main` ships to production.** There is no staging. Land the work
locally, look at it, then push.
