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
class AuthenticationError(AppException):
    """401 Authentication Required."""

    def __init__(self, message: str = "Authentication required"):
        super().__init__(
            code=30001,
            message=message,
            status_code=401
        )


class InvalidTokenError(AppException):
    """401 Invalid Token."""

    def __init__(self):
        super().__init__(
            code=30002,
            message="Invalid or expired token",
            status_code=401
        )


class TokenExpiredError(AppException):
    """401 Token Expired."""

    def __init__(self, expired_at: str):
        super().__init__(
            code=30003,
            message="Authentication token expired",
            status_code=401,
            data={"expired_at": expired_at, "hint": "Please login again to get a new token"}
        )


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


class InsufficientPermissionsError(AppException):
    """403 Insufficient Permissions."""

    def __init__(self, required_permission: str):
        super().__init__(
            code=31002,
            message="Insufficient permissions",
            status_code=403,
            data={"required_permission": required_permission}
        )


class ResourceOwnershipError(AppException):
    """403 Resource Ownership Required."""

    def __init__(self, resource_type: str, resource_id: str):
        super().__init__(
            code=31003,
            message="Access denied",
            status_code=403,
            data={
                "reason": "Only the resource owner can perform this operation",
                "resource_type": resource_type,
                "resource_id": resource_id
            }
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


class EndpointNotFoundError(AppException):
    """404 Endpoint Not Found."""

    def __init__(self, path: str):
        super().__init__(
            code=40002,
            message="Endpoint not found",
            status_code=404,
            data={"path": path}
        )


class ResourceAlreadyExistsError(AppException):
    """409 Resource Already Exists."""

    def __init__(self, resource_type: str, identifier: str):
        super().__init__(
            code=41001,
            message="Resource already exists",
            status_code=409,
            data={"resource_type": resource_type, "identifier": identifier}
        )


class ConcurrentModificationError(AppException):
    """409 Concurrent Modification."""

    def __init__(self, resource_type: str, resource_id: str, expected_version: int, actual_version: int):
        super().__init__(
            code=41002,
            message="Concurrent modification detected",
            status_code=409,
            data={
                "resource_type": resource_type,
                "resource_id": resource_id,
                "expected_version": expected_version,
                "actual_version": actual_version,
                "hint": "Please refresh and try again"
            }
        )


class RateLimitExceededError(AppException):
    """429 Rate Limit Exceeded."""

    def __init__(self, limit: str, retry_after: int, reset_at: str):
        super().__init__(
            code=42001,
            message="Rate limit exceeded",
            status_code=429,
            data={"limit": limit, "retry_after": retry_after, "reset_at": reset_at}
        )


# 5xxxx - Server Errors
class InternalServerError(AppException):
    """500 Internal Server Error."""

    def __init__(self, message: str = "Internal server error", error_type: str = None):
        data = {}
        if error_type:
            data["error_type"] = error_type
        data["hint"] = "Please try again later or contact support"

        super().__init__(
            code=50001,
            message=message,
            status_code=500,
            data=data
        )


class DatabaseError(AppException):
    """500 Database Error."""

    def __init__(self, message: str = "Database error"):
        super().__init__(
            code=50002,
            message=message,
            status_code=500,
            data={
                "error_type": "DatabaseError",
                "hint": "Please try again later or contact support"
            }
        )


class StorageError(AppException):
    """500 Storage Error."""

    def __init__(self, operation: str, error: str):
        super().__init__(
            code=51001,
            message=f"Storage {operation} failed",
            status_code=500,
            data={"error": error, "hint": "Please try again"}
        )


class UserCenterError(AppException):
    """502 User Center Error."""

    def __init__(self, error: str):
        super().__init__(
            code=52001,
            message="User center error",
            status_code=502,
            data={"error": error}
        )


class ModelAPIError(AppException):
    """502 Model API Error."""

    def __init__(self, model: str, provider: str, error: str):
        super().__init__(
            code=52101,
            message="Model API error",
            status_code=502,
            data={
                "model": model,
                "provider": provider,
                "error": error,
                "hint": "Model service encountered an error, please try a different model"
            }
        )


class ModelAPIRateLimitedError(AppException):
    """400 Model API Rate Limited."""

    def __init__(self, model: str):
        super().__init__(
            code=52103,
            message="Model API rate limited",
            status_code=400,
            data={"model": model, "hint": "Model quota exceeded, please try a different model"}
        )


class RequestTimeoutError(AppException):
    """504 Request Timeout."""

    def __init__(self, service: str, timeout: str):
        super().__init__(
            code=53001,
            message="Request timeout",
            status_code=504,
            data={"service": service, "timeout": timeout}
        )


class ModelAPITimeoutError(AppException):
    """504 Model API Timeout."""

    def __init__(self, model: str, timeout: str):
        super().__init__(
            code=53003,
            message="Model API timeout",
            status_code=504,
            data={
                "model": model,
                "timeout": timeout,
                "hint": "The model took too long to respond, please try again"
            }
        )


class ServiceUnavailableError(AppException):
    """503 Service Unavailable."""

    def __init__(self, message: str = "Service unavailable"):
        super().__init__(
            code=54001,
            message=message,
            status_code=503
        )
