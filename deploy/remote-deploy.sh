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
# --delete-excluded, not just --delete. On its own, --delete protects excluded
# files rather than removing them, so anything already in the web root that the
# allow-list does not name would simply stay there — which is exactly how docs/
# survived the first attempt at this fix and kept serving.
rsync -a --delete --delete-excluded \
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
#
# A deploy that changes nothing about the API should not restart it, and on
# 2026-08-25 one did: a commit touching only `api/tests/` recreated
# `dosebuddy-api`, ten seconds in which every phone mid-sync got a 502.
#
# The cause, read out of the deploy log the same day rather than guessed at.
# **Every layer was CACHED and the image id changed anyway.** BuildKit exports a
# provenance attestation next to the image, the attestation carries build-time
# metadata, and the digest of the manifest list therefore differs on every
# build even when the contents are byte-identical. Compose compares that digest,
# sees a new image, and recreates. Two services here build the same context, so
# it happened twice per deploy.
#
# `BUILDX_NO_DEFAULT_ATTESTATIONS` removes the attestation, which is what makes
# the export deterministic. It is set as an environment variable rather than
# passed as `--provenance=false`, because an unsupported flag fails the build
# while an unrecognised variable is simply ignored — and this script must
# survive an older compose on the box.
#
# The fingerprint below is the belt to that pair of braces: it asks whether the
# image is the *same image*, in terms of its layers and the config the
# Dockerfile sets, rather than whether its manifest happens to hash the same.
# That question has one answer no matter what the exporter decides to attach.
api_fingerprint() {
    docker image inspect \
        -f '{{json .RootFS.Layers}}|{{json .Config.Env}}|{{json .Config.Cmd}}|{{json .Config.Entrypoint}}|{{json .Config.WorkingDir}}|{{json .Config.User}}' \
        dosebuddy-api:local 2>/dev/null || echo none
}

api_before=$(api_fingerprint)

BUILDX_NO_DEFAULT_ATTESTATIONS=1 docker compose build

api_after=$(api_fingerprint)

docker compose run --rm -T api alembic upgrade head

# Leave the API alone when neither its image nor its service config moved.
#
# Both halves are required. The image alone is not enough: a change to an
# environment variable or a mount in docker-compose.yml is invisible to it, and
# skipping the restart there would mean a deploy that reports success while the
# setting it shipped is not in effect. Compose records the config it built a
# container from, so the two can simply be compared.
#
# The default is to recreate. Every way this check can fail to reach an answer —
# no previous image, an older compose without `--hash`, an unreadable label —
# falls through to the ordinary path, because a needless restart is a far
# smaller fault than a change that silently did not apply.
echo "==> api"
recreate=yes
if [ "$api_before" = "$api_after" ] && [ "$api_before" != none ]; then
    want=$(docker compose config --hash=api 2>/dev/null | awk '{print $2}')
    have=$(docker inspect -f '{{index .Config.Labels "com.docker.compose.config-hash"}}' \
           dosebuddy-api 2>/dev/null || true)
    if [ -n "$want" ] && [ "$want" = "$have" ]; then
        recreate=no
    else
        echo "    image unchanged, service config changed"
    fi
else
    echo "    image contents changed"
fi
echo "    id: $(docker image inspect -f '{{.Id}}' dosebuddy-api:local 2>/dev/null || echo none)"

if [ "$recreate" = no ]; then
    echo "    nothing changed; leaving the running container alone"
    # Named services rather than a global --no-recreate: the flag is not a
    # statement about the API, and applied to everything it would also swallow
    # a change to any other service. Everything else keeps the ordinary path,
    # including services added to the compose file after this was written.
    #
    # Everything else goes first, and the order is not cosmetic. `up api` also
    # brings up what api depends on, so doing api last means Postgres and Redis
    # are settled before the thing that talks to them — the same reason compose
    # starts dependencies first when left to itself. Reversed, a Postgres that
    # did need recreating would be pulled out from under a container that had
    # just been told to keep running.
    others=$(docker compose config --services | grep -vxE 'api|worker' | tr '\n' ' ')
    if [ -n "$others" ]; then
        # shellcheck disable=SC2086
        docker compose up -d $others
    fi
    docker compose up -d --no-recreate api worker
else
    docker compose up -d
fi

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
