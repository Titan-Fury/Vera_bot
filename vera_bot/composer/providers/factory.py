"""Provider factory.

Selection priority:
  1. Explicit `LLM_PROVIDER` setting.
  2. Auto-detect from present API keys (gemini > openrouter > groq > together > perplexity).
  3. Ollama if `OLLAMA_HOST` is set.
  4. DummyLLMProvider as final fallback.

`LLM_MODEL`, if set, overrides the provider's default model.

If `LLM_FALLBACKS` env var is set, the primary provider is wrapped in a
FallbackLLMProvider chain. Format: pipe-delimited `<provider>:<model>` entries,
e.g. `openrouter:google/gemma-3-12b-it:free|openrouter-gemma:google/gemma-3-4b-it:free|ollama:qwen2.5:7b-instruct`.
Provider keys accepted: gemini, openrouter, openrouter-gemma, groq, together,
perplexity, ollama, dummy.
"""

from __future__ import annotations

import logging
import os
from typing import List, Optional

from .base import LLMProvider
from .dummy import DummyLLMProvider
from .fallback import FallbackLLMProvider
from .gemini import GeminiLLMProvider
from .groq import GroqLLMProvider
from .ollama import OllamaLLMProvider
from .openrouter import OpenRouterLLMProvider
from .perplexity import PerplexityLLMProvider
from .together import TogetherLLMProvider
from ...config import Settings

logger = logging.getLogger(__name__)


def _with_model(kwargs: dict, model: Optional[str]) -> dict:
    if model:
        kwargs["model"] = model
    return kwargs


def _build(name: str, settings: Settings, model: Optional[str]) -> LLMProvider:
    """Instantiate a single provider by name with optional model override."""
    name = name.strip().lower()
    if name == "gemini":
        return GeminiLLMProvider(**_with_model({"api_key": settings.gemini_api_key or ""}, model))
    if name == "openrouter":
        return OpenRouterLLMProvider(
            **_with_model(
                {"api_key": settings.openrouter_api_key or "", "referer": settings.openrouter_referer},
                model,
            )
        )
    if name == "openrouter-gemma":
        return OpenRouterLLMProvider(
            **_with_model(
                {
                    "api_key": settings.openrouter_api_key or "",
                    "referer": settings.openrouter_referer,
                    "merge_system": True,
                },
                model,
            )
        )
    if name == "perplexity":
        return PerplexityLLMProvider(**_with_model({"api_key": settings.perplexity_api_key or ""}, model))
    if name == "groq":
        return GroqLLMProvider(**_with_model({"api_key": settings.groq_api_key or ""}, model))
    if name == "together":
        return TogetherLLMProvider(**_with_model({"api_key": settings.together_api_key or ""}, model))
    if name == "ollama":
        return OllamaLLMProvider(**_with_model({"host": settings.ollama_host}, model))
    if name == "dummy":
        return DummyLLMProvider()
    raise ValueError(f"Unknown provider key: {name}")


def _parse_fallback_chain(spec: str) -> List[tuple[str, Optional[str]]]:
    """Parse `provider:model|provider:model` into [(provider, model), ...].

    Model may itself contain colons (e.g. `openai/gpt-oss-20b:free`), so we
    split on the FIRST colon only.
    """
    out: List[tuple[str, Optional[str]]] = []
    for raw in spec.split("|"):
        raw = raw.strip()
        if not raw:
            continue
        if ":" in raw:
            provider, model = raw.split(":", 1)
            out.append((provider.strip(), model.strip() or None))
        else:
            out.append((raw, None))
    return out


def make_provider(settings: Settings) -> LLMProvider:
    model = settings.llm_model
    explicit = (settings.llm_provider or "").strip().lower()

    # --- Primary -------------------------------------------------------------
    primary: Optional[LLMProvider] = None
    if explicit:
        try:
            primary = _build(explicit, settings, model)
            logger.info("LLM provider: explicit=%s model=%s", explicit, model or "<default>")
        except ValueError:
            logger.warning("Unknown LLM_PROVIDER=%s; falling through to auto-detect", explicit)

    if primary is None:
        auto_order = [
            ("gemini", settings.gemini_api_key),
            ("openrouter", settings.openrouter_api_key),
            ("groq", settings.groq_api_key),
            ("together", settings.together_api_key),
            ("perplexity", settings.perplexity_api_key),
        ]
        for name, key in auto_order:
            if key and key != "dummy":
                primary = _build(name, settings, model)
                logger.info("LLM provider: auto-selected %s", name)
                break

    if primary is None and settings.ollama_host:
        primary = _build("ollama", settings, model)
        logger.info("LLM provider: ollama (host=%s)", settings.ollama_host)

    if primary is None:
        logger.warning("LLM provider: no keys configured, falling back to DummyLLMProvider")
        primary = DummyLLMProvider()

    # --- Fallback chain ------------------------------------------------------
    fallback_spec = (os.environ.get("LLM_FALLBACKS") or "").strip()
    if not fallback_spec:
        return primary

    chain: List[LLMProvider] = [primary]
    for name, m in _parse_fallback_chain(fallback_spec):
        try:
            chain.append(_build(name, settings, m))
        except ValueError as e:
            logger.warning("LLM_FALLBACKS: skipping invalid entry %s:%s (%s)", name, m, e)

    if len(chain) == 1:
        return primary

    wrapped = FallbackLLMProvider(chain)
    logger.info("LLM provider chain: %s", wrapped.name)
    return wrapped
