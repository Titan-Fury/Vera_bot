"""Console / dry-run delivery provider.

Prints messages instead of sending them. Useful for:
- CI / unit tests
- Local dev when you do not want to burn quota
- WHATSAPP_DRY_RUN=1 emergency override
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, Optional

from .base import DeliveryProvider, DeliveryResult

logger = logging.getLogger(__name__)


class ConsoleProvider:
    name = "console"

    def send_text(
        self,
        recipient: str,
        body: str,
        context: Optional[Dict[str, Any]] = None,
    ) -> DeliveryResult:
        ctx_str = ""
        if context:
            keys = ("trigger_id", "trigger_kind", "merchant_id", "weighted_score", "min_axis")
            tags = [f"{k}={context[k]}" for k in keys if k in context]
            if tags:
                ctx_str = "  [" + ", ".join(tags) + "]"
        msg_id = f"console-{int(time.time()*1000)}"
        print(
            f"\n--- [console] would send to {recipient}{ctx_str} ---\n"
            f"{body}\n"
            f"--- ({len(body)} chars, msg_id={msg_id}) ---\n",
            flush=True,
        )
        return DeliveryResult(
            success=True,
            provider=self.name,
            recipient=recipient,
            message_id=msg_id,
            body=body,
        )

    def send_template(
        self,
        recipient: str,
        template_name: str,
        language_code: str,
        components: Optional[list] = None,
    ) -> DeliveryResult:
        msg_id = f"console-tpl-{int(time.time()*1000)}"
        print(
            f"\n--- [console] would send TEMPLATE '{template_name}' "
            f"({language_code}) to {recipient} ---\n",
            flush=True,
        )
        return DeliveryResult(
            success=True,
            provider=self.name,
            recipient=recipient,
            message_id=msg_id,
            body=f"[template:{template_name}]",
        )

    def health(self) -> Dict[str, Any]:
        return {"provider": self.name, "ok": True}
