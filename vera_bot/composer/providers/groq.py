"""Groq - ultra-low-latency open-source inference (Llama 3.3, Mixtral)."""

from __future__ import annotations

from ._openai_compat import OpenAICompatibleProvider


class GroqLLMProvider(OpenAICompatibleProvider):
    name = "groq"

    def __init__(self, api_key: str, model: str = "llama-3.3-70b-versatile") -> None:
        super().__init__(
            api_key=api_key,
            url="https://api.groq.com/openai/v1/chat/completions",
            model=model,
            json_mode=True,
        )
