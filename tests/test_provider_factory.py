"""Provider factory selection matrix.

Note: `make_provider()` wraps results in a `FallbackLLMProvider` when the
`LLM_FALLBACKS` env var is set (it usually is via `.env`). These tests
monkeypatch it out so we verify the _primary_ selection logic in isolation.
"""
from __future__ import annotations

from dataclasses import replace

import pytest

from vera_bot.composer.providers import (
    DummyLLMProvider,
    GeminiLLMProvider,
    GroqLLMProvider,
    OllamaLLMProvider,
    OpenRouterLLMProvider,
    PerplexityLLMProvider,
    TogetherLLMProvider,
    make_provider,
)
from vera_bot.config import SETTINGS


@pytest.fixture(autouse=True)
def _no_fallback_chain(monkeypatch):
    monkeypatch.delenv("LLM_FALLBACKS", raising=False)


def _with(**kw):
    return replace(SETTINGS, **kw)


def test_explicit_dummy():
    assert isinstance(make_provider(_with(llm_provider="dummy")), DummyLLMProvider)


def test_explicit_each_provider():
    cases = [
        ("gemini", {"gemini_api_key": "k"}, GeminiLLMProvider),
        ("openrouter", {"openrouter_api_key": "k"}, OpenRouterLLMProvider),
        ("perplexity", {"perplexity_api_key": "k"}, PerplexityLLMProvider),
        ("groq", {"groq_api_key": "k"}, GroqLLMProvider),
        ("together", {"together_api_key": "k"}, TogetherLLMProvider),
        ("ollama", {"ollama_host": "http://localhost:11434"}, OllamaLLMProvider),
    ]
    for name, extra, cls in cases:
        assert isinstance(make_provider(_with(llm_provider=name, **extra)), cls), name


def test_auto_detect_priority_gemini_wins():
    s = _with(llm_provider=None, gemini_api_key="g", openrouter_api_key="o", groq_api_key="gr")
    assert isinstance(make_provider(s), GeminiLLMProvider)


def test_auto_detect_falls_through_to_ollama():
    s = _with(
        llm_provider=None, gemini_api_key=None, openrouter_api_key=None,
        groq_api_key=None, together_api_key=None, perplexity_api_key=None,
        ollama_host="http://localhost:11434",
    )
    assert isinstance(make_provider(s), OllamaLLMProvider)


def test_auto_detect_falls_through_to_dummy():
    s = _with(
        llm_provider=None, gemini_api_key=None, openrouter_api_key=None,
        groq_api_key=None, together_api_key=None, perplexity_api_key=None,
        ollama_host=None,
    )
    assert isinstance(make_provider(s), DummyLLMProvider)


def test_model_override_is_applied():
    p = make_provider(_with(llm_provider="gemini", gemini_api_key="k", llm_model="gemini-pro-99"))
    assert p.model == "gemini-pro-99"
