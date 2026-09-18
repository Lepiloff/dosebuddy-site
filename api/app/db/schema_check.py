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
* that function still has the body the models declare.

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


async def problems() -> list[str]:
    engine = create_async_engine(str(get_settings().database_url))
    try:
        async with engine.connect() as conn:
            return (
                await _revision_problems(conn)
                + await _trigger_problems(conn)
                + await _function_problems(conn)
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
        f"function body as declared, authority lease {lease}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
