"""Safety guards: opt-out tracking, per-recipient rate limit, daily cap.

These run BEFORE the delivery provider sees the message. They protect:
- The recipient (no spam, opt-out honored)
- Your WhatsApp Business number (Meta bans accounts at >0.3% complaint rate)
- Your wallet (a runaway loop cannot blast 10,000 messages overnight)

Persistence is plain JSONL files under DATA_DIR. This is intentionally
file-based so a personal/demo deployment has zero infra dependencies. Multi-
tenant production should swap these for Postgres-backed implementations.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Deque, Dict, Iterable, Optional, Tuple

logger = logging.getLogger(__name__)


# ---- Opt-out keywords (additive to validators.detect_hard_no) ----------------
# We accept any of these as "stop messaging me forever". detect_hard_no in
# validators.py is used INSIDE a conversation; this list is checked at the
# recipient level (do they appear in the opt-out store at all).
_OPT_OUT_KEYWORDS = (
    "stop", "stop all", "unsubscribe", "remove me",
    "do not contact", "leave me alone", "not interested",
    "बंद करो", "मत भेज", "परेशान",
)


def _normalize_recipient(recipient: str) -> str:
    """Strip whitespace and leading +; lowercase. Keeps storage canonical."""
    return recipient.replace(" ", "").replace("-", "").lstrip("+").lower()


# ============================================================================
# OptOutStore - JSONL-backed persistence
# ============================================================================

class OptOutStore:
    """Append-only file recording every opt-out event.

    Format: one JSON object per line:
        {"recipient": "...", "ts": "...", "reason": "..."}

    Recipient lookup is O(N) on first call but cached in memory after load.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.RLock()
        self._opted_out: set = set()
        self._loaded = False

    def _load(self) -> None:
        if self._loaded:
            return
        with self._lock:
            if self._loaded:
                return
            self._opted_out = set()
            if self._path.exists():
                try:
                    with self._path.open("r", encoding="utf-8") as f:
                        for line in f:
                            line = line.strip()
                            if not line:
                                continue
                            try:
                                data = json.loads(line)
                            except json.JSONDecodeError:
                                continue
                            r = data.get("recipient")
                            if r:
                                self._opted_out.add(_normalize_recipient(str(r)))
                except OSError as e:
                    logger.warning("OptOutStore load failed: %s", e)
            self._loaded = True

    def is_opted_out(self, recipient: str) -> bool:
        self._load()
        return _normalize_recipient(recipient) in self._opted_out

    def add(self, recipient: str, reason: str = "user_request") -> None:
        self._load()
        norm = _normalize_recipient(recipient)
        with self._lock:
            if norm in self._opted_out:
                return  # idempotent
            self._opted_out.add(norm)
            try:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                with self._path.open("a", encoding="utf-8") as f:
                    f.write(json.dumps({
                        "recipient": norm,
                        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                        "reason": reason,
                    }) + "\n")
            except OSError as e:
                logger.warning("OptOutStore write failed: %s", e)

    @staticmethod
    def detect_in_message(text: str) -> bool:
        """Recipient said something that looks like an opt-out request."""
        if not text:
            return False
        low = text.strip().lower()
        for kw in _OPT_OUT_KEYWORDS:
            if kw in low:
                return True
        return False


# ============================================================================
# RateLimiter - in-memory per-recipient sliding window + daily cap
# ============================================================================

@dataclass
class RateLimitVerdict:
    allowed: bool
    reason: str = ""
    retry_after_s: int = 0


class RateLimiter:
    """Two-level guard:

    1. Per-recipient: at most one message per `min_gap_seconds_per_recipient`.
       Default 60 minutes. Prevents spamming a single contact.
    2. Daily cap: at most `daily_cap` total sends across all recipients in
       a rolling 24-hour window. Default 50. Prevents runaway loops.

    Stored in memory only (process-local). For multi-process deployment,
    swap with a Redis-backed implementation later.
    """

    def __init__(
        self,
        min_gap_seconds_per_recipient: int = 3600,
        daily_cap: int = 50,
    ) -> None:
        self._min_gap = max(0, int(min_gap_seconds_per_recipient))
        self._daily_cap = max(1, int(daily_cap))
        self._lock = threading.RLock()
        self._last_send_at: Dict[str, float] = {}
        self._send_log: Deque[float] = deque()  # epoch timestamps within last 24h

    def _prune_old(self, now: float) -> None:
        cutoff = now - 86400.0
        while self._send_log and self._send_log[0] < cutoff:
            self._send_log.popleft()

    def check(self, recipient: str, now: Optional[float] = None) -> RateLimitVerdict:
        norm = _normalize_recipient(recipient)
        t = now if now is not None else time.time()
        with self._lock:
            self._prune_old(t)
            if len(self._send_log) >= self._daily_cap:
                # Oldest send still inside 24h - that is when next slot opens.
                oldest = self._send_log[0]
                return RateLimitVerdict(
                    allowed=False,
                    reason=f"daily_cap_reached ({self._daily_cap}/24h)",
                    retry_after_s=int(max(0, oldest + 86400 - t)),
                )
            last = self._last_send_at.get(norm)
            if last is not None:
                elapsed = t - last
                if elapsed < self._min_gap:
                    return RateLimitVerdict(
                        allowed=False,
                        reason=f"per_recipient_rate ({int(elapsed)}s ago, min {self._min_gap}s)",
                        retry_after_s=int(self._min_gap - elapsed),
                    )
            return RateLimitVerdict(allowed=True)

    def record(self, recipient: str, now: Optional[float] = None) -> None:
        """Call AFTER a successful send."""
        norm = _normalize_recipient(recipient)
        t = now if now is not None else time.time()
        with self._lock:
            self._last_send_at[norm] = t
            self._send_log.append(t)

    def stats(self) -> Dict[str, int]:
        with self._lock:
            self._prune_old(time.time())
            return {
                "daily_cap": self._daily_cap,
                "sent_in_last_24h": len(self._send_log),
                "tracked_recipients": len(self._last_send_at),
                "min_gap_seconds": self._min_gap,
            }


# ============================================================================
# SafetyGuard - composes opt-out + rate limit checks
# ============================================================================

@dataclass
class SafetyVerdict:
    allowed: bool
    reason: str = ""
    blocked_by: str = ""  # 'opt_out' | 'rate_limit' | ''
    retry_after_s: int = 0


class SafetyGuard:
    """One-stop guard. Call before every send."""

    def __init__(self, opt_out: OptOutStore, rate_limit: RateLimiter) -> None:
        self.opt_out = opt_out
        self.rate_limit = rate_limit

    def check(self, recipient: str) -> SafetyVerdict:
        if self.opt_out.is_opted_out(recipient):
            return SafetyVerdict(
                allowed=False, reason="recipient opted out", blocked_by="opt_out"
            )
        verdict = self.rate_limit.check(recipient)
        if not verdict.allowed:
            return SafetyVerdict(
                allowed=False,
                reason=verdict.reason,
                blocked_by="rate_limit",
                retry_after_s=verdict.retry_after_s,
            )
        return SafetyVerdict(allowed=True)

    def record_send(self, recipient: str) -> None:
        self.rate_limit.record(recipient)

    def opt_out_recipient(self, recipient: str, reason: str = "user_request") -> None:
        self.opt_out.add(recipient, reason)

    def stats(self) -> Dict[str, object]:
        return {
            "rate_limit": self.rate_limit.stats(),
            "opt_outs": len(self.opt_out._opted_out) if self.opt_out._loaded else "(unloaded)",
        }
