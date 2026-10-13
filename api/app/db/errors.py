"""Stable PostgreSQL error codes through SQLAlchemy and asyncpg wrappers."""

from sqlalchemy.exc import DBAPIError


def sqlstate(exc: DBAPIError) -> str | None:
    # asyncpg's adapted exception normally exposes sqlstate directly. Follow
    # explicit causes too; never infer a code from private, mutable messages.
    current = exc.orig
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        code = getattr(current, "sqlstate", None) or getattr(current, "pgcode", None)
        if code:
            return code
        current = getattr(current, "__cause__", None)
    return None
