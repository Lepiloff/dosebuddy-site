"""Logging setup.

JSON in production so lines can be searched; human-readable locally.

One rule this service does not get to relax: **no medication names, profile
names, or dose history in a log line.** From v1.1 that data lives on the server
and it is article 9 material under GDPR (spec part 2.1). Logs are copied,
shipped and kept far more casually than a database is, so the safe assumption
is that anything logged is retained forever and read by someone who should not
see it. Log identifiers, not contents.
"""

import logging
import sys

import structlog

from app.core.config import Settings


def setup_logging(settings: Settings) -> None:
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
    )

    shared = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]

    renderer = (
        structlog.processors.JSONRenderer()
        if settings.environment == "production"
        else structlog.dev.ConsoleRenderer()
    )

    structlog.configure(
        processors=[*shared, renderer],
        wrapper_class=structlog.stdlib.BoundLogger,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )
