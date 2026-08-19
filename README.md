# dosebuddyapp.com

Marketing site for [DoseBuddy](https://play.google.com/store/apps/details?id=com.everpine.dosebuddy),
an Android medication reminder built for families looking after an elderly parent.

Static HTML, CSS and vanilla JavaScript. No framework, no build step, no
dependencies. What is in this repository is exactly what is served.

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

api/                     FastAPI backend: sync, pairing, alerts (api.dosebuddyapp.com)
deploy/                  nginx + certbot + compose stack, and how it ships
docs/                    the contract, the architecture, the open debts
```

The site is no longer the whole repository. `api/` and `deploy/` arrived with
v1.1, when the landing moved off GitHub Pages onto the same EC2 box that serves
the API. The landing is still served exactly as written — nothing below changed
about that.

## Documents

| | |
|---|---|
| [`docs/v1.1-api-contract.md`](docs/v1.1-api-contract.md) | The wire. What the endpoints take and return, agreed with the app track. Read this to write a client. |
| [`docs/sync-architecture.md`](docs/sync-architecture.md) | The implementation. Why the server is built this way, and what breaks if a given line is changed. Read this before editing `api/app`. |
| [`docs/v1.1-contract-decisions.md`](docs/v1.1-contract-decisions.md) | Decisions taken during the contract, with their reasoning. |
| [`docs/debts.md`](docs/debts.md) | Deliberately deferred work, and acceptance findings. A debt only leaves with the commit that closes it. |
| [`deploy/README.md`](deploy/README.md) | The box, the certificates, the rollback, the backups. |

## Local preview

```bash
python3 -m http.server 8765
# http://127.0.0.1:8765/     English
# http://127.0.0.1:8765/es/  Español
```

`?consent-preview=1` forces the cookie banner to appear even if the Analytics
ID is ever put back to a placeholder.

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
| **Served from** | **EC2, `t4g.small` in eu-central-1, since 2026-08-10** |
| Stack | nginx + certbot in Docker — [`deploy/`](deploy/README.md) |
| Custom domain | `dosebuddyapp.com`, apex canonical, `www` 301s to it |
| TLS | Let's Encrypt, renewed by the certbot sidecar |
| Acceptance | `deploy/verify.sh` — 30 of 30 against the live domain |
| First live | 2026-07-31 on GitHub Pages, both locales |

The site moved off GitHub Pages on 2026-08-10 keeping the domain and every URL
identical. **Pages is still enabled and `CNAME` is still committed**, because
that is the rollback; both go away only once the move has held. How to roll
back, and how long that stays cheap, is in [`deploy/README.md`](deploy/README.md).

Steps 1 and 2 below are the original Pages wiring, kept as the record of what a
rollback restores.

---

## Owner checklist

### 1. DNS at the `dosebuddyapp.com` registrar (done 2026-07-31)

The domain is registered at **GoDaddy** (`ns39/ns40.domaincontrol.com`). It
used to point at their parking page; these are the records that replaced it.

In GoDaddy → *My Products* → `dosebuddyapp.com` → *DNS*:

1. **Delete** the two parked `A` records on `@`.
2. **Delete** any *Forwarding* rule on the domain or on `www`, if one is set.
   Forwarding overrides the records below.
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

Verify before moving on. This must list the four GitHub addresses, not the
GoDaddy ones:

```bash
dig +short dosebuddyapp.com A
dig +short www.dosebuddyapp.com CNAME
```

### 2. Enforce HTTPS (done 2026-07-31)

Once DNS resolves to GitHub, GitHub issues a free Let's Encrypt certificate.
That takes anywhere from a few minutes to about an hour; until it exists the
setting cannot be turned on at all (the API answers *"The certificate does not
exist yet"*).

Then either tick **Settings → Pages → Enforce HTTPS**, or:

```bash
gh api -X PUT repos/Lepiloff/dosebuddy-site/pages -F https_enforced=true
```

Leaving this off would serve the site over plain HTTP, so it is not optional.

### 3. Google Analytics 4 (done)

`js/consent.js` carries the live Measurement ID `G-LE5K9MMGRG` (property
*DoseBuddy site*, web stream `dosebuddyapp.com`). The consent banner is
therefore active.

Enhanced measurement is on for the stream, so outbound clicks are counted
without any extra code on the page. That includes the Google Play button, the
only conversion this page has.

To swap the ID later, edit that one line. If it is ever set back to a
placeholder the banner hides itself again on purpose: with nothing being
collected, asking for consent would be misleading.

GA will always under-count relative to reality, because visitors who decline
are never measured at all. Search Console is the honest number for organic
search.

### 4. Google Search Console (recommended)

Add `https://dosebuddyapp.com` as a property, verify by DNS TXT record, and
submit `https://dosebuddyapp.com/sitemap.xml`.

### 5. Copy review (your call)

Both languages were written for this page rather than translated from the store
listing, so the tone is yours to approve:

- English: `index.html`
- Español: `es/index.html`

Two lines to look at in particular:

- **"No ads" / "Sin publicidad"** in the hero note is true for the shipped
  release. If AdMob ever ships (v1.0.x backlog), that line has to change. The
  FAQ answer is written to survive it: it promises only that an ad will never
  appear on a reminder, an alarm or the elder-mode screen, which is a hard
  invariant of the app, not a temporary state.
- **Premium price** is deliberately not printed anywhere; the page points at the
  Play listing instead, so the site never goes stale when prices change per
  market.

---

## Keyword research (2026-08-01)

Method: Google autocomplete expanded across the alphabet for seven seeds per
language, plus Google Trends for relative volume. Autocomplete is the honest
signal here: a phrase that returns no suggestions is a phrase nobody types.

### What the data changed

**Spanish was aimed at the wrong noun.** The page led with *medicación*. Trends
(Spain, 12 months) puts `recordar pastillas` at 1.9 and both `recordatorio de
medicamentos` and `recordatorio de medicación` at 0.0, while autocomplete
returns a thick cluster around *pastillas* and *medicamentos*. The two phrases
the original plan targeted, `recordatorio de medicación para personas mayores`
and `app para recordar la medicación a mis padres`, return **nothing at all**.
They are not queries. Title, H1 and description now lead with *pastillas*, with
*medicamentos* in the body.

**"Free" and "Android" were missing everywhere.** Autocomplete is full of
`medication reminder app free android`, `best free medication reminder app for
android`, `recordatorio de medicamentos gratis`, `app para recordar tomar
pastillas gratis`. DoseBuddy is free and Android-only, so this is the easiest
qualifier available and neither title carried it. Both now do.

**The alarm cluster was untapped.** `alarms for taking medication`,
`medication alarm app`, `medication alarm for elderly`, `alarma para tomar
medicamentos android`, `como poner alarma para tomar medicamentos` are all real
queries. The full-screen alarm is the product's differentiator, so "alarm" now
appears in the hero lead and both meta descriptions rather than only halfway
down the page.

**"Caregiver" is a weaker target than assumed.** English autocomplete around
*caregiver medication* skews professional: `caregiver medication aide`,
`medication technician`, `medication administration`, `medication training`.
Consumer intent phrases itself as *for elderly* / *for seniors*. The caregiver
framing stays in the copy because it is true and it converts, but it is not
worth optimising for as a keyword.

**Nouns, English.** Trends (US): `medication reminder` 24.9, `medicine
reminder` 15.0, `pill reminder` 14.7. The title already used the strongest one.

### What on-page SEO cannot fix

The SERP for `best free medication reminder app for android` and for `app para
recordar tomar las pastillas personas mayores` is owned by roundups, not by
product pages: seniorsguide.com, myseniorcarehub.com and pillo.care in English;
tevafarmacia.es, cuideo.com, facilparatodos.es, pastillero.com and
mitservicios.com in Spanish. A landing page will not outrank a listicle for a
"best app" query. Getting DoseBuddy *into* those lists is worth more than any
further on-page work, and it is outreach, not code.

### Competitors the market analysis does not mention

Surfaced while checking the SERP, each competing on ground we assumed was ours:

- **Pillo**, "persistent alarm system that won't stop until you respond". Our
  differentiator, described in our own words.
- **MedTimer**, open source, zero data collection, no account. Our privacy angle.
- **mySeniorCareHub**, "designed especially for seniors with large text, simple
  buttons". Our exact wedge.
- **Recuerdamed**, Spanish, free, simple. A local competitor in market #1.

Worth folding back into `docs/06-market-analysis.md` in the app repo.

---

## Decisions worth knowing before editing

**Fonts are self-hosted, never from the Google Fonts CDN.** Loading them from
`fonts.googleapis.com` sends every EU visitor's IP address to Google before they
have consented to anything. The whole family is one 33 KB `.woff2`.

**The brand teal is not a text colour.** `#2A9D8F` measures 3.2:1 against the
page background, below the WCAG AA threshold of 4.5:1. It is used for fills,
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

**No medical claims anywhere.** The app is a reminder organiser, not a medical
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
| Largest Contentful Paint | 1.4 s both, on EC2 (was 1.7 s en · 1.8 s es on Pages) |
| Time to first byte | 30–40 ms |
| Cumulative Layout Shift | 0 |
| W3C HTML validation, all three pages | clean |
| Horizontal reflow, 320 → 1440 px, both languages | no overflow |
| Rendering with JavaScript disabled | pixel-identical |
| Analytics before consent | zero requests to Google |
| Structured data | `SoftwareApplication` + `FAQPage` (9 Q&A per language) |
| Dark mode (`prefers-color-scheme`) | full token set |

Re-run the Lighthouse check with the local server on:

```bash
npx -y lighthouse@latest http://127.0.0.1:8765/ --view \
  --chrome-flags="--headless=new"
```

The EC2 numbers above were measured from Spain, which is market #1 and so the
number that matters most — but it is also 25 ms from the Frankfurt origin. They
do **not** show what a US visitor sees. GitHub Pages served from a CDN and this
does not, so if the loss shows up anywhere it will show up there, and nothing
here has measured it.
