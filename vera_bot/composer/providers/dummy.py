"""Dummy provider - returns hardcoded JSON. Used in CI and when no keys configured."""

from __future__ import annotations

import json

from .base import LLMProvider


class DummyLLMProvider(LLMProvider):
    name = "dummy"

    def generate(self, system_prompt: str, user_prompt: str) -> str:
        is_customer_facing = "CUSTOMER CONTEXT" in user_prompt
        is_reply = "[CHAT HISTORY]" in user_prompt
        send_as = "merchant_on_behalf" if is_customer_facing else "vera"

        if is_reply:
            return json.dumps(
                {
                    "action": "send",
                    "body": "Noted - sharing next step shortly.",
                    "cta": "open_ended",
                    "rationale": "[composer_v1] dummy reply",
                }
            )
        return json.dumps(
            {
                "body": "Dummy draft grounded on the provided context.",
                "cta": "open_ended",
                "send_as": send_as,
                "suppression_key": "dummy_key",
                "rationale": "[composer_v1] dummy composition",
            }
        )
