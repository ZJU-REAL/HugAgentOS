"""
Rate limiting and circuit breaker for HugAgentOS.

This module provides:
- Rate limiting using slowapi
- Circuit breaker pattern for resilience
- Protection against overload and cascading failures
"""

import time
from typing import Callable, Optional, Any
from enum import Enum
from functools import wraps

from core.config.settings import settings

from slowapi import Limiter
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded

from core.infra.logging import get_logger


logger = get_logger(__name__)


# Rate limiter configuration
def get_rate_limit_enabled() -> bool:
    """Check if rate limiting is enabled."""
    return settings.rate_limit.enabled


def get_rate_limit_storage() -> str:
    """Get rate limit storage backend."""
    # Can be "memory" or "redis://host:port"
    return settings.rate_limit.storage


# Create limiter instance
limiter = Limiter(
    key_func=get_remote_address,
    storage_uri=get_rate_limit_storage(),
    enabled=get_rate_limit_enabled(),
    headers_enabled=True,  # Add rate limit headers to response
)


class CircuitBreakerState(Enum):
    """Circuit breaker states."""
    CLOSED = 0  # Normal operation
    OPEN = 1    # Failing, reject requests
    HALF_OPEN = 2  # Testing if service recovered


class CircuitBreakerOpenError(Exception):
    """Exception raised when circuit breaker is open."""
    pass


class CircuitBreaker:
    """
    Circuit breaker implementation for resilience.

    The circuit breaker monitors failures and can "open" to prevent
    cascading failures. It has three states:
    - CLOSED: Normal operation, requests pass through
    - OPEN: Too many failures, requests are rejected immediately
    - HALF_OPEN: Testing if service recovered, limited requests allowed

    Args:
        name: Circuit breaker name (for metrics)
        failure_threshold: Number of failures before opening (default: 5)
        success_threshold: Number of successes to close from half-open (default: 2)
        timeout: Seconds to wait before trying half-open (default: 60)
        expected_exception: Exception type that counts as failure (default: Exception)
    """

    def __init__(
        self,
        name: str,
        failure_threshold: int = 5,
        success_threshold: int = 2,
        timeout: int = 60,
        expected_exception: type = Exception
    ):
        self.name = name
        self.failure_threshold = failure_threshold
        self.success_threshold = success_threshold
        self.timeout = timeout
        self.expected_exception = expected_exception

        self.failure_count = 0
        self.success_count = 0
        self.state = CircuitBreakerState.CLOSED
        self.last_failure_time: Optional[float] = None

    def _should_attempt_reset(self) -> bool:
        """Check if we should attempt to reset from OPEN to HALF_OPEN."""
        if self.state != CircuitBreakerState.OPEN:
            return False

        if self.last_failure_time is None:
            return True

        return time.time() - self.last_failure_time >= self.timeout

    def _on_success(self):
        """Handle successful call."""
        if self.state == CircuitBreakerState.HALF_OPEN:
            self.success_count += 1
            if self.success_count >= self.success_threshold:
                # Recovered, close the circuit
                logger.info(
                    "circuit_breaker_closed",
                    breaker=self.name,
                    previous_state=self.state.name
                )
                self.state = CircuitBreakerState.CLOSED
                self.failure_count = 0
                self.success_count = 0
        elif self.state == CircuitBreakerState.CLOSED:
            # Reset failure count on success
            self.failure_count = 0

    def _on_failure(self):
        """Handle failed call."""
        self.failure_count += 1
        self.last_failure_time = time.time()

        if self.state == CircuitBreakerState.HALF_OPEN:
            # Failed during half-open, go back to open
            logger.warning(
                "circuit_breaker_reopened",
                breaker=self.name
            )
            self.state = CircuitBreakerState.OPEN
            self.success_count = 0

        elif self.state == CircuitBreakerState.CLOSED:
            if self.failure_count >= self.failure_threshold:
                # Too many failures, open the circuit
                logger.error(
                    "circuit_breaker_opened",
                    breaker=self.name,
                    failure_count=self.failure_count,
                    threshold=self.failure_threshold
                )
                self.state = CircuitBreakerState.OPEN

    def call(self, func: Callable, *args, **kwargs) -> Any:
        """
        Call a function through the circuit breaker.

        Args:
            func: Function to call
            *args: Positional arguments for the function
            **kwargs: Keyword arguments for the function

        Returns:
            Function result

        Raises:
            CircuitBreakerOpenError: If circuit is open
            Exception: Original exception from function
        """
        # Check if we should attempt reset
        if self._should_attempt_reset():
            logger.info(
                "circuit_breaker_half_open",
                breaker=self.name
            )
            self.state = CircuitBreakerState.HALF_OPEN
            self.success_count = 0

        # If circuit is open, fail fast
        if self.state == CircuitBreakerState.OPEN:
            raise CircuitBreakerOpenError(
                f"Circuit breaker '{self.name}' is OPEN. "
                f"Will retry in {self.timeout - (time.time() - self.last_failure_time):.0f}s"
            )

        # Attempt the call
        try:
            result = func(*args, **kwargs)
            self._on_success()
            return result
        except self.expected_exception as e:
            self._on_failure()
            raise

    async def call_async(self, func: Callable, *args, **kwargs) -> Any:
        """
        Call an async function through the circuit breaker.

        Args:
            func: Async function to call
            *args: Positional arguments for the function
            **kwargs: Keyword arguments for the function

        Returns:
            Function result

        Raises:
            CircuitBreakerOpenError: If circuit is open
            Exception: Original exception from function
        """
        # Check if we should attempt reset
        if self._should_attempt_reset():
            logger.info(
                "circuit_breaker_half_open",
                breaker=self.name
            )
            self.state = CircuitBreakerState.HALF_OPEN
            self.success_count = 0

        # If circuit is open, fail fast
        if self.state == CircuitBreakerState.OPEN:
            raise CircuitBreakerOpenError(
                f"Circuit breaker '{self.name}' is OPEN. "
                f"Will retry in {self.timeout - (time.time() - self.last_failure_time):.0f}s"
            )

        # Attempt the call
        try:
            result = await func(*args, **kwargs)
            self._on_success()
            return result
        except self.expected_exception as e:
            self._on_failure()
            raise

    def __call__(self, func: Callable) -> Callable:
        """
        Use circuit breaker as a decorator.

        Example:
            breaker = CircuitBreaker("my_service")

            @breaker
            def call_service():
                ...
        """
        @wraps(func)
        async def async_wrapper(*args, **kwargs):
            return await self.call_async(func, *args, **kwargs)

        @wraps(func)
        def sync_wrapper(*args, **kwargs):
            return self.call(func, *args, **kwargs)

        if hasattr(func, '__call__') and hasattr(func.__call__, '__await__'):
            return async_wrapper
        else:
            return sync_wrapper


# Global circuit breakers for common services
user_center_breaker = CircuitBreaker(
    name="user_center",
    failure_threshold=settings.rate_limit.cb_user_center_threshold,
    timeout=settings.rate_limit.cb_user_center_timeout,
)

model_api_breaker = CircuitBreaker(
    name="model_api",
    failure_threshold=settings.rate_limit.cb_model_api_threshold,
    timeout=settings.rate_limit.cb_model_api_timeout,
)

storage_breaker = CircuitBreaker(
    name="storage",
    failure_threshold=settings.rate_limit.cb_storage_threshold,
    timeout=settings.rate_limit.cb_storage_timeout,
)


def snapshot_circuit_breakers() -> list[dict]:
    """Read-only snapshot: current state of the three global circuit breakers (for the security admin console).

    last_failure_time is converted to "seconds since last failure"; None means it has never failed.
    """
    now = time.time()
    out: list[dict] = []
    for b in (user_center_breaker, model_api_breaker, storage_breaker):
        seconds_since_failure = (
            None if b.last_failure_time is None else max(0.0, now - b.last_failure_time)
        )
        out.append({
            "name": b.name,
            "state": b.state.name,  # CLOSED / OPEN / HALF_OPEN
            "failure_count": b.failure_count,
            "failure_threshold": b.failure_threshold,
            "seconds_since_failure": seconds_since_failure,
            "timeout": b.timeout,
        })
    return out


def get_circuit_breaker(service: str) -> CircuitBreaker:
    """
    Get a circuit breaker instance for a service.

    Args:
        service: Service name (user_center, model_api, storage)

    Returns:
        CircuitBreaker instance
    """
    breakers = {
        "user_center": user_center_breaker,
        "model_api": model_api_breaker,
        "storage": storage_breaker,
    }

    if service not in breakers:
        # Create a new breaker for unknown service
        breakers[service] = CircuitBreaker(name=service)

    return breakers[service]
