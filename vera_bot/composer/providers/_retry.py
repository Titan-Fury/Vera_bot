"""Retry decorator for transient provider failures."""

from __future__ import annotations

import logging
import time
from functools import wraps
from typing import Callable, TypeVar

import requests

logger = logging.getLogger(__name__)

T = TypeVar("T")

_RETRIABLE = (
    requests.exceptions.Timeout,
    requests.exceptions.ConnectionError,
    requests.exceptions.ChunkedEncodingError,
)


def with_retries(max_attempts: int = 2, backoffs: tuple[float, ...] = (0.5, 1.5)) -> Callable[[Callable[..., T]], Callable[..., T]]:
    def deco(fn: Callable[..., T]) -> Callable[..., T]:
        @wraps(fn)
        def wrapper(*args, **kwargs):
            last_exc: Exception | None = None
            for attempt in range(max_attempts + 1):
                try:
                    return fn(*args, **kwargs)
                except _RETRIABLE as e:
                    last_exc = e
                    if attempt >= max_attempts:
                        break
                    sleep = backoffs[min(attempt, len(backoffs) - 1)]
                    logger.warning("Provider transient error (attempt %d): %s; retrying in %.1fs", attempt + 1, e, sleep)
                    time.sleep(sleep)
                except requests.exceptions.HTTPError as e:
                    status = getattr(e.response, "status_code", None)
                    if status and 500 <= status < 600 and attempt < max_attempts:
                        sleep = backoffs[min(attempt, len(backoffs) - 1)]
                        logger.warning("Provider %s (attempt %d); retrying in %.1fs", status, attempt + 1, sleep)
                        time.sleep(sleep)
                        last_exc = e
                        continue
                    raise
            assert last_exc is not None
            raise last_exc

        return wrapper

    return deco
