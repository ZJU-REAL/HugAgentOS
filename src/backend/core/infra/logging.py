"""
Logging configuration for HugAgentOS.

This module provides structured logging with:
- JSON format for production environments
- Colored text format for development
- Context variables: trace_id, user_id, chat_id, latency, status_code
- Based on structlog for structured logging
"""

import os
import sys
import logging
import tempfile
import uuid
from pathlib import Path
from logging.handlers import RotatingFileHandler
from typing import Any, Dict
from contextvars import ContextVar

from core.config.settings import settings

try:
    import structlog
    from structlog.types import EventDict, Processor
    STRUCTLOG_AVAILABLE = True
except ModuleNotFoundError:
    structlog = None  # type: ignore
    EventDict = Dict[str, Any]  # type: ignore
    Processor = Any  # type: ignore
    STRUCTLOG_AVAILABLE = False

# Import data masking utilities
try:
    from core.infra.data_masking import mask_log_data
except ImportError:
    # Fallback if data_masking module is not available
    def mask_log_data(data):
        return data


# Context variables for logging
trace_id_var: ContextVar[str] = ContextVar("trace_id", default="")
user_id_var: ContextVar[str] = ContextVar("user_id", default="")
chat_id_var: ContextVar[str] = ContextVar("chat_id", default="")


def generate_trace_id() -> str:
    """Generate a unique trace ID."""
    return str(uuid.uuid4())


def add_trace_id(logger: Any, method_name: str, event_dict: EventDict) -> EventDict:
    """Add trace_id to log event if available."""
    trace_id = trace_id_var.get()
    if trace_id:
        event_dict["trace_id"] = trace_id
    return event_dict


def add_user_context(logger: Any, method_name: str, event_dict: EventDict) -> EventDict:
    """Add user_id and chat_id to log event if available."""
    user_id = user_id_var.get()
    chat_id = chat_id_var.get()

    if user_id:
        event_dict["user_id"] = user_id
    if chat_id:
        event_dict["chat_id"] = chat_id

    return event_dict


def add_service_info(logger: Any, method_name: str, event_dict: EventDict) -> EventDict:
    """Add service name to log event."""
    event_dict["service"] = "hugagent"
    return event_dict


def drop_color_message_key(logger: Any, method_name: str, event_dict: EventDict) -> EventDict:
    """
    Uvicorn logs use the "color_message" key, which we don't want in JSON output.
    This processor drops that key if it exists.
    """
    event_dict.pop("color_message", None)
    return event_dict


def mask_sensitive_fields(logger: Any, method_name: str, event_dict: EventDict) -> EventDict:
    """
    Mask sensitive fields in log events.

    This processor masks common sensitive fields like passwords, tokens, etc.
    """
    # Get the event message
    event = event_dict.get("event")
    if event and isinstance(event, str):
        event_dict["event"] = mask_log_data(event)

    # Mask sensitive keys in the event dict
    sensitive_keys = ["password", "token", "secret", "api_key", "access_key", "secret_key", "authorization"]
    for key in sensitive_keys:
        if key in event_dict:
            event_dict[key] = "***"

    # Recursively mask nested data structures
    for key, value in list(event_dict.items()):
        if isinstance(value, dict):
            event_dict[key] = mask_log_data(value)
        elif isinstance(value, str) and len(value) > 20:
            # Mask potential tokens in long strings
            event_dict[key] = mask_log_data(value)

    return event_dict


def get_log_level() -> int:
    """Get log level from environment variable."""
    level_name = settings.server.log_level
    return getattr(logging, level_name, logging.INFO)


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


def _resolve_writable_log_path(configured_path: str) -> Path:
    """Pick the first writable log path for the current runtime."""
    repo_root = Path(__file__).resolve().parents[4]
    candidates = [
        Path(configured_path),
        repo_root / "logs" / "backend.log",
        Path(tempfile.gettempdir()) / "hugagent" / "backend.log",
    ]
    for path in candidates:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            if os.access(path.parent, os.W_OK):
                return path
        except OSError:
            continue
    return candidates[-1]


def _build_log_handlers(log_level: int) -> list[logging.Handler]:
    """
    Build logging handlers.

    Always includes stdout handler.
    Optionally adds rotating file handler when LOG_TO_FILE is enabled.
    """
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]

    if not _env_bool("LOG_TO_FILE", False):
        return handlers

    log_file_path = settings.server.log_file_path
    max_bytes = settings.server.log_file_max_bytes
    if max_bytes <= 0:
        max_bytes = 10 * 1024 * 1024

    backup_count = settings.server.log_file_backup_count
    if backup_count < 0:
        backup_count = 5

    try:
        log_path = _resolve_writable_log_path(log_file_path)

        file_handler = RotatingFileHandler(
            filename=str(log_path),
            maxBytes=max_bytes,
            backupCount=backup_count,
            encoding="utf-8",
        )
        file_handler.setLevel(log_level)
        handlers.append(file_handler)
    except Exception as exc:
        # Keep service available even when file logging setup fails.
        print(f"[logging] failed to configure file handler: {exc}", file=sys.stderr)

    return handlers


def is_production() -> bool:
    """Check if running in production environment."""
    return settings.server.is_prod


def setup_logging() -> None:
    """
    Configure structlog for the application.

    Production: JSON output
    Development: Colored console output with key-value pairs
    """
    # Get log level
    log_level = get_log_level()
    handlers = _build_log_handlers(log_level)

    if not STRUCTLOG_AVAILABLE:
        logging.basicConfig(
            format="%(asctime)s %(levelname)s %(name)s %(message)s",
            handlers=handlers,
            level=log_level,
            force=True,
        )
        logging.getLogger(__name__).warning(
            "structlog_not_installed_fallback_to_stdlib_logging"
        )
        return

    # Shared processors
    shared_processors: list[Processor] = [
        structlog.contextvars.merge_contextvars,
        add_trace_id,
        add_user_context,
        add_service_info,
        mask_sensitive_fields,  # Mask sensitive data before output
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.stdlib.PositionalArgumentsFormatter(),
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
    ]

    if is_production():
        # Production: JSON output
        processors = shared_processors + [
            drop_color_message_key,
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer()
        ]
    else:
        # Development: colored console output
        processors = shared_processors + [
            structlog.processors.format_exc_info,
            structlog.dev.ConsoleRenderer(colors=True)
        ]

    # Configure structlog
    structlog.configure(
        processors=processors,
        wrapper_class=structlog.stdlib.BoundLogger,
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    # Configure standard logging
    logging.basicConfig(
        format="%(message)s",
        handlers=handlers,
        level=log_level,
        force=True,
    )

    # Set log levels for noisy libraries
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


class _FallbackLogger:
    """Small adapter to keep key/value logging call style without structlog."""

    def __init__(self, name: str | None = None):
        self._logger = logging.getLogger(name)

    def _log(self, level: int, event: str, **kwargs: Any) -> None:
        msg = mask_log_data(event)
        if kwargs:
            extra = " ".join([f"{k}={mask_log_data(v)}" for k, v in kwargs.items()])
            msg = f"{msg} {extra}"
        self._logger.log(level, msg)

    def debug(self, event: str, **kwargs: Any) -> None:
        self._log(logging.DEBUG, event, **kwargs)

    def info(self, event: str, **kwargs: Any) -> None:
        self._log(logging.INFO, event, **kwargs)

    def warning(self, event: str, **kwargs: Any) -> None:
        self._log(logging.WARNING, event, **kwargs)

    def error(self, event: str, **kwargs: Any) -> None:
        self._log(logging.ERROR, event, **kwargs)


def get_logger(name: str = None) -> Any:
    """
    Get a structured logger instance.

    Args:
        name: Logger name (typically __name__)

    Returns:
        A bound logger instance
    """
    if STRUCTLOG_AVAILABLE:
        return structlog.get_logger(name)
    return _FallbackLogger(name)


# Context managers for setting context variables
class LogContext:
    """Context manager for setting logging context variables."""

    def __init__(self, trace_id: str = None, user_id: str = None, chat_id: str = None):
        self.trace_id = trace_id
        self.user_id = user_id
        self.chat_id = chat_id
        self._trace_id_token = None
        self._user_id_token = None
        self._chat_id_token = None

    def __enter__(self):
        if self.trace_id:
            self._trace_id_token = trace_id_var.set(self.trace_id)
        if self.user_id:
            self._user_id_token = user_id_var.set(self.user_id)
        if self.chat_id:
            self._chat_id_token = chat_id_var.set(self.chat_id)
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self._trace_id_token:
            trace_id_var.reset(self._trace_id_token)
        if self._user_id_token:
            user_id_var.reset(self._user_id_token)
        if self._chat_id_token:
            chat_id_var.reset(self._chat_id_token)


# Initialize logging on module import
setup_logging()
