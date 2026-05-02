"""In-memory operational metrics.

Powers `/v1/metrics` and `/dashboard`. Thread-safe, zero external deps.
Captures compose counts by provider + path, per-tier 429 counts,
p50/p95 latency, and a recent-composes ring buffer.
"""

from __future__ import annotations

import statistics
import threading
import time
from collections import Counter, deque
from typing import Any, Deque, Dict, List, Optional


class _Metrics:
    _RECENT_CAP = 20
    _LATENCY_CAP = 500

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._compose_by_provider: Counter[str] = Counter()
        self._compose_by_path: Counter[str] = Counter()
        self._compose_by_kind: Counter[str] = Counter()
        self._fallback_cascade: Counter[str] = Counter()
        self._tier_429: Counter[str] = Counter()
        self._latency_ms: Deque[float] = deque(maxlen=self._LATENCY_CAP)
        self._recent: Deque[Dict[str, Any]] = deque(maxlen=self._RECENT_CAP)
        self._tier_cold_until: Dict[str, float] = {}
        self._start = time.time()

    # ---- recording ------------------------------------------------------------

    def record_compose(
        self, *, kind: str, path: str, provider: Optional[str],
        latency_ms: float, body: str,
    ) -> None:
        with self._lock:
            self._compose_by_provider[provider or "none"] += 1
            self._compose_by_path[path] += 1
            self._compose_by_kind[kind] += 1
            self._latency_ms.append(float(latency_ms))
            self._recent.append({
                "ts": time.time(),
                "kind": kind,
                "path": path,
                "provider": provider,
                "latency_ms": int(latency_ms),
                "body_len": len(body or ""),
                "body_preview": (body or "")[:120],
            })

    def record_fallback_to(self, tier_name: str) -> None:
        with self._lock:
            self._fallback_cascade[tier_name] += 1

    def record_tier_429(self, tier_name: str, cold_seconds: float) -> None:
        with self._lock:
            self._tier_429[tier_name] += 1
            self._tier_cold_until[tier_name] = time.time() + cold_seconds

    def is_tier_cold(self, tier_name: str) -> bool:
        with self._lock:
            until = self._tier_cold_until.get(tier_name)
            if until is None:
                return False
            if time.time() >= until:
                self._tier_cold_until.pop(tier_name, None)
                return False
            return True

    # ---- snapshots ------------------------------------------------------------

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            lat = list(self._latency_ms)
            p50 = int(statistics.median(lat)) if lat else 0
            p95 = int(_percentile(lat, 95)) if lat else 0
            total_compose = sum(self._compose_by_path.values())
            template_count = self._compose_by_path.get("template", 0)
            template_ratio = (template_count / total_compose) if total_compose else 0.0
            now = time.time()
            cold = {k: max(0, int(v - now)) for k, v in self._tier_cold_until.items() if v > now}
            return {
                "uptime_seconds": int(time.time() - self._start),
                "compose_total": total_compose,
                "compose_by_provider": dict(self._compose_by_provider),
                "compose_by_path": dict(self._compose_by_path),
                "compose_by_kind": dict(self._compose_by_kind),
                "fallback_cascade": dict(self._fallback_cascade),
                "tier_429_counts": dict(self._tier_429),
                "tier_cold_remaining_s": cold,
                "latency_ms": {"p50": p50, "p95": p95, "samples": len(lat)},
                "template_ratio": round(template_ratio, 3),
                "recent": list(self._recent),
            }


def _percentile(vals: List[float], pct: int) -> float:
    if not vals:
        return 0.0
    s = sorted(vals)
    k = (len(s) - 1) * (pct / 100.0)
    lo, hi = int(k), min(int(k) + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


METRICS = _Metrics()
