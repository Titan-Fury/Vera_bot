"""FallbackLLMProvider — chain providers with auto-failover.

Tries providers in order; on transient errors (HTTP, timeout, connection,
null content) moves to the next tier. Only raises if every tier fails.

Log noise is deduped: a given `(provider.name, error_class)` pair is logged
at most once every _DEDUPE_WINDOW_S seconds so a burst of 429s across a
tick's parallel composes collapses to one line instead of twenty.
"""

from __future__ import annotations

import logging
import time
from typing import Dict, List, Tuple

import requests

from .base import LLMProvider

logger = logging.getLogger(__name__)


_TRANSIENT = (
    requests.exceptions.Timeout,
    requests.exceptions.ConnectionError,
    requests.exceptions.ChunkedEncodingError,
    requests.exceptions.HTTPError,
    RuntimeError,  # base raises these for null / malformed responses
)

_DEDUPE_WINDOW_S = 5.0


class FallbackLLMProvider(LLMProvider):
    def __init__(self, providers: List[LLMProvider]) -> None:
        if not providers:
            raise ValueError("FallbackLLMProvider requires at least one provider")
        self.providers = providers
        self.name = "fallback[" + "->".join(p.name for p in providers) + "]"
        self._last_log: Dict[Tuple[str, str], float] = {}

    def _log_failure(self, provider_name: str, exc: Exception) -> None:
        key = (provider_name, type(exc).__name__)
        now = time.monotonic()
        last = self._last_log.get(key, 0.0)
        if now - last < _DEDUPE_WINDOW_S:
            return
        self._last_log[key] = now
        logger.warning(
            "fallback: %s failed (%s: %s); trying next tier",
            provider_name, type(exc).__name__, str(exc)[:200],
        )

    def generate(self, system_prompt: str, user_prompt: str) -> str:
        last_exc: Exception | None = None
        for i, p in enumerate(self.providers):
            try:
                out = p.generate(system_prompt, user_prompt)
                if i > 0:
                    logger.info("fallback: recovered via %s (tier %d)", p.name, i + 1)
                return out
            except _TRANSIENT as e:
                self._log_failure(p.name, e)
                last_exc = e
                continue
        assert last_exc is not None
        raise last_exc
