"""Google Gemini provider (REST v1beta)."""

from __future__ import annotations

import logging

import requests

from .base import LLMProvider
from ._retry import with_retries

logger = logging.getLogger(__name__)


class GeminiLLMProvider(LLMProvider):
    name = "gemini"

    def __init__(self, api_key: str, model: str = "gemini-1.5-flash") -> None:
        self.api_key = api_key
        self.model = model
        self.url = (
            f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
            f"?key={api_key}"
        )
        # Reuse one Session per provider for HTTP connection pooling.
        self._session = requests.Session()

    @with_retries()
    def generate(self, system_prompt: str, user_prompt: str) -> str:
        payload = {
            "systemInstruction": {"parts": [{"text": system_prompt}]},
            "contents": [{"role": "user", "parts": [{"text": user_prompt}]}],
            "generationConfig": {"temperature": 0.0, "responseMimeType": "application/json"},
        }
        resp = self._session.post(self.url, headers={"Content-Type": "application/json"}, json=payload, timeout=8)
        resp.raise_for_status()
        data = resp.json()
        try:
            return data["candidates"][0]["content"]["parts"][0]["text"]
        except (KeyError, IndexError) as e:
            logger.error("Unexpected Gemini response: %s", data)
            raise RuntimeError("Malformed Gemini response") from e
