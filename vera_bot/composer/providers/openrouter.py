"""OpenRouter - one key, many models."""

from __future__ import annotations

from typing import Dict

from ._openai_compat import OpenAICompatibleProvider
from ._retry import with_retries


class OpenRouterLLMProvider(OpenAICompatibleProvider):
    name = "openrouter"

    def __init__(
        self,
        api_key: str,
        model: str = "meta-llama/llama-3.3-70b-instruct",
        referer: str = "https://magicpin.in",
        merge_system: bool = False,
    ) -> None:
        super().__init__(
            api_key=api_key,
            url="https://openrouter.ai/api/v1/chat/completions",
            model=model,
            extra_headers={
                "HTTP-Referer": referer,
                "X-Title": "Vera Engagement Bot",
            },
            # Many OpenRouter free-tier upstreams silently return content=null
            # when response_format=json_object is sent. The composer parser is
            # fence/prose tolerant, so we ask for plain text and parse it.
            json_mode=False,
        )
        # Gemma endpoints reject role:"system" with HTTP 400. When
        # merge_system=True we collapse the system prompt into a user message.
        self.merge_system = merge_system
        if merge_system:
            self.name = "openrouter-gemma"

    @with_retries()
    def generate(self, system_prompt: str, user_prompt: str) -> str:
        if not self.merge_system:
            return super().generate(system_prompt, user_prompt)

        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }
        headers.update(self.extra_headers)
        payload: Dict = {
            "model": self.model,
            "messages": [
                {"role": "user", "content": f"{system_prompt}\n\n{user_prompt}"},
            ],
            "temperature": 0.0,
            "max_tokens": self.max_tokens,
        }
        resp = self._session.post(self.url, headers=headers, json=payload, timeout=self.timeout_s)
        resp.raise_for_status()
        data = resp.json()
        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError) as e:
            raise RuntimeError(f"Malformed response from {self.name}") from e
        if content is None:
            raise RuntimeError(f"Null content from {self.name}")
        return content
