"""Together AI - hosted open-source models (Llama, Qwen, DeepSeek)."""

from __future__ import annotations

from ._openai_compat import OpenAICompatibleProvider


class TogetherLLMProvider(OpenAICompatibleProvider):
    name = "together"

    def __init__(self, api_key: str, model: str = "meta-llama/Llama-3.3-70B-Instruct-Turbo") -> None:
        super().__init__(
            api_key=api_key,
            url="https://api.together.xyz/v1/chat/completions",
            model=model,
            json_mode=True,
        )
