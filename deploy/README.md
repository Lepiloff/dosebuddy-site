# Hosting: GitHub Pages → EC2

The landing moves off GitHub Pages onto an EC2 box, keeping the same domain and
the same URLs, because `dosebuddy-site` becomes the monorepo for the v1.1 backend
(spec `05-technical-spec-v1.1.md` §0.2, §4.1). This directory holds everything
the move needs and everything undoing it needs.

Doing it now is deliberate: traffic is ≈0, so a botched cutover costs nothing and
DNS rolls back in about ten minutes. Later the same mistake is expensive.

**The move is only successful if it is invisible from outside.** `verify.sh` is
the executable form of that claim, calibrated against the live Pages edge on
2026-08-09.

---

## What GitHub was doing for free

| | On Pages | Here |
|---|---|---|
| TLS certificate | issued and renewed by GitHub | Let's Encrypt via the certbot sidecar |
| `http` → `https` | automatic | `conf.d/10-sonadose.conf` |
| `www` → apex, 301 | automatic | same, in one hop as before |
| CDN | Fastly, global | **lost.** Single origin in eu-central-1 |
| Web root | the whole repository | only the site (`site-exclude.txt`) |

Losing the CDN is a known, accepted regression (owner's call). Measure it after
the cutover and record the numbers next to the ones in the top-level `README.md`.

---

## Files

```
docker-compose.yml         nginx, certbot, postgres, redis, api
env.example                what deploy/.env holds; .env itself is gitignored
nginx/conf.d/00-common.conf        http-level settings, TLS params, catch-all vhost
nginx/conf.d/10-sonadose.conf      the landing: four server blocks
nginx/conf.d/20-api.conf           api.sonadose.com
nginx/snippets/landing.conf        how the site is served
site-include.txt           what the web root may contain (allow-list)
init-cert.sh               first certificate for a hostname
verify.sh                  the acceptance test for the landing
```

The API itself lives in [`../api`](../api).

---

## 1. The box

Created in the console; everything after that is SSH. The AWS API is not used
anywhere in this runbook, so the IAM user needs no EC2 permissions.

- `t4g.small`, Ubuntu 24.04 ARM, **eu-central-1**, gp3 20 GB, **volume encrypted**
  (at-rest for the backend is still an open decision — spec §2.2 — but an
  encrypted volume costs nothing to take now).
- **Elastic IP.** Not optional: without it the address changes on stop/start and
  the DNS records point at nothing.
- Security group: 22 from the owner's address only, 80 and 443 from anywhere.
- No IPv6 — see step 3.
- Key pair: **import** `~/.ssh/dosebuddy_ec2.pub` rather than letting AWS
  generate one.

Two keys, not one:

| Key | Used by | Why separate |
|---|---|---|
| `~/.ssh/dosebuddy_ec2` | the owner, and any operator session | stays on the laptop, never leaves it |
| `~/.ssh/dosebuddy_deploy` | GitHub Actions only | its private half lives in a GitHub secret, so it must not be a key that also opens anything else |

Reusing an old `.pem` shared with another project would mean one leak opens both
boxes, and putting a personal login key into CI secrets widens that further.

```bash
ssh -i ~/.ssh/dosebuddy_ec2 ubuntu@<elastic-ip>

sudo apt update && sudo apt install -y docker.io docker-compose-v2 rsync git
sudo usermod -aG docker ubuntu       # log out and back in for this to take

# Docker's default json-file driver has no size limit, and the alert worker
# writes a line a minute forever. On a volume shared with Postgres that ends as
# "the database is behaving strangely" rather than "the disk is full".
# Applies to containers created afterwards, so existing ones pick it up on the
# next deploy.
sudo tee /etc/docker/daemon.json >/dev/null <<'"'"'JSON'"'"'
{
  "log-driver": "json-file",
  "log-opts": { "max-size": "10m", "max-file": "3" }
}
JSON
sudo systemctl restart docker

sudo mkdir -p /srv/dosebuddy/site
sudo chown ubuntu:ubuntu /srv/dosebuddy/site

git clone https://github.com/Lepiloff/dosebuddy-site.git /home/ubuntu/dosebuddy-site

# let the deploy key in, alongside the operator key
echo 'ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIMKWx+LQyRlZjFU0/FTD3g2pAfhXFXo+ehDBb58/hemZ dosebuddy_deploy@dosebuddy' \
  >> ~/.ssh/authorized_keys
```

GitHub repository secrets for the deploy workflow:

| Secret | Value |
|---|---|
| `EC2_HOST` | the Elastic IP |
| `EC2_SSH_KEY` | contents of `~/.ssh/dosebuddy_deploy` — the private half, whole file |
| `EC2_HOST_KEY` | output of `ssh-keyscan -t ed25519 <elastic-ip>` |

`EC2_HOST_KEY` is pinned rather than trusted blindly. This box holds article 9
health data once the api lands on it; a deploy that accepts whatever answers on
port 22 is a standing hole.

## 2. Rehearsal on `new.dosebuddyapp.com`

The point of this step: prove the whole stack over real TLS **while
dosebuddyapp.com is still served by Pages**, so a mistake is invisible.

1. Add one A record at GoDaddy: `new` → Elastic IP.
2. Issue the rehearsal certificate. This comes **before** the first deploy, not
   after: nginx will not start until every certificate its config names exists,
   and `init-cert.sh` is what puts placeholders there and brings nginx up. A
   deploy attempted first would just fail on a container that cannot boot.

   Use staging on the first run; production Let's Encrypt allows five identical
   certificates a week and a typo eats the allowance:

   ```bash
   cd /home/ubuntu/dosebuddy-site/deploy
   STAGING=1 ./init-cert.sh new.dosebuddyapp.com   # dry run
   ./init-cert.sh new.dosebuddyapp.com             # for real
   ```

3. Publish the site: GitHub → Actions → *Deploy landing to EC2* → Run. Until this
   runs the web root is empty and every path answers 404.
4. Run the acceptance test from anywhere:

   ```bash
   ./verify.sh new.dosebuddyapp.com
   ```

   It must end `failed 0`. The `https://www → apex` check is skipped here and
   only runs after the cutover: pinning `www` to this box now would present the
   rehearsal certificate and fail for the wrong reason.

`20-rehearsal.conf` sends `X-Robots-Tag: noindex, nofollow`. That header is the
only thing stopping Google from indexing a second copy of the landing under a
subdomain, which is exactly the duplicate content this migration exists to avoid.

## 3. Cutover

DNS TTL is already 600 s, so there is nothing to lower first.

At GoDaddy → `dosebuddyapp.com` → DNS:

1. Replace the **four A records** on `@` (`185.199.108–111.153`) with one A
   record pointing at the Elastic IP.
2. **Delete the four AAAA records** on `@`. Leaving them is the one change that
   breaks quietly: dual-stack clients would keep reaching GitHub over IPv6 while
   IPv4 clients hit EC2, splitting traffic across two hosts until Pages is turned
   off, at which point half of it dies. IPv6-only clients still reach an
   IPv4-only host through their carrier's DNS64/NAT64.
3. Change `www` from `CNAME lepiloff.github.io` to `CNAME dosebuddyapp.com`.
4. **Do not touch** the `google-site-verification` TXT record.

Wait for the TTL, confirm, then take the production certificate:

```bash
dig +short dosebuddyapp.com A          # expect the Elastic IP alone
dig +short dosebuddyapp.com AAAA       # expect nothing
dig +short www.dosebuddyapp.com

cd /home/ubuntu/dosebuddy-site/deploy
./init-cert.sh dosebuddyapp.com www.dosebuddyapp.com
```

`https://dosebuddyapp.com` is unreachable between the DNS change and this
certificate — a few minutes. Accepted, given traffic is ≈0.

## 4. Sign-off

```bash
./verify.sh dosebuddyapp.com                                     # must be failed 0

docker compose run --rm -T --entrypoint certbot certbot \
  renew --dry-run --no-random-sleep-on-renew
```

The renewal dry run is not optional. `init-cert.sh` uses webroot HTTP-01
precisely so renewal is automatic from day one; the dry run is what proves it.
Expect `Congratulations, all simulated renewals succeeded`.

Both flags are there to stop it looking broken when it is not:

- **`--no-random-sleep-on-renew`.** Run non-interactively, certbot sleeps a
  random interval of up to eight minutes before renewing, to spread load across
  Let's Encrypt's clients. It prints nothing further while it waits, so it reads
  exactly like a hang. The sidecar keeps the delay — that is what it is for —
  but a person watching a terminal should not have to.
- **`-T`.** Without it `docker compose run` wants a TTY.

If a run is interrupted, check for a leftover container before retrying:
`docker ps --filter name=certbot-run`. Certbot takes a lock on
`/etc/letsencrypt`, so an orphan from a cancelled run makes every later attempt
sit and wait, silently, which looks like the same hang again.

Then:

- Delete `nginx/conf.d/20-rehearsal.conf`, delete the `new` A record, and remove
  its certificate (`certbot delete --cert-name new.dosebuddyapp.com`).
- Uncomment the `push` trigger in `.github/workflows/deploy.yml`.
- Re-measure Lighthouse and LCP, compare against the top-level `README.md`
  (`100/100/100/100`, LCP 1.7 s en / 1.8 s es). This is the CDN regression.
- Search Console needs no change of address — same domain. Resubmit the sitemap
  and run a Live Test.

HSTS started at `max-age=300` and was raised to `31536000` on 2026-08-18, once
the move had held and Pages had stopped being the fallback. It was short at
first on purpose: a year-long promise made on day one would have made a
rollback painful if GitHub's certificate had lapsed by then. The value lives in
three places — `nginx/snippets/landing.conf`, `nginx/conf.d/10-dosebuddyapp.conf`
(the `www` redirect, which does not include the snippet) and
`nginx/conf.d/20-api.conf`.

---

## The API

`api.dosebuddyapp.com`, served by the same nginx, proxied to uvicorn. It has its
own hostname because that address ends up compiled into app builds that are
already on people's phones, and because the landing's config then never has to
be edited to change the API.

**A dead API cannot take the landing down.** `20-api.conf` resolves its upstream
through a variable with a `resolver` rather than naming the container in
`proxy_pass`. With a literal name nginx resolves at config load, so a stopped
api container makes nginx refuse to start — and that takes dosebuddyapp.com with
it. Through a variable nginx starts regardless and the API host answers 502.
Verified by stopping the api and restarting nginx cold: the landing still passed.

First-time setup on a new box:

```bash
cd /home/ubuntu/dosebuddy-site/deploy

# 1. Secrets. See env.example for what goes in and why the password cannot be
#    changed by editing this file later.
{ echo "POSTGRES_USER=dosebuddy"
  echo "POSTGRES_DB=dosebuddy"
  echo "POSTGRES_PASSWORD=$(openssl rand -base64 48 | tr -dc 'A-Za-z0-9' | head -c 40)"
} > .env
chmod 600 .env

# 2. Certificate, after `api` resolves to this box.
./init-cert.sh api.dosebuddyapp.com

# 3. Up.
docker compose up -d --build
```

Postgres and Redis publish no ports; they are reachable only from the compose
network. For a database holding article 9 health data that is the single most
useful thing that can be said about it, so if a port ever needs publishing,
treat that as a decision rather than a convenience.

Day to day:

```bash
docker compose ps                      # api should read "healthy", not just "running"
docker compose logs -f api
docker compose run --rm -T api alembic upgrade head
```

`healthy` here means `/health/ready` answered 200, which means Postgres and Redis
both replied. `running` on its own means only that the process started.

To move a dependency: edit `api/requirements.txt`, then regenerate the lock the
image installs from.

```bash
rm ../api/requirements.lock.txt        # or the build reinstalls the old set
docker build -t dosebuddy-api:lock ../api
docker run --rm dosebuddy-api:lock pip freeze \
  | grep -viE '^(pip|setuptools|wheel)==' > ../api/requirements.lock.txt
```

**Delete the lock first.** The build prefers it over `requirements.txt`, so
regenerating without removing it just freezes the old set again — and the tests
will not notice, because they install `requirements-dev.txt` directly. That is
how three new runtime dependencies once got left out of the image while every
check stayed green.

## Backups

Two layers, because they fail differently.

**Logical, on the box** — `backup.sh`, nightly at 02:17 UTC, encrypted with
`BACKUP_PASSPHRASE`, 14 days kept. A weekly job actually restores the newest
dump into a scratch database and checks that tables came back.

```bash
./backup.sh dump      # take one now
./backup.sh verify    # restore the newest and check it
./backup.sh list
```

The verify job is the point. Backups do not usually fail by being absent; they
fail by being written faithfully for a year and then not restoring. Checking
that the file exists proves nothing, and neither does a restore that reports no
error — an empty dump restores perfectly, which is why the check counts tables.

This layer covers a bad migration, a mistaken delete, a corrupted table. It does
**not** cover losing the instance, because it lives on the instance.

**Physical, off the box — the owner's to set up.** EC2 → Elastic Block Store →
Lifecycle Manager → create a snapshot policy for the instance's volume, daily,
7 days retention. Entirely in the console: no credentials on the box, nothing to
rotate, and it survives the machine.

Shipping dumps to S3 would need an IAM user or an instance role. Worth doing
eventually; snapshots cover the same gap today without putting a key on a box
that holds article 9 data.

> Two secrets in `deploy/.env` are unrecoverable and must be kept **off the box
> as well**: `ENCRYPTION_KEY`, without which the encrypted columns are lost even
> with an intact database, and `BACKUP_PASSPHRASE`, without which the dumps are
> just noise. A backup whose passphrase died with the machine is not a backup.

## The FCM credential

Caregiver alerts are detected without it — the worker says plainly that nothing
was delivered — and sent with it.

Use a service account scoped to messaging, not the Firebase Admin SDK key the
console offers first: that one is full project administrator, and this file sits
on a box holding article 9 data. Cloud Console → IAM → Service Accounts → create
one with **Firebase Cloud Messaging API Admin**, then add a JSON key.

```bash
# On the box. The image runs as uid 10001 — deliberately not root — so the
# credential has to belong to that user, not to ubuntu.
mkdir -p deploy/secrets && chmod 755 deploy/secrets
# copy the JSON to deploy/secrets/fcm.json, then:
sudo chown 10001:10001 deploy/secrets/fcm.json
sudo chmod 400 deploy/secrets/fcm.json
```

Then in `.env`:

```
FCM_PROJECT_ID=dosebuddy-cfa04
FCM_CREDENTIALS_PATH=/run/secrets/fcm.json
```

Mounted read-only into the worker and nowhere else: the API never sends a push,
so the credential has no business being reachable from the process that answers
the internet.

To prove it works without a device, send to a deliberately invalid registration
token. `INVALID_ARGUMENT` means Google authenticated the request and rejected
only the token — which is the part being tested. `401` or `403` means the
credential or the API is the problem.

> **Deleting a key file does not revoke the key.** It stays valid until it is
> deleted in the console, and any copy — a backup, a synced folder, a trash can
> — still works. Revoke first, delete second.

## Rollback

Keep this available for the whole migration. **Pages stays enabled and the
`CNAME` file stays in the repository** — the spec notes the file is no longer
needed, which is true, but deleting it before Pages is retired kills the rollback.

Restore at GoDaddy:

```
A     @      185.199.108.153
A     @      185.199.109.153
A     @      185.199.110.153
A     @      185.199.111.153
AAAA  @      2606:50c0:8000::153
AAAA  @      2606:50c0:8001::153
AAAA  @      2606:50c0:8002::153
AAAA  @      2606:50c0:8003::153
CNAME www    lepiloff.github.io
```

Live again in about ten minutes.

**The rollback window is not open forever.** The Let's Encrypt certificate GitHub
holds for the domain stops renewing once DNS points elsewhere. Rolling back in
the first days is free; after a couple of weeks it means a window with no HTTPS
while GitHub reissues. So the decision to stay is one to make within days.

---

## Known differences from Pages

Two, both deliberate, both caught by `verify.sh` when it is run against Pages:

- **`/README.md` and `/tool/prepare-assets.sh` answered 200 on Pages** and answer
  404 here. Pages publishes the whole repository; this host publishes the site.
  Neither path is linked or in `sitemap.xml`, and the README documents the
  infrastructure, so this is a small improvement rather than a loss.
- **`/CNAME` and `/.nojekyll` answered 200 on Pages** and answer 404 here. Both
  are Pages plumbing, not content.

Everything else — statuses, redirect targets, content types, `max-age=600` on
HTML — is reproduced exactly. Images and fonts are the one intended upgrade, at
30 days; CSS and JS deliberately stay at 600 s because they are hand-edited
alongside the HTML and are not content-hashed.

## Not part of this move

- Content Security Policy. Every page carries a small inline script
  (`index.html:126`, the anti-CLS language hint), so a CSP needs a sha256 hash
  for it. Worth doing, but as its own commit: a CSP mistake must not look like a
  migration failure.
- Cloudflare. It would bring the CDN and IPv6 back and take ACME off our hands,
  but it moves nameservers off GoDaddy — a second independent DNS operation that
  does not belong in the same window as this one. Nothing here is wasted if it
  happens later: under Full (strict) the origin still needs a valid certificate
  and Let's Encrypt keeps working unchanged. Flexible mode is not an option, it
  leaves Cloudflare→EC2 in clear text. It also makes Cloudflare a processor for
  health data, which needs a DPA and a line in the privacy policy.
- Moving the landing into `site/`. It stays at the repository root until Pages is
  retired, because Pages can only publish from the root or `/docs`.
- Postgres, Redis and the FastAPI skeleton — the next step of the build order
  (spec §0.5 item 4), joining this same `docker-compose.yml`.
