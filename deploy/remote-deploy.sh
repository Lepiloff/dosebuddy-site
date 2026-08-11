#!/usr/bin/env bash
#
# What runs on the box for a deploy. Copied there by the workflow and executed,
# rather than piped into `bash -s` over ssh.
#
# That distinction is the whole reason this file exists. Fed through stdin, the
# first `docker compose exec -T` consumed the rest of the script: compose
# forwards stdin to the container, and stdin *was* the remaining commands. The
# deploy then exited 0 because the last command it managed to run succeeded, so
# every run reported success while `nginx -s reload` had never executed. A vhost
# shipped, sat correct on disk, and was not served for ten minutes before a
# certificate request failed against it and gave the game away.
#
# `exec </dev/null` below closes the whole class of it: nothing this script runs
# can reach back into the script itself.

set -euo pipefail
exec </dev/null

REPO=/home/ubuntu/dosebuddy-site
WEB_ROOT=/srv/dosebuddy/site

cd "$REPO"
git fetch --prune origin
git reset --hard origin/main

# Publish only what deploy/site-include.txt names, and nothing else. The
# trailing --exclude='*' is what makes it an allow-list rather than a suggestion.
rsync -a --delete \
    --include-from=deploy/site-include.txt --exclude='*' \
    ./ "$WEB_ROOT/"

cd "$REPO/deploy"

# Build without starting anything, so the migration can run before the new code
# does. Started first, a container that expects a column the database has not
# grown yet fails on every request until the migration catches up — and the
# worker did exactly that, loudly, for the length of one deploy.
#
# The other order is the safe one: additive migrations leave the *old* code
# working, so the brief overlap is old-code-on-new-schema rather than the
# reverse.
docker compose build

docker compose run --rm -T api alembic upgrade head

docker compose up -d

for _ in $(seq 30); do
    docker compose exec -T nginx nginx -t >/dev/null 2>&1 && break
    sleep 1
done

# nginx reads its config from a bind mount, so `up -d` alone does not pick up a
# config change. Test first: a failed test aborts the deploy and leaves the
# previous config serving, which is the safe outcome.
docker compose exec -T nginx nginx -t
docker compose exec -T nginx nginx -s reload

# Prove the reload took effect, rather than trusting that it did.
#
# `nginx -s reload` exits 0 for sending the signal, not for the config being
# applied, and `nginx -t` only reads the files on disk. Both can pass while the
# running master still serves the previous config.
#
# Every vhost serves the ACME path, so asking the running process for it under
# each configured name proves that name is routed. An unloaded vhost falls
# through to the catch-all, which closes the connection without a response.
echo "==> vhost routing"
for name in $(docker compose exec -T nginx nginx -T 2>/dev/null \
              | awk '$1=="server_name"{print $2}' | tr -d ';' | sort -u); do
    code=$(docker compose exec -T nginx sh -c \
        "wget -qS -O /dev/null --header='Host: $name' \
         http://127.0.0.1/.well-known/acme-challenge/__probe 2>&1 \
         | awk '/HTTP\//{print \$2; exit}'" || true)
    if [ "$code" != "404" ]; then
        echo "    $name is not being served (got '${code:-no answer}')"
        exit 1
    fi
    echo "    ok: $name"
done

# A deploy that changes api/ leaves the previous image untagged, and nothing
# reclaims it. One 20 GB volume is shared with Postgres, and a disk filling up
# on this box presents as a database behaving strangely rather than as "out of
# space" — cheap to prevent, tedious to diagnose.
#
# Dangling images only, so nothing in use is touched. Build cache is trimmed to
# 1 GB rather than emptied: rebuilds on a 2 vCPU ARM box are slow enough that
# the cache earns its space.
#
# (Note for anyone reading `docker system df` here: it reports these images as
# 100% reclaimable while every one of them is in use. Shared layers confuse its
# arithmetic. Trust `docker image prune` over that column.)
echo "==> reclaiming disk"
docker image prune -f | tail -1 | sed 's/^/    /'
docker builder prune -f --keep-storage 1GB 2>/dev/null | tail -1 | sed 's/^/    /'
df -h / | awk 'NR==2{print "    root: "$3" of "$2" used ("$5")"}'

# Fail the deploy if the api never becomes ready. Without this the job goes
# green while the API answers 502 — the shape of outage that gets noticed by
# users rather than by us.
echo "==> api readiness"
for _ in $(seq 30); do
    state=$(docker inspect -f '{{.State.Health.Status}}' dosebuddy-api 2>/dev/null || echo missing)
    [ "$state" = "healthy" ] && break
    sleep 4
done
state=$(docker inspect -f '{{.State.Health.Status}}' dosebuddy-api 2>/dev/null || echo missing)
if [ "$state" != "healthy" ]; then
    echo "    api never became ready (state: $state)"
    docker compose logs --tail=50 api
    exit 1
fi
echo "    ok: api is ready"

# The worker has no health check to gate on — it holds no port — so the useful
# question is whether it is still the process that started. A crash loop shows
# up as a restart count climbing, and an alert loop that is quietly dead looks
# exactly like one with nothing to report.
echo "==> worker"
running=$(docker inspect -f '{{.State.Running}}' dosebuddy-worker 2>/dev/null || echo false)
restarts=$(docker inspect -f '{{.RestartCount}}' dosebuddy-worker 2>/dev/null || echo 0)
if [ "$running" != "true" ]; then
    echo "    worker is not running"
    docker compose logs --tail=50 worker
    exit 1
fi
echo "    ok: worker running (restarts since creation: $restarts)"
