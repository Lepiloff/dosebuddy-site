"""Alembic environment.

Wired up but empty: there are no revisions yet, and that is the point. The
server schema mirrors the device data model, the device leads (spec 0.5), and
the schema itself is an open decision in part 6 to be agreed rather than
guessed at. This exists so that when the model is settled, the first migration
is a one-liner instead of a day of plumbing.
"""

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.config import get_settings
from app.db.session import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Models get imported here once they exist, so that autogenerate can see them.
target_metadata = Base.metadata


def _database_url() -> str:
    return str(get_settings().database_url)


def run_migrations_offline() -> None:
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def _run(connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    engine = create_async_engine(_database_url(), poolclass=None)
    async with engine.connect() as connection:
        await connection.run_sync(_run)
    await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
