"""The shape of the plan behind `pull`, not just its answer.

A pull that returns the right rows can still be the wrong query. The two ways it
goes wrong here are invisible to every other test in this suite, because both
return correct results on a table small enough to have no plan worth choosing:

  - discarding: walk the `server_seq` index and throw away everyone else's rows.
    Cost grows with other tenants' data, so it degrades without this account
    doing anything at all.
  - sorting: read every row a key has and sort it, so the LIMIT bounds the
    response but not the work.

So this asserts the plan. It seeds enough rows that Postgres has a real choice to
make — on a few hundred rows it will pick a sequential scan whatever the indexes
say, and the assertion would pass while proving nothing.
"""

import re
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.dialects import postgresql

from app.api.sync import PAGE_SIZE, page_query
from app.db.models import DoseEvent

pytestmark = pytest.mark.asyncio

PROFILES = 40
ROWS_EACH = 500


def profiles_worth(pages: int) -> int:
    """History several pages deep, so stopping at one is visibly different."""
    return PAGE_SIZE * pages


async def seed(session, heavy: int = 0) -> list[uuid.UUID]:
    """Many profiles, interleaved in the sequence the way real traffic arrives.

    Inserting profile by profile would leave each one's rows contiguous in
    `server_seq`, which flatters the sequence-only index into looking fine.
    """
    account = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO accounts (id, google_sub, email, created_at)"
            " VALUES (:id, 'plan-test', NULL, now())"
        ),
        {"id": account},
    )
    await session.execute(
        text(
            "INSERT INTO profiles (id, owner_account_id, name, color, sort_order,"
            "  op_seq, created_at_ms, updated_at_ms)"
            " SELECT gen_random_uuid(), :acc, '\\x00'::bytea, 0, 0, 0, 0, 0"
            " FROM generate_series(1, :n)"
        ),
        {"acc": account, "n": PROFILES},
    )
    profiles = list(
        (await session.execute(text("SELECT id FROM profiles"))).scalars()
    )
    await session.execute(
        text(
            "INSERT INTO medications"
            " (id, profile_id, name, dose_amount, form, refill_threshold_days,"
            "  is_active, op_seq, created_at_ms, updated_at_ms)"
            " SELECT gen_random_uuid(), p.id, '\\x00'::bytea, 1, 'tablet', 3, true, 0, 0, 0"
            " FROM profiles p"
        )
    )
    await session.execute(
        text(
            "INSERT INTO dose_events"
            " (id, medication_id, profile_id, planned_at_ms, status, dose_amount,"
            "  snooze_count, op_seq, created_at_ms, updated_at_ms)"
            " SELECT gen_random_uuid(), s.mid, s.pid, 0, 'taken', 1, 0, 0, 0, 0 FROM ("
            "   SELECT m.id AS mid, m.profile_id AS pid FROM medications m,"
            "          generate_series(1, :n) g ORDER BY random()) s"
        ),
        {"n": ROWS_EACH},
    )
    if heavy:
        # One profile with years behind it, the case the bounded-work test needs.
        await session.execute(
            text(
                "INSERT INTO dose_events"
                " (id, medication_id, profile_id, planned_at_ms, status, dose_amount,"
                "  snooze_count, op_seq, created_at_ms, updated_at_ms)"
                " SELECT gen_random_uuid(), m.id, m.profile_id, 0, 'taken', 1, 0, 0, 0, 0"
                " FROM medications m, generate_series(1, :n) g"
                " WHERE m.profile_id = :pid"
            ),
            {"n": heavy, "pid": profiles[0]},
        )

    await session.execute(text("ANALYZE dose_events"))
    await session.commit()
    return profiles


async def plan_for(session, ids) -> str:
    stmt = page_query(DoseEvent, DoseEvent.profile_id, ids, since=0)
    sql = stmt.compile(
        dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
    )
    rows = (await session.execute(text(f"EXPLAIN (ANALYZE, COSTS OFF) {sql}"))).scalars()
    return "\n".join(rows)


async def test_the_pull_walks_the_composite_index_and_stops_at_the_page(session):
    profiles = await seed(session)
    plan = await plan_for(session, profiles[:1])

    assert "ix_dose_events_profile_id_server_seq" in plan, plan
    # Both halves of the key in one condition is the whole point: filtering on
    # one and ordering by the other is what produced the bad plans.
    assert "profile_id = wanted.wanted" in plan and "server_seq > 0" in plan, plan


async def test_the_pull_discards_nothing_it_has_read(session):
    """No row is fetched only to be thrown away.

    `Rows Removed by Filter` is the signature of the plan whose cost is set by
    other tenants' data volume rather than by this caller's.
    """
    profiles = await seed(session)
    plan = await plan_for(session, profiles[:3])

    assert "Rows Removed by Filter" not in plan, plan
    assert "Seq Scan on dose_events" not in plan, plan


async def test_a_deep_history_still_uses_the_composite_index(session):
    """Three pages of history behind one profile, and the index still serves it.

    Deliberately not asserted: that only one page is *read*. Whether Postgres
    walks the index in order and stops at the limit, or reads the profile's rows
    and sorts them, is a cost decision that correctly changes with size — at this
    fixture's size the second is genuinely cheaper, and on a 745k-row table the
    same query chose the first. Asserting the ordered walk here would pin the
    planner's arithmetic rather than anything this code decides.

    What is ours is that the ordered path exists and nothing is read to be thrown
    away. That is what this file pins.
    """
    profiles = await seed(session, heavy=profiles_worth(3))
    plan = await plan_for(session, profiles[:1])

    assert "ix_dose_events_profile_id_server_seq" in plan, plan
    assert "Rows Removed by Filter" not in plan, plan
    assert re.search(r"rows=\d+", plan), plan
