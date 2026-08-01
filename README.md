# dosebuddyapp.com

Marketing site for [DoseBuddy](https://play.google.com/store/apps/details?id=com.everpine.dosebuddy),
an Android medication reminder built for families looking after an elderly parent.

Static HTML, CSS and vanilla JavaScript. No framework, no build step, no
dependencies — what is in this repository is exactly what is served.

- English: `/` → `index.html`
- Español: `/es/` → `es/index.html`

---

## Layout

```
CNAME                    custom domain, read by GitHub Pages
.nojekyll                serve files as-is, no Jekyll pass
index.html               English page
es/index.html            Spanish page (localised, not translated)
404.html                 bilingual not-found page
robots.txt  sitemap.xml  crawling
css/site.css             the whole design system, one file
js/consent.js            cookie banner + GA4 behind Consent Mode v2
js/ui.js                 language-hint dismissal + dormant lazy-video helper
fonts/                   self-hosted Atkinson Hyperlegible Next (+ OFL licence)
img/                     mark, OG cards, Play badges, app screenshots
tool/prepare-assets.sh   regenerates everything in img/ and fonts/
tool/og-template.html    source of the Open Graph cards
```

## Local preview

```bash
python3 -m http.server 8765
# http://127.0.0.1:8765/     English
# http://127.0.0.1:8765/es/  Español
```

Add `?consent-preview=1` to force the cookie banner to appear while the
Analytics ID is still a placeholder.

## Regenerating assets

```bash
./tool/prepare-assets.sh            # everything
./tool/prepare-assets.sh screens    # only the app screenshots
```

Screenshots are read from the app repository (`../dosebuddy` by default,
override with `APP_REPO=…`). Needs `imagemagick`, `curl` and `google-chrome`.

---

## Deployment status

| | |
|---|---|
| Repository | [Lepiloff/dosebuddy-site](https://github.com/Lepiloff/dosebuddy-site), public |
| GitHub Pages | enabled, `main` / root, build **green** |
| Custom domain | `dosebuddyapp.com`, picked up from the committed `CNAME` |
| `lepiloff.github.io/dosebuddy-site` | 301 → `dosebuddyapp.com` — the `github.io` URL can never be indexed |
| Serving correctly | verified against the Pages edge with the DNS bypassed |
| **Blocked on** | **DNS (step 1), then HTTPS (step 2)** |

The site is already built and served by GitHub. It is not reachable at
`dosebuddyapp.com` yet only because the domain still resolves to GoDaddy's
parking page.

---

## Owner checklist

### 1. DNS at the `dosebuddyapp.com` registrar — required, blocking

The domain is registered at **GoDaddy** (`ns39/ns40.domaincontrol.com`) and
currently points at their parking page (`76.223.105.230`, `13.248.243.5`).

In GoDaddy → *My Products* → `dosebuddyapp.com` → *DNS*:

1. **Delete** the two parked `A` records on `@`.
2. **Delete** any *Forwarding* rule on the domain or on `www`, if one is set —
   forwarding overrides the records below.
3. **Change** the existing `www` record: it is currently a `CNAME` to `@`; it
   must become a `CNAME` to `lepiloff.github.io`.
4. **Add** the eight records listed below.

The `CNAME` file is already committed, so the site never gets indexed under a
`*.github.io` address. It only takes effect once DNS points at GitHub.

**Four A records** on the apex (`@` / blank host), TTL default:

```
185.199.108.153
185.199.109.153
185.199.110.153
185.199.111.153
```

**Four AAAA records** on the apex (IPv6, recommended):

```
2606:50c0:8000::153
2606:50c0:8001::153
2606:50c0:8002::153
2606:50c0:8003::153
```

**One CNAME record** so `www` redirects to the apex:

```
Host: www     Value: lepiloff.github.io
```

Apex is the canonical host; GitHub issues the 301 from `www` automatically.

Verify before moving on — this must list the four GitHub addresses, not the
GoDaddy ones:

```bash
dig +short dosebuddyapp.com A
dig +short www.dosebuddyapp.com CNAME
```

### 2. Enforce HTTPS — required, after DNS

Once DNS resolves to GitHub, GitHub issues a free Let's Encrypt certificate.
That takes anywhere from a few minutes to about an hour; until it exists the
setting cannot be turned on at all (the API answers *"The certificate does not
exist yet"*).

Then either tick **Settings → Pages → Enforce HTTPS**, or:

```bash
gh api -X PUT repos/Lepiloff/dosebuddy-site/pages -F https_enforced=true
```

Leaving this off would serve the site over plain HTTP, so it is not optional.

### 3. Google Analytics 4 — done

`js/consent.js` carries the live Measurement ID `G-LE5K9MMGRG` (property
*DoseBuddy site*, web stream `dosebuddyapp.com`). The consent banner is
therefore active.

Enhanced measurement is on for the stream, so outbound clicks — including the
Google Play button, the only conversion this page has — are counted without any
extra code on the page.

To swap the ID later, edit that one line. If it is ever set back to a
placeholder the banner hides itself again on purpose: with nothing being
collected, asking for consent would be misleading.

GA will always under-count relative to reality, because visitors who decline
are never measured at all. Search Console is the honest number for organic
search.

### 4. Google Search Console — recommended

Add `https://dosebuddyapp.com` as a property, verify by DNS TXT record, and
submit `https://dosebuddyapp.com/sitemap.xml`.

### 5. Copy review — your call

Both languages were written for this page rather than translated from the store
listing, so the tone is yours to approve:

- English: `index.html`
- Español: `es/index.html`

Two lines to look at in particular:

- **"No ads" / "Sin publicidad"** in the hero note is true for the shipped
  release. If AdMob ever ships (v1.0.x backlog), that line has to change. The
  FAQ answer is written to survive it: it promises only that an ad will never
  appear on a reminder, an alarm or the elder-mode screen — which is a hard
  invariant of the app, not a temporary state.
- **Premium price** is deliberately not printed anywhere; the page points at the
  Play listing instead, so the site never goes stale when prices change per
  market.

---

## Decisions worth knowing before editing

**Fonts are self-hosted, never from the Google Fonts CDN.** Loading them from
`fonts.googleapis.com` sends every EU visitor's IP address to Google before they
have consented to anything. The whole family is one 33 KB `.woff2`.

**The brand teal is not a text colour.** `#2A9D8F` measures 3.2:1 against the
page background — below the WCAG AA threshold of 4.5:1. It is used for fills,
borders and the mark; `--accent-ink` (`#12655C`, 6.6:1) carries text, links and
filled buttons. Please keep that split when adding anything.

**Analytics loads after consent, not before it.** The usual Consent Mode setup
loads `gtag.js` immediately and merely restricts it. This one does not inject
the script at all until Accept is clicked, and both buttons are styled with the
same weight, because a banner that makes "Decline" quieter is not freely given
consent.

**URLs must not change.** The site moves to its own host at v1.1 under the same
domain; identical paths mean the migration costs nothing in search. Changing a
path later is the one thing that genuinely breaks rankings.

**No medical claims — anywhere.** The app is a reminder organiser, not a medical
device. No dosage guidance, no interaction checks, no promises about treatment
outcomes, in either language. The footer disclaimer matches the approved store
listing word for word.

**A video can be added without a redesign.** Drop this anywhere in a section:

```html
<div class="video" data-yt="VIDEO_ID" data-poster="/img/video-poster.webp"
     data-label="Play the DoseBuddy overview"></div>
```

`js/ui.js` renders a poster and a play button; nothing is requested from Google
until the visitor clicks, and the embed then uses `youtube-nocookie.com`.

---

## Verified on this build

| Check | Result |
|---|---|
| Lighthouse mobile, `/` and `/es/` | 100 / 100 / 100 / 100 |
| Largest Contentful Paint | 1.7 s en · 1.8 s es |
| Cumulative Layout Shift | 0 |
| W3C HTML validation, all three pages | clean |
| Horizontal reflow, 320 → 1440 px, both languages | no overflow |
| Rendering with JavaScript disabled | pixel-identical |
| Analytics before consent | zero requests to Google |
| Structured data | `SoftwareApplication` + `FAQPage` (8 Q&A per language) |
| Dark mode (`prefers-color-scheme`) | full token set |

Re-run the Lighthouse check with the local server on:

```bash
npx -y lighthouse@latest http://127.0.0.1:8765/ --view \
  --chrome-flags="--headless=new"
```
