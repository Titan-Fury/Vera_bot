"""Shared base for providers that speak the OpenAI Chat Completions protocol."""

from __future__ import annotations

import logging
from typing import Dict, Optional

import requests

from .base import LLMProvider
from ._retry import with_retries

logger = logging.getLogger(__name__)


class OpenAICompatibleProvider(LLMProvider):
    name = "openai_compat"

    def __init__(
        self,
        api_key: str,
        url: str,
        model: str,
        extra_headers: Optional[Dict[str, str]] = None,
        json_mode: bool = True,
        timeout_s: float = 10.0,
        max_tokens: int = 300,
    ) -> None:
        self.api_key = api_key
        self.url = url
        self.model = model
        self.extra_headers = extra_headers or {}
        self.json_mode = json_mode
        self.timeout_s = timeout_s
        self.max_tokens = max_tokens
        # Reuse one Session per provider so urllib3 can pool TLS connections
        # across concurrent composes.
        self._session = requests.Session()

    @with_retries()
    def generate(self, system_prompt: str, user_prompt: str) -> str:
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }
        headers.update(self.extra_headers)

        payload: Dict = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0.0,
            "max_tokens": self.max_tokens,
        }
        if self.json_mode:
            payload["response_format"] = {"type": "json_object"}

        resp = self._session.post(self.url, headers=headers, json=payload, timeout=self.timeout_s)
        resp.raise_for_status()
        data = resp.json()
        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError) as e:
            logger.error("Unexpected %s response: %s", self.__class__.__name__, data)
            raise RuntimeError(f"Malformed response from {self.name}") from e
        if content is None:
            logger.error("Null content from %s; full response=%s", self.name, data)
            raise RuntimeError(f"Null content from {self.name} (likely unsupported response_format)")
        return content
