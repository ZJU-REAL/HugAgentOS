"""Circuit state and invocation accounting, independent of HTTP rate limiting."""

import inspect
import time
from enum import Enum
from functools import wraps
from typing import Any, Callable, Iterable, Optional

from core.infra.logging import get_logger

logger = get_logger(__name__)


class CircuitBreakerState(Enum):
    """Circuit breaker states."""

    CLOSED = 0  # Normal operation
    OPEN = 1  # Failing, reject requests
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
        expected_exception: type = Exception,
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
                    "circuit_breaker_closed", breaker=self.name, previous_state=self.state.name
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
            logger.warning("circuit_breaker_reopened", breaker=self.name)
            self.state = CircuitBreakerState.OPEN
            self.success_count = 0

        elif self.state == CircuitBreakerState.CLOSED:
            if self.failure_count >= self.failure_threshold:
                # Too many failures, open the circuit
                logger.error(
                    "circuit_breaker_opened",
                    breaker=self.name,
                    failure_count=self.failure_count,
                    threshold=self.failure_threshold,
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
            logger.info("circuit_breaker_half_open", breaker=self.name)
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
        except self.expected_exception:
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
            logger.info("circuit_breaker_half_open", breaker=self.name)
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
        except self.expected_exception:
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

        if inspect.iscoroutinefunction(func) or inspect.iscoroutinefunction(
            getattr(func, "__call__", None)
        ):
            return async_wrapper
        else:
            return sync_wrapper


def snapshot_circuit_breakers(breakers: Iterable[CircuitBreaker]) -> list[dict]:
    """Read-only snapshot: current state of supplied live circuit breakers (for the security admin console).

    last_failure_time is converted to "seconds since last failure"; None means it has never failed.
    """
    now = time.time()
    out: list[dict] = []
    for b in breakers:
        seconds_since_failure = (
            None if b.last_failure_time is None else max(0.0, now - b.last_failure_time)
        )
        out.append(
            {
                "name": b.name,
                "state": b.state.name,  # CLOSED / OPEN / HALF_OPEN
                "failure_count": b.failure_count,
                "failure_threshold": b.failure_threshold,
                "seconds_since_failure": seconds_since_failure,
                "timeout": b.timeout,
            }
        )
    return out
