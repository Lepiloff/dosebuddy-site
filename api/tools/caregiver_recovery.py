"""Explicit coverage operation. Never called by API startup or worker.main."""

import argparse
import asyncio
import json

from sqlalchemy import select

from app.core.config import get_settings
from app.core.caregiver_cursor import CursorCodec
from app.db.models import CaregiverRecoveryControl
from app.db.schema_check import problems
from app.db.session import create_engine, create_sessionmaker
from app.services.caregiver_recovery import close_coverage, enable_coverage, set_reader


async def run(action: str, writers_verified: bool) -> dict:
    if action in ("enable", "reader-enable") and not writers_verified:
        raise ValueError("--all-writers-upgraded requires the reviewed cutover checks")
    if action in ("enable", "reader-enable") and await problems():
        raise ValueError("Schema guards must pass before coverage enable")
    if action == "reader-enable":
        CursorCodec(get_settings())
    engine = create_engine(get_settings())
    try:
        async with create_sessionmaker(engine)() as session:
            if action == "enable":
                control = await enable_coverage(session, writers_verified=writers_verified)
            elif action == "close":
                control = await close_coverage(session)
            elif action in ("reader-enable", "reader-disable"):
                control = await set_reader(session, enabled=action == "reader-enable", writers_verified=writers_verified)
            else:
                control = (await session.execute(select(CaregiverRecoveryControl))).scalar_one()
            result = {"producer_enabled": control.producer_enabled,
                      "reader_enabled": control.reader_enabled,
                      "committed_frontier": str(control.committed_frontier),
                      "epoch": str(control.epoch) if control.epoch else None,
                      "coverage_started_at": control.coverage_started_at.isoformat() if control.coverage_started_at else None,
                      "closed_at": control.closed_at.isoformat() if control.closed_at else None}
            await session.commit()
            return result
    finally:
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("status", "enable", "close", "reader-enable", "reader-disable"))
    parser.add_argument("--all-writers-upgraded", action="store_true")
    args = parser.parse_args()
    print(json.dumps(asyncio.run(run(args.action, args.all_writers_upgraded))))


if __name__ == "__main__":
    main()
