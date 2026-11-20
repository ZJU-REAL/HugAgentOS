"""Custom exceptions for the application."""

from typing import Any, Dict, Optional


class AppException(Exception):
    """Base application exception."""

    def __init__(
        self,
        code: int,
        message: str,
        status_code: int = 500,
        data: Optional[Dict[str, Any]] = None
    ):
        self.code = code
        self.message = message
        self.status_code = status_code
        self.data = data or {}
        super().__init__(self.message)


class FirstResponseTimeoutError(TimeoutError):
    """A chat produced no user-visible response before its deadline."""

    def __init__(self):
        super().__init__("当前模型调用量大，算力资源紧张，请稍后再试！")


# 2xxxx - Client Request Errors
class BadRequestError(AppException):
    """400 Bad Request."""

    def __init__(self, message: str = "Invalid request parameters", data: Dict = None):
        super().__init__(
            code=20001,
            message=message,
            status_code=400,
            data=data
        )


class ValidationError(AppException):
    """400 Validation Error."""

    def __init__(self, errors: list, message: str = "Validation failed"):
        super().__init__(
            code=20001,
            message=message,
            status_code=400,
            data={"errors": errors}
        )


class FileTooLargeError(AppException):
    """400 File Too Large."""

    def __init__(self, max_size: int, actual_size: int):
        super().__init__(
            code=21001,
            message="File too large",
            status_code=400,
            data={"max_size": max_size, "actual_size": actual_size, "unit": "bytes"}
        )


class InvalidFileTypeError(AppException):
    """400 Invalid File Type."""

    def __init__(self, allowed_types: list, actual_type: str):
        super().__init__(
            code=21002,
            message="Invalid file type",
            status_code=400,
            data={"allowed_types": allowed_types, "actual_type": actual_type}
        )


# 3xxxx - Auth Errors
class AccessDeniedError(AppException):
    """403 Access Denied."""

    def __init__(self, message: str = "Access denied", reason: str = None):
        data = {}
        if reason:
            data["reason"] = reason

        super().__init__(
            code=31001,
            message=message,
            status_code=403,
            data=data
        )


# 4xxxx - Resource Errors
class ResourceNotFoundError(AppException):
    """404 Resource Not Found."""

    def __init__(self, resource_type: str, resource_id: str):
        super().__init__(
            code=40001,
            message="Resource not found",
            status_code=404,
            data={
                "resource_type": resource_type,
                "resource_id": resource_id,
                "hint": f"The {resource_type} may have been deleted"
            }
        )


# 5xxxx - Server Errors
class StorageError(AppException):
    """500 Storage Error."""

    def __init__(self, operation: str, error: str):
        super().__init__(
            code=51001,
            message=f"Storage {operation} failed",
            status_code=500,
            data={"error": error, "hint": "Please try again"}
        )


class ServiceUnavailableError(AppException):
    """503 Service Unavailable."""

    def __init__(self, message: str = "Service unavailable"):
        super().__init__(
            code=54001,
            message=message,
            status_code=503
        )
