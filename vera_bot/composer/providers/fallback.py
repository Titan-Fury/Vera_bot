"""FallbackLLMProvider — chain providers with auto-failover + circuit breaker.

Tries providers in order; on transient errors (HTTP, timeout, connection,
null content) moves to the next tier. Only raises if every tier fails.

Circuit breaker: when a tier returns 429 Too Many Requests with a
`Retry-After` header, that tier is marked cold for the returned window.
Subsequent calls skip the cold tier without a network round-trip.

Log noise is deduped: a given `(provider.name, error_class)` pair is logged
at most once every _DEDUPE_WINDOW_S seconds so a burst of 429s across a
tick's parallel composes collapses to one line instead of twenty.
"""

from __future__ import annotations

import logging
import time
from typing import Dict, List, Tuple

import requests

from ..metrics import METRICS
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
_DEFAULT_COLD_S = 30.0  # fallback cold window when Retry-After is missing
_MAX_COLD_S = 300.0     # hard cap; even a long Retry-After is bounded here


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

    @staticmethod
    def _extract_retry_after(exc: Exception) -> float | None:
        """If the exception is a 429 HTTPError, return the Retry-After seconds."""
        resp = getattr(exc, "response", None)
        if resp is None:
            return None
        status = getattr(resp, "status_code", None)
        if status != 429:
            return None
        ra = resp.headers.get("Retry-After") if getattr(resp, "headers", None) else None
        if not ra:
            return _DEFAULT_COLD_S
        try:
            return float(ra)
        except (TypeError, ValueError):
            return _DEFAULT_COLD_S

    def generate(self, system_prompt: str, user_prompt: str) -> str:
        last_exc: Exception | None = None
        tried_count = 0
        for i, p in enumerate(self.providers):
            if METRICS.is_tier_cold(p.name):
                # Skip without a network call. Count as a cascade step.
                if i > 0:
                    METRICS.record_fallback_to(p.name)
                continue
            tried_count += 1
            try:
                out = p.generate(system_prompt, user_prompt)
                if i > 0:
                    logger.info("fallback: recovered via %s (tier %d)", p.name, i + 1)
                    METRICS.record_fallback_to(p.name)
                return out
            except _TRANSIENT as e:
                self._log_failure(p.name, e)
                last_exc = e
                ra = self._extract_retry_after(e)
                if ra is not None:
                    cold = min(max(ra, 1.0), _MAX_COLD_S)
                    METRICS.record_tier_429(p.name, cold)
                    logger.info("fallback: tier %s cold for %.0fs (429)", p.name, cold)
                continue
        if last_exc is None:
            # Every tier was cold — synthesize a transient error so callers
            # treat it like any other upstream blip.
            raise RuntimeError(
                f"fallback: all {len(self.providers)} tiers cold (circuit breaker)"
            )
        raise last_exc
