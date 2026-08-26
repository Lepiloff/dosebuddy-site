# The landing page: what must stay true

Moved out of `README.md` on 2026-08-25. The README had grown into a mixture of
orientation, operational history and research, and the parts that mattered were
the hardest to find. What follows is the half worth keeping: constraints that
outlive any particular edit, and one dated piece of research.

---

## Constraints

**Fonts are self-hosted, never from the Google Fonts CDN.** Loading them from
`fonts.googleapis.com` sends every EU visitor's IP address to Google before they
have consented to anything. The whole family is one 33 KB `.woff2`.

**The brand teal is not a text colour.** `#2A9D8F` measures 3.2:1 against the
page background, below the WCAG AA threshold of 4.5:1. It is used for fills,
borders and the mark; `--accent-ink` (`#12655C`, 6.6:1) carries text, links and
filled buttons. Keep that split when adding anything.

**Analytics loads after consent, not before it.** The usual Consent Mode setup
loads `gtag.js` immediately and merely restricts it. This one does not inject
the script at all until Accept is clicked, and both buttons carry the same
visual weight, because a banner that makes "Decline" quieter is not freely
given consent.

**Styling lives in `css/site.css`, never inline.** The production CSP sets
`style-src 'self'` and `script-src 'self' '<one hash>'`, so an inline `<style>`
block, a `style="…"` attribute, or an edit to the one inline script is dropped by
the browser. `python3 -m http.server` sends no CSP at all, so the page looks
correct locally and is broken in production — silently, with no error anywhere.
`tool/ui-shots.sh` replays the real header locally so the violation surfaces as a
console error; `deploy/nginx/snippets/landing.conf` is where the policy actually
lives, and `.github/workflows/deploy.yml` fails the deploy on a stale hash.

**URLs must not change.** Identical paths are what made the move off GitHub
Pages, and later the move to a new domain, cost nothing in search. Changing a
path is the one edit that genuinely breaks rankings.

**No medical claims anywhere.** The app is a reminder organiser, not a medical
device. No dosage guidance, no interaction checks, no promises about treatment
outcomes, in either language. The footer disclaimer matches the approved store
listing word for word.

**The FAQ exists twice.** Visible `<details>` copy and `FAQPage` JSON-LD in the
`<head>` must say the same thing. Editing one and not the other publishes
structured data that contradicts the page.

**A video can be added without a redesign.** Drop this into any section:

```html
<div class="video" data-yt="VIDEO_ID" data-poster="/img/video-poster.webp"
     data-label="Play the Sonadose overview"></div>
```

`js/ui.js` renders a poster and a play button; nothing is requested from Google
until the visitor clicks, and the embed then uses `youtube-nocookie.com`.

---

## Keyword research — snapshot, 2026-08-01

⚠️ Taken under the name **DoseBuddy** and the domain **dosebuddyapp.com**, both
of which are gone. The conclusions about *which words people type* are unaffected
by the rename; the examples are not re-run.

Method: Google autocomplete expanded across the alphabet for seven seeds per
language, plus Google Trends for relative volume. Autocomplete is the honest
signal here — a phrase that returns no suggestions is a phrase nobody types.

**Spanish was aimed at the wrong noun.** The page led with *medicación*. Trends
(Spain, 12 months) puts `recordar pastillas` at 1.9 and both `recordatorio de
medicamentos` and `recordatorio de medicación` at 0.0, while autocomplete
returns a thick cluster around *pastillas* and *medicamentos*. The two phrases
the original plan targeted — `recordatorio de medicación para personas mayores`
and `app para recordar la medicación a mis padres` — return **nothing at all**.
They are not queries. Title, H1 and description lead with *pastillas*, with
*medicamentos* in the body.

**"Free" and "Android" were missing everywhere.** Autocomplete is full of
`medication reminder app free android`, `best free medication reminder app for
android`, `recordatorio de medicamentos gratis`, `app para recordar tomar
pastillas gratis`. The app is free and Android-only, so this is the easiest
qualifier available and neither title carried it. Both now do.

**The alarm cluster was untapped.** `alarms for taking medication`, `medication
alarm app`, `medication alarm for elderly`, `alarma para tomar medicamentos
android`, `como poner alarma para tomar medicamentos` are all real queries. The
full-screen alarm is the product's differentiator, so "alarm" appears in the
hero lead and both meta descriptions rather than only halfway down the page.

**"Caregiver" is a weaker target than assumed.** English autocomplete around
*caregiver medication* skews professional: `caregiver medication aide`,
`medication technician`, `medication administration`. Consumer intent phrases
itself as *for elderly* / *for seniors*. The caregiver framing stays in the copy
because it is true and it converts, but it is not worth optimising for.

**Nouns, English.** Trends (US): `medication reminder` 24.9, `medicine
reminder` 15.0, `pill reminder` 14.7.

### What on-page SEO cannot fix

The SERP for `best free medication reminder app for android` and for `app para
recordar tomar las pastillas personas mayores` is owned by roundups, not by
product pages: seniorsguide.com, myseniorcarehub.com and pillo.care in English;
tevafarmacia.es, cuideo.com, facilparatodos.es, pastillero.com and
mitservicios.com in Spanish. A landing page will not outrank a listicle for a
"best app" query. Getting the app *into* those lists is worth more than any
further on-page work, and it is outreach, not code.

### Competitors the market analysis does not mention

Each competing on ground the plan assumed was ours:

- **Pillo** — "persistent alarm system that won't stop until you respond". Our
  differentiator, in our own words.
- **MedTimer** — open source, zero data collection, no account. Our privacy angle.
- **mySeniorCareHub** — "designed especially for seniors with large text, simple
  buttons". Our exact wedge.
- **Recuerdamed** — Spanish, free, simple. A local competitor in market #1.

Worth folding into `docs/06-market-analysis.md` in the app repo.
