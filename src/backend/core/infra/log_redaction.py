"""Final sink redaction for stdlib, structured logs and rendered tracebacks.

Transport libraries format URLs and exceptions outside structlog's processors.
Redact after the existing formatter so enabling diagnostic logs cannot expose
URL credentials. Keep hosts, paths, exception types and traceback locations.
"""
import logging
import re
from urllib.parse import urlsplit, urlunsplit

_URL = re.compile(r"https?://[^\s<>\"'\\]+", re.IGNORECASE)
_AUTH = re.compile(r"\b(Bearer|Basic)\s+[^\s\"'<>,;\\]+", re.IGNORECASE)


def _redact_url(match: re.Match) -> str:
    value = match.group(0)
    try:
        url = urlsplit(value)
        return urlunsplit((url.scheme, url.netloc.rsplit("@", 1)[-1], url.path,
                           "[redacted]" if url.query else "",
                           "[redacted]" if url.fragment else ""))
    except ValueError:
        # Malformed client URLs are especially likely to occur in error logs.
        return "[redacted-url]"


class RedactingFormatter(logging.Formatter):
    """Wrap, rather than replace, the deployment's chosen output format."""

    def __init__(self, formatter: logging.Formatter):
        super().__init__()
        self.formatter = formatter

    def format(self, record: logging.LogRecord) -> str:
        rendered = self.formatter.format(record)
        rendered = _URL.sub(_redact_url, rendered)
        rendered = _AUTH.sub(lambda match: match.group(1) + " [redacted]", rendered)
        return rendered


def protect_log_sinks(handlers: list[logging.Handler]) -> None:
    for handler in handlers:
        formatter = handler.formatter or logging.Formatter()
        if not isinstance(formatter, RedactingFormatter):
            handler.setFormatter(RedactingFormatter(formatter))
    # Keep existing warning/error diagnostics while suppressing transport noise.
    # SDK 2 uses the suffixed libraries; the old names are still used elsewhere.
    for name in ("uvicorn.access", "httpx", "httpcore", "httpx2", "httpcore2", "mcp.client"):
        logging.getLogger(name).setLevel(logging.WARNING)
