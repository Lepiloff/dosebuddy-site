# sonadose.com

Private repository for **Sonadose** — the landing page, the v1.1 backend, and
the deployment that puts both on one box. Not an open-source project: there is
no contribution path, and nothing here is written for a stranger who might fork
it. It is written for whoever is looking after this in six months.

The Android app lives in a separate repository (`dosebuddy`), and the legal
pages in a third (`dosebuddy-legal`).

## What is here

```
index.html  es/index.html   the landing, English and Spanish, hand-written
404.html  robots.txt  sitemap.xml
css/  js/  fonts/  img/     no framework, no build step — served as committed
tool/                       regenerates img/ and fonts/ from sources

api/                        FastAPI backend: sync, pairing, alerts
deploy/                     nginx, certbot, compose, and the deploy script
docs/                       the contract, the architecture, the open debts
```

## Where it runs

One EC2 box (`t4g.small`, `eu-central-1`) serves everything:

| | |
|---|---|
| `sonadose.com` | the landing; `www` 301s to the apex |
| `api.sonadose.com` | the API |

TLS is Let's Encrypt, renewed by the certbot sidecar. GitHub Pages is still
enabled as a dormant rollback for the landing.

## How it ships

A push to `main` that touches the site, `api/` or `deploy/` runs
`.github/workflows/deploy.yml`, which executes `deploy/remote-deploy.sh` on the
box. `deploy/verify.sh` is the acceptance test and is the thing to trust — the
deploy asserts its own effects rather than assuming them.

`api/` also runs its tests on every branch (`.github/workflows/api-tests.yml`)
against a real Postgres, because encrypted columns, a partial unique index and
a sequence are not things SQLite can stand in for.

## Working locally

```bash
python3 -m http.server 8765          # the landing: /  and  /es/
tool/ui-shots.sh                     # render both locales and report what a PNG cannot
./tool/prepare-assets.sh             # regenerate img/ and fonts/ (or: screens)
```

`tool/ui-shots.sh` writes PNGs to `build/ui-shots/` and reports console errors,
failed requests, horizontal overflow and CSP violations — the local preview
sends no CSP of its own, so an inline style works there and vanishes in
production. It borrows Playwright from the npx cache and adds nothing to this
repository.

```bash
cd api && pytest                     # needs TEST_DATABASE_URL pointing at Postgres
```

## Documents

| | |
|---|---|
| [`docs/v1.1-api-contract.md`](docs/v1.1-api-contract.md) | The wire. What the endpoints take and return, agreed with the app track. Read this to write a client. |
| [`docs/sync-architecture.md`](docs/sync-architecture.md) | Why the server is built this way, and what breaks if a given line changes. Read before editing `api/app`. |
| [`docs/v1.1-contract-decisions.md`](docs/v1.1-contract-decisions.md) | Decisions taken while agreeing the contract, with their reasoning. |
| [`docs/debts.md`](docs/debts.md) | Deferred work and acceptance findings. A debt leaves only with the commit that closes it. |
| [`docs/landing.md`](docs/landing.md) | Constraints the page must keep — contrast, consent, URLs, no medical claims — and the keyword research behind the copy. |
| [`deploy/README.md`](deploy/README.md) | The box, the certificates, the rollback, the backups. |
| [`CLAUDE.md`](CLAUDE.md) | What an agent gets wrong here without being told: the deploy trigger, the CSP, the allow-list. |
