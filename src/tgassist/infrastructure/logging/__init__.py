"""Structured logging with central redaction.

Public surface of the logging subsystem. The sensitivity policy it applies lives
in ``tgassist.domain.services.sensitivity``; see ``docs/SECURITY.md`` section 9.
"""

from tgassist.infrastructure.logging.redaction import (
    build_redaction_processor,
    redact_entry,
    redact_value,
)
from tgassist.infrastructure.logging.setup import (
    LOG_FILE_NAME,
    configure_logging,
    get_logger,
    purge_expired_logs,
)

__all__ = [
    "LOG_FILE_NAME",
    "build_redaction_processor",
    "configure_logging",
    "get_logger",
    "purge_expired_logs",
    "redact_entry",
    "redact_value",
]
