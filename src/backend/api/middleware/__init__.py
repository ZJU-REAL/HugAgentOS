"""Middleware modules for the FastAPI application."""

from api.middleware.cors import setup_cors
from api.middleware.logging import setup_logging_middleware
from api.middleware.error_handler import setup_error_handlers

__all__ = ["setup_cors", "setup_logging_middleware", "setup_error_handlers"]
