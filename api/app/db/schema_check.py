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
  table, guarding the right column, and calls the shared function.

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
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.config import get_settings
from app.db.models import PARENT_IS_IMMUTABLE_TRIGGERS

TRIGGER_QUERY = text(
    "SELECT c.relname, t.tgname, pg_get_triggerdef(t.oid) "
    "FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid "
    "WHERE NOT t.tgisinternal AND c.relnamespace = 'public'::regnamespace"
)


async def problems() -> list[str]:
    found: list[str] = []
    expected_head = ScriptDirectory.from_config(Config("alembic.ini")).get_current_head()

    engine = create_async_engine(str(get_settings().database_url))
    try:
        async with engine.connect() as conn:
            at = (
                await conn.execute(text("SELECT version_num FROM alembic_version"))
            ).scalars().all()
            if at != [expected_head]:
                found.append(
                    f"alembic_version is {at or 'empty'}, expected [{expected_head!r}]"
                )

            triggers = {
                (table, name): definition
                for table, name, definition in (await conn.execute(TRIGGER_QUERY)).all()
            }
            for table, name, column in PARENT_IS_IMMUTABLE_TRIGGERS:
                definition = triggers.get((table, name))
                if definition is None:
                    found.append(f"{table}: trigger {name} is missing")
                    continue
                # The column has to appear in both halves — the WHEN clause is
                # what refuses, the argument is only what the message says — and
                # a trigger guarding the wrong column would pass a check that
                # looked at either one alone.
                for fragment in (
                    f"new.{column} IS DISTINCT FROM old.{column}",
                    f"parent_is_immutable('{column}')",
                ):
                    if fragment not in definition:
                        found.append(f"{table}.{name}: expected {fragment} in {definition}")
    finally:
        await engine.dispose()
    return found


def main() -> int:
    found = asyncio.run(problems())
    for line in found:
        print(f"schema check: {line}", file=sys.stderr)
    if found:
        print(f"schema check FAILED with {len(found)} problem(s)", file=sys.stderr)
        return 1
    print(
        "schema check: at head, "
        f"{len(PARENT_IS_IMMUTABLE_TRIGGERS)} parent-immutability triggers in place"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
