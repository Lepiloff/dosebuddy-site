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
* every parent-immutability trigger the models declare exists, on the right
  table, guarding the right column, and calls the shared function;
* that function still has the body the models declare.

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
import sys

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.config import get_settings
from app.db.models import PARENT_IS_IMMUTABLE_FUNCTION, PARENT_IS_IMMUTABLE_TRIGGERS

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


async def _trigger_problems(conn) -> list[str]:
    found: list[str] = []
    triggers = {
        (table, name): definition
        for table, name, definition in (await conn.execute(TRIGGER_QUERY)).all()
    }
    for table, name, column in PARENT_IS_IMMUTABLE_TRIGGERS:
        definition = triggers.get((table, name))
        if definition is None:
            found.append(f"{table}: trigger {name} is missing")
            continue
        # The column has to appear in both halves — the WHEN clause is what
        # refuses, the argument is only what the message says — and a trigger
        # guarding the wrong column would pass a check that looked at either one
        # alone.
        for fragment in (
            f"new.{column} IS DISTINCT FROM old.{column}",
            f"parent_is_immutable('{column}')",
        ):
            if fragment not in definition:
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
    print(
        "schema check: at head, "
        f"{len(PARENT_IS_IMMUTABLE_TRIGGERS)} parent-immutability triggers in place, "
        "function body as declared"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
