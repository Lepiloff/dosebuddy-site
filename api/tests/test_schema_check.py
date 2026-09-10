"""The deploy's schema check, checked.

It is the only thing that looks at the database production actually runs, so a
check that passes on a schema it should have refused is worse than no check:
it is the same green line either way.

Each case tampers with the live schema and asserts the specific complaint. The
tampering does not leak into the next test — the fixture drops and rebuilds the
schema per test, and rebuilding re-runs the DDL the models attach.
"""

import pytest
from sqlalchemy import text

from app.db.models import PARENT_IS_IMMUTABLE_TRIGGERS
from app.db.schema_check import _function_problems, _trigger_problems

pytestmark = pytest.mark.asyncio


async def test_a_schema_built_from_the_models_has_nothing_to_report(session):
    conn = await session.connection()

    assert await _trigger_problems(conn) == []
    assert await _function_problems(conn) == []


async def test_a_missing_trigger_is_named(session):
    conn = await session.connection()
    table, name, _column = PARENT_IS_IMMUTABLE_TRIGGERS[0]
    await conn.execute(text(f"DROP TRIGGER {name} ON {table}"))

    found = await _trigger_problems(conn)

    assert found == [f"{table}: trigger {name} is missing"]


async def test_a_function_that_keeps_the_name_and_drops_the_refusal_is_caught(session):
    """The replacement worth catching, and the one a trigger cannot see.

    Every trigger still exists, still names its column, still calls
    `parent_is_immutable` — and the rule is gone, because the function it calls
    now returns instead of raising. Sampling the body for `SD001` would not
    catch it either: the code is still in there, in a branch that never runs.
    """
    conn = await session.connection()
    await conn.execute(
        text(
            """
            CREATE OR REPLACE FUNCTION parent_is_immutable() RETURNS trigger
            LANGUAGE plpgsql AS $$
            BEGIN
                IF false THEN
                    RAISE EXCEPTION USING ERRCODE = 'SD001', MESSAGE = 'never';
                END IF;
                RETURN NEW;
            END;
            $$
            """
        )
    )

    assert await _trigger_problems(conn) == []   # every trigger is still there
    found = await _function_problems(conn)

    assert len(found) == 1
    assert "does not have the body the models declare" in found[0]


async def test_a_missing_function_is_named(session):
    conn = await session.connection()
    for table, name, _column in PARENT_IS_IMMUTABLE_TRIGGERS:
        await conn.execute(text(f"DROP TRIGGER {name} ON {table}"))
    await conn.execute(text("DROP FUNCTION parent_is_immutable()"))

    assert await _function_problems(conn) == [
        "function parent_is_immutable() is missing"
    ]
