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

from app.db.models import PARENT_IS_IMMUTABLE_TRIGGERS, SET_ONCE_TRIGGERS
from app.db.schema_check import (
    _alert_eligibility_problems, _credential_problems, _function_problems, _publication_problems, _trigger_problems,
)

pytestmark = pytest.mark.asyncio


async def test_a_schema_built_from_the_models_has_nothing_to_report(session):
    conn = await session.connection()

    assert await _trigger_problems(conn) == []
    assert await _function_problems(conn) == []
    assert await _publication_problems(conn) == []
    assert await _credential_problems(conn) == []
    assert await _alert_eligibility_problems(conn) == []


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
    for table, name, _column in PARENT_IS_IMMUTABLE_TRIGGERS + SET_ONCE_TRIGGERS:
        await conn.execute(text(f"DROP TRIGGER {name} ON {table}"))
    await conn.execute(text("DROP FUNCTION parent_is_immutable()"))

    assert await _function_problems(conn) == [
        "function parent_is_immutable() is missing"
    ]


@pytest.mark.parametrize("tamper, complaint", [
    ("DROP TRIGGER devices_sync_publication ON devices",
     "devices: sync publication statement fence is missing or changed"),
    ("ALTER TABLE schedules DISABLE TRIGGER schedules_sync_publication",
     "schedules: sync publication statement fence is missing or changed"),
    ("ALTER SEQUENCE server_seq CACHE 4",
     "server_seq must use INCREMENT 1 CACHE 1 NO CYCLE"),
    ("ALTER SEQUENCE server_seq INCREMENT BY 2",
     "server_seq must use INCREMENT 1 CACHE 1 NO CYCLE"),
    ("ALTER SEQUENCE server_seq CYCLE",
     "server_seq must use INCREMENT 1 CACHE 1 NO CYCLE"),
    ("ALTER TABLE stock_events ALTER COLUMN server_seq SET DEFAULT nextval('server_seq')",
     "stock_events.server_seq must default to next_server_seq()"),
    ("CREATE OR REPLACE FUNCTION sync_publication_lock() RETURNS void "
     "LANGUAGE plpgsql AS $$ BEGIN PERFORM pg_advisory_xact_lock(1396985678, 1); END; $$",
     "function sync_publication_lock() is missing or changed"),
    ("CREATE OR REPLACE FUNCTION next_server_seq() RETURNS bigint "
     "LANGUAGE plpgsql AS $$ BEGIN RETURN nextval('server_seq'); END; $$",
     "function next_server_seq() is missing or changed"),
    ("CREATE OR REPLACE FUNCTION sync_publication_fence() RETURNS trigger "
     "LANGUAGE plpgsql AS $$ BEGIN RETURN NULL; END; $$",
     "function sync_publication_fence() is missing or changed"),
])
async def test_a_broken_publication_fence_or_sequence_is_caught(session, tamper, complaint):
    conn = await session.connection()
    await conn.execute(text(tamper))
    assert await _publication_problems(conn) == [complaint]


async def test_a_row_fence_is_not_mistaken_for_a_statement_fence(session):
    conn = await session.connection()
    await conn.execute(text("DROP TRIGGER medications_sync_publication ON medications"))
    await conn.execute(text(
        "CREATE TRIGGER medications_sync_publication "
        "BEFORE INSERT OR UPDATE OR DELETE ON medications "
        "FOR EACH ROW EXECUTE FUNCTION sync_publication_fence()"
    ))
    assert await _publication_problems(conn) == [
        "medications: sync publication statement fence is missing or changed"
    ]


@pytest.mark.parametrize("tamper, complaint", [
    ("ALTER TABLE devices DROP COLUMN credential_generation",
     "devices.credential_generation must be BIGINT NOT NULL DEFAULT 0"),
    ("ALTER TABLE devices ALTER COLUMN credential_generation DROP NOT NULL",
     "devices.credential_generation must be BIGINT NOT NULL DEFAULT 0"),
    ("ALTER TABLE refresh_tokens ALTER COLUMN credential_generation SET DEFAULT 1",
     "refresh_tokens.credential_generation must be BIGINT NOT NULL DEFAULT 0"),
    ("ALTER TABLE devices DROP CONSTRAINT ck_devices_credential_generation",
     "devices: credential generation CHECK is missing or changed"),
    ("ALTER TABLE refresh_tokens DROP CONSTRAINT ck_refresh_tokens_credential_generation",
     "refresh_tokens: credential generation CHECK is missing or changed"),
])
async def test_a_broken_credential_fence_is_caught(session, tamper, complaint):
    conn = await session.connection()
    await conn.execute(text(tamper))
    found = await _credential_problems(conn)
    assert complaint in found
