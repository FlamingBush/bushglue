import logging
import sys
import time
from collections.abc import Callable
from typing import Any

from src.common.config import ErrorHandlingConfig

logger = logging.getLogger(__name__)


class NetworkError(Exception):
    """Connection failures, timeouts, HTTP 5xx responses."""


class ValidationError(Exception):
    """Malformed JSON, invalid LLM output content."""


class FatalDBError(Exception):
    """Unrecoverable database write failure."""


class RetriesExhausted(Exception):
    """All retries for a work item have been exhausted."""

    def __init__(self, error_class: str, attempts: int, last_error: Exception):
        self.error_class = error_class
        self.attempts = attempts
        self.last_error = last_error
        super().__init__(
            f"{error_class} retries exhausted after {attempts} attempts: {last_error}"
        )


class ErrorTracker:
    """Tracks errors by class with separate retry counters and running totals."""

    def __init__(self, config: ErrorHandlingConfig):
        self.config = config
        self.network_errors = 0
        self.validation_errors = 0

    def _log_totals(self) -> None:
        print(
            f"[errors] network: {self.network_errors}"
            f" | validation: {self.validation_errors}",
            flush=True,
        )

    def _backoff_delay(self, attempt: int) -> float:
        delay = self.config.retry_base_delay_seconds * (2**attempt)
        return min(delay, self.config.max_retry_delay_seconds)

    def retry_network(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        """Call fn, retrying on NetworkError with exponential backoff."""
        for attempt in range(self.config.max_network_retries):
            try:
                return fn(*args, **kwargs)
            except NetworkError as e:
                self.network_errors += 1
                logger.error(
                    "Network error (attempt %d/%d): %s",
                    attempt + 1,
                    self.config.max_network_retries,
                    e,
                )
                self._log_totals()
                if attempt + 1 >= self.config.max_network_retries:
                    raise RetriesExhausted("network", attempt + 1, e) from e
                delay = self._backoff_delay(attempt)
                logger.info("Retrying in %.1fs...", delay)
                time.sleep(delay)

    def retry_validation(
        self, fn: Callable[..., Any], *args: Any, **kwargs: Any
    ) -> Any:
        """Call fn, retrying on ValidationError immediately."""
        for attempt in range(self.config.max_validation_retries):
            try:
                return fn(*args, **kwargs)
            except ValidationError as e:
                self.validation_errors += 1
                logger.error(
                    "Validation error (attempt %d/%d): %s",
                    attempt + 1,
                    self.config.max_validation_retries,
                    e,
                )
                self._log_totals()
                if attempt + 1 >= self.config.max_validation_retries:
                    raise RetriesExhausted("validation", attempt + 1, e) from e

    def retry(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        """Call fn, retrying on both NetworkError and ValidationError.

        Each error class has its own counter. The call fails when either
        class exceeds its max retries.
        """
        network_attempts = 0
        validation_attempts = 0

        while True:
            try:
                return fn(*args, **kwargs)
            except NetworkError as e:
                network_attempts += 1
                self.network_errors += 1
                logger.error(
                    "Network error (attempt %d/%d): %s",
                    network_attempts,
                    self.config.max_network_retries,
                    e,
                )
                self._log_totals()
                if network_attempts >= self.config.max_network_retries:
                    raise RetriesExhausted("network", network_attempts, e) from e
                delay = self._backoff_delay(network_attempts - 1)
                logger.info("Retrying in %.1fs...", delay)
                time.sleep(delay)
            except ValidationError as e:
                validation_attempts += 1
                self.validation_errors += 1
                logger.error(
                    "Validation error (attempt %d/%d): %s",
                    validation_attempts,
                    self.config.max_validation_retries,
                    e,
                )
                self._log_totals()
                if validation_attempts >= self.config.max_validation_retries:
                    raise RetriesExhausted("validation", validation_attempts, e) from e


def handle_fatal_db_error(error: Exception) -> None:
    """Log a fatal DB error and exit."""
    logger.critical("Fatal database error: %s", error)
    sys.exit(1)
