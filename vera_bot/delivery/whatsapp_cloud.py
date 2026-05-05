"""Meta WhatsApp Cloud API delivery adapter.

This is the simplest path to a live WhatsApp send:
- Free up to 1,000 conversations/month (Meta's tier)
- No BSP deposit / no third-party reseller markup
- Self-serve setup at https://developers.facebook.com/

Setup (one-time, ~15 minutes):
1. Go to Meta for Developers -> Create App -> "Business" type.
2. Add the 'WhatsApp' product to your app.
3. In WhatsApp -> API Setup, Meta gives you:
   - A test phone number (until you add a real one)
   - A temporary access token (rotate after 24h with a permanent System User token)
   - A Phone Number ID
4. Add your personal/test phone number as a recipient ("Add phone number").
5. Verify it via the OTP that arrives on WhatsApp.
6. Set env:
   WHATSAPP_PROVIDER=cloud
   WHATSAPP_CLOUD_TOKEN=EAAG...    (the access token)
   WHATSAPP_CLOUD_PHONE_NUMBER_ID=1234567890   (NOT the number itself)
   WHATSAPP_CLOUD_API_VERSION=v20.0  (optional)

API reference: https://developers.facebook.com/docs/whatsapp/cloud-api/reference/messages

Important constraints we enforce upstream (in safety.py):
- Free-text messages are only deliverable inside a 24-hour session window.
  For first-contact (cold) sends Meta requires an APPROVED template. This
  adapter currently sends 'text' type only; template support is a TODO and
  documented in send_template().
- Per-recipient rate limiting and opt-out checks are owned by SafetyGuard.
"""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Any, Dict, Optional

import requests

from .base import DeliveryProvider, DeliveryResult

logger = logging.getLogger(__name__)


_PHONE_E164 = re.compile(r"^\+?[1-9]\d{6,14}$")


def _normalize_recipient(recipient: str) -> str:
    """Strip whitespace + leading '+'. Meta's API accepts either form for
    recipient but expects digits only in the JSON 'to' field for predictability.
    """
    cleaned = recipient.replace(" ", "").replace("-", "").strip()
    if cleaned.startswith("+"):
        cleaned = cleaned[1:]
    return cleaned


def _is_valid_phone(recipient: str) -> bool:
    return bool(_PHONE_E164.match(recipient.replace(" ", "").replace("-", "")))


class WhatsAppCloudProvider:
    """Meta WhatsApp Cloud API adapter."""

    name = "whatsapp_cloud"

    def __init__(
        self,
        token: str,
        phone_number_id: str,
        api_version: str = "v20.0",
        timeout_s: float = 8.0,
    ) -> None:
        if not token:
            raise ValueError("WhatsApp Cloud token is required")
        if not phone_number_id:
            raise ValueError("WhatsApp Cloud phone_number_id is required")
        self._token = token
        self._phone_number_id = phone_number_id
        self._api_version = api_version
        self._timeout_s = timeout_s
        self._base_url = (
            f"https://graph.facebook.com/{api_version}/{phone_number_id}/messages"
        )

    def _headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
        }

    # ---- Public API --------------------------------------------------------

    def send_text(
        self,
        recipient: str,
        body: str,
        context: Optional[Dict[str, Any]] = None,
    ) -> DeliveryResult:
        """Send a free-text message. Requires 24h session window with recipient.

        Returns DeliveryResult; never raises for HTTP errors. Network-level
        exceptions are caught and surfaced via success=False.
        """
        if not body or not body.strip():
            return DeliveryResult(
                success=False,
                provider=self.name,
                recipient=recipient,
                error_code="empty_body",
                error_message="body is empty - composer suppressed or compliance-blocked",
                body=body,
            )

        if not _is_valid_phone(recipient):
            return DeliveryResult(
                success=False,
                provider=self.name,
                recipient=recipient,
                error_code="invalid_phone",
                error_message=f"recipient '{recipient}' is not E.164",
                body=body,
            )

        normalized = _normalize_recipient(recipient)
        payload = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": normalized,
            "type": "text",
            "text": {
                "preview_url": False,
                "body": body[:4096],  # Cloud API hard limit
            },
        }

        t0 = time.perf_counter()
        try:
            r = requests.post(
                self._base_url,
                headers=self._headers(),
                json=payload,
                timeout=self._timeout_s,
            )
        except requests.exceptions.RequestException as e:
            return DeliveryResult(
                success=False,
                provider=self.name,
                recipient=normalized,
                error_code="network_error",
                error_message=f"{type(e).__name__}: {e}",
                latency_ms=int((time.perf_counter() - t0) * 1000),
                body=body,
            )

        latency_ms = int((time.perf_counter() - t0) * 1000)
        try:
            data = r.json() if r.content else {}
        except json.JSONDecodeError:
            data = {"raw_text": r.text[:1000]}

        if r.status_code == 200 and "messages" in data:
            msg_id = (data["messages"] or [{}])[0].get("id")
            return DeliveryResult(
                success=True,
                provider=self.name,
                recipient=normalized,
                message_id=msg_id,
                latency_ms=latency_ms,
                raw_response=data,
                body=body,
            )

        # Non-200 - extract Meta's error envelope
        err = data.get("error") or {}
        code = err.get("code")
        sub = err.get("error_subcode")
        msg = err.get("message", "unknown error")
        if sub:
            msg = f"{msg} (subcode {sub})"
        # Common pitfalls worth surfacing for tomorrow's test:
        # - 131030: "Recipient phone number not in allowed list" - need to add
        #   the test number under WhatsApp -> API Setup
        # - 131047: "Re-engagement message" - outside 24h session window;
        #   need to use template
        # - 131056: "Pair rate limit hit" - same recipient too fast
        return DeliveryResult(
            success=False,
            provider=self.name,
            recipient=normalized,
            error_code=str(code) if code is not None else f"http_{r.status_code}",
            error_message=str(msg),
            latency_ms=latency_ms,
            raw_response=data,
            body=body,
        )

    def send_template(
        self,
        recipient: str,
        template_name: str,
        language_code: str,
        components: Optional[list] = None,
    ) -> DeliveryResult:
        """Send a pre-approved template. Required outside the 24h session window.

        Templates must be approved in WhatsApp Manager before use. For a first
        cold send to your own number after the test number is added to allowed
        list, Meta provides 'hello_world' as a built-in template you can use.
        """
        normalized = _normalize_recipient(recipient)
        payload = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": normalized,
            "type": "template",
            "template": {
                "name": template_name,
                "language": {"code": language_code},
            },
        }
        if components:
            payload["template"]["components"] = components

        t0 = time.perf_counter()
        try:
            r = requests.post(
                self._base_url,
                headers=self._headers(),
                json=payload,
                timeout=self._timeout_s,
            )
        except requests.exceptions.RequestException as e:
            return DeliveryResult(
                success=False,
                provider=self.name,
                recipient=normalized,
                error_code="network_error",
                error_message=f"{type(e).__name__}: {e}",
                latency_ms=int((time.perf_counter() - t0) * 1000),
            )

        latency_ms = int((time.perf_counter() - t0) * 1000)
        try:
            data = r.json() if r.content else {}
        except json.JSONDecodeError:
            data = {"raw_text": r.text[:1000]}

        if r.status_code == 200 and "messages" in data:
            return DeliveryResult(
                success=True,
                provider=self.name,
                recipient=normalized,
                message_id=(data["messages"] or [{}])[0].get("id"),
                latency_ms=latency_ms,
                raw_response=data,
                body=f"[template:{template_name}]",
            )

        err = data.get("error") or {}
        return DeliveryResult(
            success=False,
            provider=self.name,
            recipient=normalized,
            error_code=str(err.get("code") or f"http_{r.status_code}"),
            error_message=str(err.get("message", "unknown error")),
            latency_ms=latency_ms,
            raw_response=data,
            body=f"[template:{template_name}]",
        )

    def health(self) -> Dict[str, Any]:
        """Lightweight health probe - hits Meta's GET on the phone number ID."""
        url = f"https://graph.facebook.com/{self._api_version}/{self._phone_number_id}"
        try:
            r = requests.get(url, headers=self._headers(), timeout=4.0)
            ok = r.status_code == 200
            data = r.json() if r.content else {}
            return {
                "provider": self.name,
                "ok": ok,
                "status_code": r.status_code,
                "display_phone": data.get("display_phone_number"),
                "verified_name": data.get("verified_name"),
            }
        except requests.exceptions.RequestException as e:
            return {
                "provider": self.name,
                "ok": False,
                "error": f"{type(e).__name__}: {e}",
            }
