"""Composer subpackage - prompts, validators, providers, orchestrator."""
from .base import Composition, EngagementComposer
from .prompts import select_prompt, known_kinds
from .providers import make_provider
from .validators import (
    detect_auto_reply,
    detect_explicit_intent,
    detect_hard_no,
    enforce_length,
    is_repeat,
    strip_urls,
    validate_outbound,
)

__all__ = [
    "Composition",
    "EngagementComposer",
    "select_prompt",
    "known_kinds",
    "make_provider",
    "detect_auto_reply",
    "detect_explicit_intent",
    "detect_hard_no",
    "enforce_length",
    "is_repeat",
    "strip_urls",
    "validate_outbound",
]
