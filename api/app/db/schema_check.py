"""Assert the schema on this database is the one the code expects.

Run by the deploy, after `alembic upgrade head` and before anything is
restarted (`deploy/remote-deploy.sh`). A failure here stops the deploy with the
old code still serving, which is the safe end of a bad migration.

It exists because nothing else asks the question. `/health/ready` reports that
the database answers and Redis answers — both true of a database whose
migration silently did not apply. CI compares the migrated schema against the
models, but CI runs on a scratch database in a container, not on the one holding
the data. The gap between them is exactly where a half-applied migration lives.

What it checks is what cannot be seen from outside and is load-bearing:

* the database is at the head revision the code ships, read from the migration
  scripts rather than hard-coded, so this file needs no edit per migration;
* every trigger the models declare exists, on the right table, guarding the
  right column, with the right condition, and calling the shared function —
  the parent-immutability ones and the set-once ones, which differ only in
  that WHEN clause and so are the pair a check comparing names alone would
  read as interchangeable;
* those functions still have the bodies the models declare;
* sync publication statement fences run before defaults and row locks, every
  mirror default uses the fenced allocator, and the shared sequence uses
  INCREMENT 1 CACHE 1 NO CYCLE (cached ranges could reverse publication order).

It also prints the authority lease as **this box** has it, which is a different
statement from the default in the source. A setting that decides whether the
server may take reminders off a phone should be read from the environment that
will act on it, and read out loud, in the log of the deploy that installs it.
Asked for by the app track on 2026-09-17.

The last one was missing until the app track's review asked for it, and what it
left out is the shape of a proof that stops one step early: a trigger proves the
*name* it calls, and a name can be pointed at anything. One `CREATE OR REPLACE`
by hand, or an image built from a branch where the body differs, and every other
check here stays green while the rule itself is gone. The body is compared as
text rather than sampled for a keyword, because the replacement worth catching
is the one that keeps `SD001` in the source and returns NEW without raising.

Requested by the app track's review, 2026-09-09: the client's quarantine rule
assumes the server refuses a parent move, and a server that quietly did not
would look exactly like one that does until a dose went to the wrong medicine.
"""

from __future__ import annotations

import asyncio
import re
import sys

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.config import get_settings
from app.db.models import (
    PARENT_IS_IMMUTABLE_FUNCTION,
    PARENT_IS_IMMUTABLE_TRIGGERS,
    SET_ONCE_TRIGGERS,
)

from app.db.publication import (
    MIRROR_TABLES,
    NEXT_SERVER_SEQ_FUNCTION,
    PUBLICATION_FENCE_FUNCTION,
    PUBLICATION_LOCK_FUNCTION,
    PUBLICATION_TABLES,
)
from app.db.alert_eligibility import (
    RETIRE_CAREGIVER_FUNCTION, RETIRE_CAREGIVER_TRIGGERS,
)

TRIGGER_QUERY = text(
    "SELECT c.relname, t.tgname, pg_get_triggerdef(t.oid) "
    "FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid "
    "WHERE NOT t.tgisinternal AND c.relnamespace = 'public'::regnamespace"
)

FUNCTION_QUERY = text("SELECT pg_get_functiondef('parent_is_immutable()'::regprocedure)")


def _flat(sql: str) -> str:
    """Whitespace is not schema: two spaces and a newline say the same thing."""
    return " ".join(sql.split())


async def _revision_problems(conn) -> list[str]:
    expected = ScriptDirectory.from_config(Config("alembic.ini")).get_current_head()
    at = (
        await conn.execute(text("SELECT version_num FROM alembic_version"))
    ).scalars().all()
    if at == [expected]:
        return []
    return [f"alembic_version is {at or 'empty'}, expected [{expected!r}]"]


def _plainly(sql: str) -> str:
    """The same condition, written the one way both sides can be compared in.

    Postgres does not hand back the WHEN clause as it was written: it adds its
    own parentheses and casts every varchar comparison to text, so the trigger
    that reads `NEW.dose_key IS DISTINCT FROM OLD.dose_key` in the models comes
    back as `(new.dose_key)::text IS DISTINCT FROM (old.dose_key)::text`. Both
    sides are flattened rather than the expectation being written in pg's
    dialect, because pg's dialect is not ours to predict — it depends on the
    column type, and a check that has to be re-guessed whenever a column type
    changes is a check that will one day be relaxed to make a deploy pass.
    """
    return re.sub(r"::\w+", "", sql).replace("(", "").replace(")", "")


async def _trigger_problems(conn) -> list[str]:
    found: list[str] = []
    triggers = {
        (table, name): definition
        for table, name, definition in (await conn.execute(TRIGGER_QUERY)).all()
    }
    expected = [
        (table, name, f"new.{column} IS DISTINCT FROM old.{column}", column)
        for table, name, column in PARENT_IS_IMMUTABLE_TRIGGERS
    ] + [
        (
            table,
            name,
            f"old.{column} IS NOT NULL AND new.{column} IS DISTINCT FROM old.{column}",
            column,
        )
        for table, name, column in SET_ONCE_TRIGGERS
    ]
    for table, name, condition, column in expected:
        definition = triggers.get((table, name))
        if definition is None:
            found.append(f"{table}: trigger {name} is missing")
            continue
        definition = _plainly(definition)
        # The column has to appear in both halves — the WHEN clause is what
        # refuses, the argument is only what the message says — and a trigger
        # guarding the wrong column would pass a check that looked at either one
        # alone.
        for fragment in (condition, f"parent_is_immutable('{column}')"):
            if _plainly(fragment) not in definition:
                found.append(f"{table}.{name}: expected {fragment} in {definition}")
    return found


async def _function_problems(conn) -> list[str]:
    # Only the body is compared. The wrapper Postgres prints back is its own —
    # the schema prefix, the dollar-quote tag, where the newlines fall — and
    # differs from the source without meaning anything.
    try:
        live = (await conn.execute(FUNCTION_QUERY)).scalar_one()
    except DBAPIError:
        return ["function parent_is_immutable() is missing"]

    body = _flat(PARENT_IS_IMMUTABLE_FUNCTION.split("$$")[1])
    if body in _flat(live):
        return []
    return [
        "function parent_is_immutable() does not have the body the models "
        f"declare; the database has: {_flat(live)}"
    ]


async def _publication_problems(conn) -> list[str]:
    """The fence must run before row locks/defaults, with an uncached sequence."""
    found: list[str] = []
    triggers = {
        table: (kind, enabled, function, arguments, condition)
        for table, kind, enabled, function, arguments, condition in (
            await conn.execute(text(
                "SELECT c.relname, t.tgtype, t.tgenabled::text, p.proname, "
                "t.tgnargs, t.tgqual IS NOT NULL "
                "FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid "
                "JOIN pg_proc p ON p.oid = t.tgfoid "
                "WHERE c.relnamespace = 'public'::regnamespace "
                "AND t.tgname = c.relname || '_sync_publication' "
                "AND t.tgfoid = to_regprocedure('sync_publication_fence()') "
                "AND t.tgattr::text = ''"
            ))
        ).all()
    }
    # pg_trigger's bitmask: BEFORE (2), INSERT (4), DELETE (8), UPDATE (16),
    # without ROW (1), so precisely 30. A row trigger locks too late.
    for table in PUBLICATION_TABLES:
        if triggers.get(table) != (30, "O", "sync_publication_fence", 0, False):
            found.append(f"{table}: sync publication statement fence is missing or changed")

    for signature, definition, returns in (
        ("sync_publication_lock()", PUBLICATION_LOCK_FUNCTION, "void"),
        ("next_server_seq()", NEXT_SERVER_SEQ_FUNCTION, "bigint"),
        ("sync_publication_fence()", PUBLICATION_FENCE_FUNCTION, "trigger"),
    ):
        row = (await conn.execute(text(
            "SELECT pg_get_functiondef(p.oid), p.prorettype::regtype::text, p.provolatile::text "
            "FROM pg_proc p WHERE p.oid = to_regprocedure(:signature)"
        ), {"signature": signature})).one_or_none()
        if (row is None or row[1:] != (returns, "v")
                or _flat(definition.split("$$")[1]) not in _flat(row[0])):
            found.append(f"function {signature} is missing or changed")

    sequence = (await conn.execute(text(
        "SELECT increment_by, cache_size, cycle FROM pg_sequences "
        "WHERE schemaname = 'public' AND sequencename = 'server_seq'"
    ))).one_or_none()
    if sequence != (1, 1, False):
        found.append("server_seq must use INCREMENT 1 CACHE 1 NO CYCLE")

    defaults = dict((await conn.execute(text(
        "SELECT table_name, column_default FROM information_schema.columns "
        "WHERE table_schema = 'public' AND column_name = 'server_seq'"
    ))).all())
    for table in MIRROR_TABLES:
        if defaults.get(table) != "next_server_seq()":
            found.append(f"{table}.server_seq must default to next_server_seq()")
    return found


async def _credential_problems(conn) -> list[str]:
    """Revocation depends on a durable non-null, non-negative generation."""
    found = []
    columns = dict((table, (kind, nullable, default)) for table, kind, nullable, default in (
        await conn.execute(text(
            "SELECT table_name, data_type, is_nullable, column_default "
            "FROM information_schema.columns WHERE table_schema='public' "
            "AND column_name='credential_generation'"
        ))
    ).all())
    constraints = dict((table, (definition, validated)) for table, definition, validated in (
        await conn.execute(text(
            "SELECT c.relname, pg_get_constraintdef(k.oid), k.convalidated "
            "FROM pg_constraint k JOIN pg_class c ON c.oid=k.conrelid "
            "WHERE c.relnamespace='public'::regnamespace "
            "AND k.conname='ck_' || c.relname || '_credential_generation' AND k.contype='c'"
        ))
    ).all())
    for table in ("devices", "refresh_tokens"):
        if columns.get(table) != ("bigint", "NO", "0"):
            found.append(f"{table}.credential_generation must be BIGINT NOT NULL DEFAULT 0")
        constraint = constraints.get(table)
        if (constraint is None or not constraint[1]
                or _plainly(constraint[0]) != "CHECK credential_generation >= 0"):
            found.append(f"{table}: credential generation CHECK is missing or changed")
    return found


async def _alert_eligibility_problems(conn) -> list[str]:
    """Successful consent changes must durably retire pending caregiver debt."""
    found = []
    triggers = {
        (table, name): (definition, kind, enabled, function, arguments, columns)
        for table, name, definition, kind, enabled, function, arguments, columns in (
            await conn.execute(text(
                "SELECT c.relname, t.tgname, pg_get_triggerdef(t.oid), t.tgtype, "
                "t.tgenabled::text, p.proname, t.tgnargs, t.tgattr::text "
                "FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid "
                "JOIN pg_proc p ON p.oid=t.tgfoid "
                "WHERE NOT t.tgisinternal AND c.relnamespace='public'::regnamespace"
            ))
        ).all()
    }
    for table, name, operation, condition in RETIRE_CAREGIVER_TRIGGERS:
        row = triggers.get((table, name))
        # AFTER ROW UPDATE=17, DELETE=9. Column-restricted or disabled variants
        # must not pass: alternative writers need the same lifecycle boundary.
        if (row is None or row[1:] != (
            17 if operation == "UPDATE" else 9, "O", "retire_caregiver_deliveries", 0, "",
        ) or f"when {_plainly(condition).lower()} execute function" not in _plainly(row[0]).lower()):
            found.append(f"{table}: caregiver retirement trigger {name} is missing or changed")
    row = (await conn.execute(text(
        "SELECT pg_get_functiondef(p.oid), p.prorettype::regtype::text, p.provolatile::text "
        "FROM pg_proc p WHERE p.oid=to_regprocedure('retire_caregiver_deliveries()')"
    ))).one_or_none()
    if (row is None or row[1:] != ("trigger", "v")
            or _flat(RETIRE_CAREGIVER_FUNCTION.split("$$")[1]) not in _flat(row[0])):
        found.append("function retire_caregiver_deliveries() is missing or changed")
    return found


async def problems() -> list[str]:
    engine = create_async_engine(str(get_settings().database_url))
    try:
        async with engine.connect() as conn:
            return (
                await _revision_problems(conn)
                + await _trigger_problems(conn)
                + await _function_problems(conn)
                + await _publication_problems(conn)
                + await _credential_problems(conn)
                + await _alert_eligibility_problems(conn)
            )
    finally:
        await engine.dispose()


def main() -> int:
    found = asyncio.run(problems())
    for line in found:
        print(f"schema check: {line}", file=sys.stderr)
    if found:
        print(f"schema check FAILED with {len(found)} problem(s)", file=sys.stderr)
        return 1
    minutes = get_settings().authority_lease_minutes
    lease = f"{minutes} min" if minutes else "off"
    print(
        "schema check: at head, "
        f"{len(PARENT_IS_IMMUTABLE_TRIGGERS)} parent-immutability "
        f"and {len(SET_ONCE_TRIGGERS)} set-once triggers in place, "
        f"function bodies and sync publication fences as declared, "
        f"server_seq INCREMENT 1 CACHE 1 NO CYCLE, durable credential generations, "
        f"caregiver consent retirement, "
        f"authority lease {lease}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
