"""Append-only JSONL audit log for every delivery attempt."""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any, Dict, Optional

from .base import DeliveryResult

logger = logging.getLogger(__name__)


def _data_dir() -> Path:
    p = Path(os.environ.get("DATA_DIR") or "./data").resolve()
    p.mkdir(parents=True, exist_ok=True)
    return p


def _default_log_path() -> Path:
    custom = os.environ.get("DELIVERY_LOG_FILE")
    if custom:
        return Path(custom).resolve()
    return _data_dir() / "send_log.jsonl"


class SendLog:
    """Threadsafe JSONL writer; one record per delivery attempt.

    Record shape:
        {
          "ts": "...",
          "trigger_id": "...",
          "trigger_kind": "...",
          "merchant_id": "...",
          "recipient": "...",
          "provider": "whatsapp_cloud" | "console",
          "delivery_status": "sent" | "blocked" | "failed",
          "blocked_by": "opt_out" | "rate_limit" | null,
          "message_id": "...",
          "error_code": "...",
          "error_message": "...",
          "latency_ms": 0,
          "body_len": 0,
          "rubric": {"weighted": 8.3, "min_axis": 7, "compliance": 9},
          "flags": ["multi_draft_picked=1of3", ...]
        }
    """

    def __init__(self, path: Optional[Path] = None) -> None:
        self._path = path or _default_log_path()
        self._lock = threading.RLock()
        self._path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, record: Dict[str, Any]) -> None:
        record.setdefault("ts", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
        try:
            with self._lock, self._path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        except OSError as e:
            logger.warning("send-log write failed: %s", e)

    @staticmethod
    def from_result(
        result: DeliveryResult,
        *,
        trigger_id: Optional[str] = None,
        trigger_kind: Optional[str] = None,
        merchant_id: Optional[str] = None,
        rubric: Optional[Dict[str, Any]] = None,
        flags: Optional[list] = None,
        delivery_status: str = "sent",
        blocked_by: Optional[str] = None,
    ) -> Dict[str, Any]:
        rec = {
            "trigger_id": trigger_id,
            "trigger_kind": trigger_kind,
            "merchant_id": merchant_id,
            "recipient": result.recipient,
            "provider": result.provider,
            "delivery_status": delivery_status,
            "blocked_by": blocked_by,
            "message_id": result.message_id,
            "error_code": result.error_code,
            "error_message": result.error_message,
            "latency_ms": result.latency_ms,
            "body_len": len(result.body or ""),
            "rubric": rubric or {},
            "flags": flags or [],
        }
        return rec


_default_log: Optional[SendLog] = None


def get_send_log() -> SendLog:
    global _default_log
    if _default_log is None:
        _default_log = SendLog()
    return _default_log
