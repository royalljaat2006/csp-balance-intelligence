"""
structlog wiring — Phase 12. `structlog` was a declared dependency with
nothing actually configured; this is what makes it real.

`json_output=True` (production) renders one JSON object per line — safe for
a log aggregator to parse, and impossible to accidentally leak a secret into
via string formatting since every field is a named key, not string-interpolated.
`json_output=False` (development) renders human-readable console output.

Neither renderer is ever told about request headers, bodies, or credentials
— see api/middleware.py's docstring for the concrete "never log" list.
"""

from __future__ import annotations

import structlog


def build_logging_config(*, json_output: bool) -> dict:
    renderer = (
        structlog.processors.JSONRenderer() if json_output else structlog.dev.ConsoleRenderer()
    )

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.stdlib.add_log_level,
            structlog.stdlib.PositionalArgumentsFormatter(),
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    return {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {
            "structured": {
                "()": structlog.stdlib.ProcessorFormatter,
                "processor": renderer,
                "foreign_pre_chain": [
                    structlog.stdlib.add_log_level,
                    structlog.processors.TimeStamper(fmt="iso"),
                ],
            },
        },
        "handlers": {
            "console": {"class": "logging.StreamHandler", "formatter": "structured"},
        },
        "root": {"handlers": ["console"], "level": "INFO"},
        "loggers": {
            "django": {"handlers": ["console"], "level": "INFO", "propagate": False},
            "api.requests": {"handlers": ["console"], "level": "INFO", "propagate": False},
            "ingestion": {"handlers": ["console"], "level": "INFO", "propagate": False},
        },
    }
