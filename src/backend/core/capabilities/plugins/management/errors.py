"""Actionable management failures without SQL text or bound package contents."""
import re

from sqlalchemy.exc import SQLAlchemyError


def management_error(exc: Exception) -> str:
    if isinstance(exc, SQLAlchemyError):
        # SQLAlchemy's detail is commonly [], and str(exc) includes SQL parameters.
        # Driver SQLSTATE is safe to expose; driver messages may include row values.
        original = getattr(exc, "orig", None)
        state = getattr(original, "sqlstate", None) or getattr(original, "pgcode", None)
        state = state if isinstance(state, str) and re.fullmatch(r"[0-9A-Z]{5}", state) else None
        if state == "22001":
            return "Database field length limit exceeded (SQLSTATE 22001); check database migrations."
        suffix = f" (SQLSTATE {state})" if state else ""
        return f"Database operation failed{suffix}; contact the administrator."
    detail = getattr(exc, "detail", None)
    message = str(detail) if detail else str(exc)
    return message.strip() or type(exc).__name__
