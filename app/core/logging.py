import logging
import sys

import structlog

from app.core.config import get_settings


def configure_logging() -> None:
    """Structured JSON logs.

    The system this replaces wrote 94 MB of unrotated free-text log in which a
    ten-day regression went unnoticed. Events here are machine-readable so the
    dashboard, not a human tailing a file, is the primary way to see state.
    """
    settings = get_settings()
    level = getattr(logging, settings.log_level.upper(), logging.INFO)

    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=level)

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )
