#!/usr/bin/env bash
#
# Postgres backups.
#
#   ./backup.sh dump      take one, encrypted, and rotate old ones
#   ./backup.sh verify    restore the newest into a scratch database and check it
#   ./backup.sh list      what exists
#
# Two things worth saying plainly.
#
# **A backup nobody has restored is not a backup.** `verify` exists because the
# failure mode of backups is not "the file is missing" — it is a file that has
# been written faithfully every night for a year and cannot be restored. It runs
# on its own schedule, not only when someone remembers.
#
# **This layer lives on the box it protects.** It covers the likely disasters —
# a bad migration, a mistaken delete, a corrupted table — and not the one where
# the instance is gone. That one is covered by EBS snapshots, configured in the
# console with no credentials on the box (deploy/README.md). Two layers, each
# covering what the other cannot.

set -euo pipefail
exec </dev/null

cd "$(dirname "$0")"

BACKUP_DIR=${BACKUP_DIR:-/srv/dosebuddy/backups}
KEEP_DAYS=${KEEP_DAYS:-14}
CONTAINER=dosebuddy-postgres

# shellcheck disable=SC1091
set -a; . ./.env; set +a

: "${POSTGRES_USER:?}" "${POSTGRES_DB:?}"
: "${BACKUP_PASSPHRASE:?BACKUP_PASSPHRASE is not set — see deploy/env.example}"

mkdir -p "$BACKUP_DIR"

log() { printf '%s  %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }

cmd_dump() {
    local stamp file
    stamp=$(date -u +%Y%m%dT%H%M%SZ)
    file="$BACKUP_DIR/dosebuddy-$stamp.dump.enc"

    # -Fc so a restore can be selective, and so the dump is compressed before it
    # is encrypted — the other order compresses noise and achieves nothing.
    docker exec "$CONTAINER" pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc \
        | openssl enc -aes-256-cbc -pbkdf2 -iter 200000 -salt \
            -pass env:BACKUP_PASSPHRASE -out "$file.partial"

    # Rename only once it is whole. A dump interrupted half-way must never be
    # mistaken for the newest good one by `verify` or by a person in a hurry.
    mv "$file.partial" "$file"
    chmod 600 "$file"
    log "wrote $file ($(du -h "$file" | cut -f1))"

    find "$BACKUP_DIR" -name 'dosebuddy-*.dump.enc' -mtime "+$KEEP_DAYS" -print -delete \
        | sed 's/^/  rotated out /'
}

cmd_verify() {
    local newest scratch
    newest=$(find "$BACKUP_DIR" -name 'dosebuddy-*.dump.enc' | sort | tail -1)
    [ -n "$newest" ] || { log "no backups to verify"; exit 1; }

    scratch="verify_$(date -u +%s)"
    log "restoring $(basename "$newest") into $scratch"

    docker exec "$CONTAINER" psql -U "$POSTGRES_USER" -d postgres \
        -c "CREATE DATABASE $scratch" >/dev/null

    # Restore into a scratch database rather than the live one, obviously — but
    # also so the check exercises a real restore rather than reading the file.
    if openssl enc -d -aes-256-cbc -pbkdf2 -iter 200000 \
            -pass env:BACKUP_PASSPHRASE -in "$newest" \
        | docker exec -i "$CONTAINER" pg_restore -U "$POSTGRES_USER" -d "$scratch" \
            --no-owner --no-privileges >/dev/null 2>&1; then
        :
    else
        log "RESTORE FAILED for $newest"
        docker exec "$CONTAINER" psql -U "$POSTGRES_USER" -d postgres \
            -c "DROP DATABASE IF EXISTS $scratch" >/dev/null
        exit 1
    fi

    # Restoring without error is not the same as restoring something. An empty
    # dump restores perfectly.
    local tables
    tables=$(docker exec "$CONTAINER" psql -U "$POSTGRES_USER" -d "$scratch" -tAc \
        "SELECT count(*) FROM pg_tables WHERE schemaname='public'")

    docker exec "$CONTAINER" psql -U "$POSTGRES_USER" -d postgres \
        -c "DROP DATABASE $scratch" >/dev/null

    if [ "$tables" -lt 6 ]; then
        log "RESTORE SUSPECT: only $tables tables came back"
        exit 1
    fi
    log "restore ok: $tables tables"
}

cmd_list() {
    find "$BACKUP_DIR" -name 'dosebuddy-*.dump.enc' -printf '%TY-%Tm-%Td %TH:%TM  %10s  %p\n' \
        | sort
}

case "${1:-}" in
    dump)   cmd_dump ;;
    verify) cmd_verify ;;
    list)   cmd_list ;;
    *)      echo "usage: $0 {dump|verify|list}" >&2; exit 64 ;;
esac
