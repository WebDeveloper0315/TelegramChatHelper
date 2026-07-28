"""Structured logging configuration.

structlog is routed through the standard library so that log records emitted by
third-party packages pass through the same processor chain -- including
redaction. A redaction step that only covered our own call sites would leave the
larger surface unguarded.

Two sinks:

* **Console** -- human-readable, for a developer at a terminal.
* **File** -- size-rotating, JSON by default, for diagnosis after the fact.
"""

from __future__ import annotations

import logging
import logging.handlers
import sys
import time
from pathlib import Path
from typing import Any

import structlog

from tgassist.infrastructure.config.settings import AppConfig, LoggingSection
from tgassist.infrastructure.logging.redaction import build_redaction_processor

LOG_FILE_NAME = "tgassist.log"

_BYTES_PER_MB = 1024 * 1024


def configure_logging(config: AppConfig) -> None:
    """Configure structlog and the standard library logging hierarchy.

    Safe to call more than once; each call replaces the previous configuration.

    Args:
        config: The resolved application configuration.
    """
    section = config.logging
    log_dir = config.log_dir

    shared_processors = _shared_processors(section)

    structlog.configure(
        processors=[
            *shared_processors,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    root = logging.getLogger()
    _reset_handlers(root)
    root.setLevel(section.level.value)

    if section.console_enabled:
        root.addHandler(_console_handler(section, shared_processors))
    if section.file_enabled:
        root.addHandler(_file_handler(section, shared_processors, log_dir))

    for logger_name, level in section.component_levels.items():
        logging.getLogger(logger_name).setLevel(level.value)

    if section.diagnostic_mode:
        structlog.get_logger(__name__).warning(
            "diagnostic_logging_enabled",
            detail="Message content will be written to logs until this is disabled.",
        )


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    """Return a bound logger.

    Args:
        name: Logger name. Defaults to the caller's module name.
    """
    logger: structlog.stdlib.BoundLogger = structlog.get_logger(name)
    return logger


def purge_expired_logs(log_dir: Path, retention_days: int) -> int:
    """Delete rotated log files older than the retention period.

    Size-based rotation bounds disk usage; this bounds how long records are
    retained, which is the privacy-relevant limit.

    Args:
        log_dir: Directory holding log files.
        retention_days: Maximum age in days.

    Returns:
        The number of files removed.
    """
    if not log_dir.is_dir():
        return 0

    cutoff = time.time() - (retention_days * 86400)
    removed = 0
    for path in log_dir.glob(f"{LOG_FILE_NAME}*"):
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
                removed += 1
        except OSError:
            # A locked or vanished file must not abort the whole cleanup.
            continue
    return removed


def _shared_processors(section: LoggingSection) -> list[Any]:
    """Build the processor chain applied to every record from every source."""
    return [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        # Redaction runs last, so it also covers fields added by earlier
        # processors and by bound context.
        build_redaction_processor(allow_content=section.diagnostic_mode),
    ]


def _console_handler(section: LoggingSection, shared: list[Any]) -> logging.Handler:
    handler = logging.StreamHandler(stream=sys.stderr)
    handler.setLevel(section.level.value)
    handler.setFormatter(
        structlog.stdlib.ProcessorFormatter(
            foreign_pre_chain=shared,
            processors=[
                structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                structlog.dev.ConsoleRenderer(colors=sys.stderr.isatty()),
            ],
        )
    )
    return handler


def _file_handler(section: LoggingSection, shared: list[Any], log_dir: Path) -> logging.Handler:
    log_dir.mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(
        filename=log_dir / LOG_FILE_NAME,
        maxBytes=section.max_file_mb * _BYTES_PER_MB,
        backupCount=section.backup_count,
        encoding="utf-8",
        delay=True,
    )
    handler.setLevel(section.level.value)

    renderer: Any = (
        structlog.processors.JSONRenderer()
        if section.format == "json"
        else structlog.dev.ConsoleRenderer(colors=False)
    )
    handler.setFormatter(
        structlog.stdlib.ProcessorFormatter(
            foreign_pre_chain=shared,
            processors=[
                structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                structlog.processors.format_exc_info,
                renderer,
            ],
        )
    )
    return handler


def _reset_handlers(logger: logging.Logger) -> None:
    """Close and remove existing handlers so reconfiguration cannot duplicate output."""
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()
