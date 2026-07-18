"""Global exception handlers for the FastAPI application."""

from fastapi import FastAPI, Request

from core.infra.exceptions import AppException
from core.infra.responses import error_response


def setup_error_handlers(app: FastAPI) -> None:
    """Register global exception handlers on *app*."""

    @app.exception_handler(AppException)
    async def app_exception_handler(request: Request, exc: AppException):
        """Handle custom application exceptions."""
        return error_response(
            code=exc.code,
            message=exc.message,
            data=exc.data,
            status_code=exc.status_code,
        )
