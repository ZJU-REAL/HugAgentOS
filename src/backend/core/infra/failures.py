"""Observable best-effort failures without logging payloads or credentials."""
import logging

logger = logging.getLogger(__name__)


def record_best_effort_failure(operation: str, error: Exception) -> None:
    """Preserve a caller's fallback while identifying the failed operation.

    Exception messages and tracebacks can contain tokens or user content. Record
    only the operation and exception class at this generic boundary.
    """
    logger.warning("best_effort_failure operation=%s error_type=%s", operation, type(error).__name__)
