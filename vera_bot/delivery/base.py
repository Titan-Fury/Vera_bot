"""Abstract delivery contract."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Protocol


@dataclass
class DeliveryResult:
    """Normalized result returned by every provider.

    success=True means the BSP accepted the message for delivery (not that
    the recipient has read it - that requires webhook callbacks later).
    """
    success: bool
    provider: str
    message_id: Optional[str] = None
    recipient: Optional[str] = None
    error_code: Optional[str] = None
    error_message: Optional[str] = None
    raw_response: Optional[Dict[str, Any]] = None
    # Latency in milliseconds for the BSP call itself
    latency_ms: Optional[int] = None
    # Echo of the body that was sent (for the send log)
    body: Optional[str] = None


class DeliveryError(Exception):
    """Raised on terminal delivery failures (bad credentials, quota exceeded).

    Transient errors (HTTP 5xx, timeout) are returned as DeliveryResult with
    success=False so callers can retry without exception handling.
    """


class DeliveryProvider(Protocol):
    """All BSP adapters implement this minimal contract."""

    name: str

    def send_text(
        self,
        recipient: str,
        body: str,
        context: Optional[Dict[str, Any]] = None,
    ) -> DeliveryResult:
        """Send a free-text WhatsApp message inside the 24h session window.

        Args:
            recipient: E.164 phone number ('+919876543210') or BSP-specific
                identifier. Adapters normalize as needed.
            body: The message text (already passed through composer + critic).
            context: Optional metadata - trigger_id, merchant_id, rubric scores.
                Adapters may pass relevant subset to BSP for analytics.
        """
        ...

    def health(self) -> Dict[str, Any]:
        """Return provider liveness + quota info if available. Never raises."""
        ...
