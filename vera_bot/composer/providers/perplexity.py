"""Perplexity - Sonar models with live web grounding.

Ideal for `research_digest` triggers where the composer needs a fresh,
source-citable fact.
"""

from __future__ import annotations

from ._openai_compat import OpenAICompatibleProvider


class PerplexityLLMProvider(OpenAICompatibleProvider):
    name = "perplexity"

    def __init__(self, api_key: str, model: str = "sonar") -> None:
        # Perplexity ignores response_format; we rely on prompt-level JSON coercion.
        super().__init__(
            api_key=api_key,
            url="https://api.perplexity.ai/chat/completions",
            model=model,
            json_mode=False,
        )
