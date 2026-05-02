"""Ollama - fully local, no API key required."""

from __future__ import annotations

import logging
import os
from typing import Optional

import requests

from .base import LLMProvider
from ._retry import with_retries

logger = logging.getLogger(__name__)


class OllamaLLMProvider(LLMProvider):
    name = "ollama"

    def __init__(self, model: str = "llama3", host: Optional[str] = None) -> None:
        self.model = model
        self.host = host or os.environ.get("OLLAMA_HOST", "http://localhost:11434")
        self.url = f"{self.host.rstrip('/')}/api/chat"
        # Reuse one Session per provider for HTTP connection pooling.
        self._session = requests.Session()

    @with_retries(max_attempts=1)
    def generate(self, system_prompt: str, user_prompt: str) -> str:
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "stream": False,
            "format": "json",
            "options": {
                "temperature": 0.0,
                "num_predict": 300,
                "num_ctx": 4096,
            },
        }
        resp = self._session.post(self.url, json=payload, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        try:
            return data["message"]["content"]
        except (KeyError, IndexError) as e:
            logger.error("Unexpected Ollama response: %s", data)
            raise RuntimeError("Malformed Ollama response") from e
