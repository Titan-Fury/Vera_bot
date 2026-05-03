"""Self-critique / reflection loop — LLM-as-judge rubric-gated revision.

For high-urgency triggers the composer can pass its draft through a second
LLM call acting as the challenge's own judge. If any of the five scoring
axes comes back below SELF_CRITIQUE_MIN_AXIS_SCORE, a one-shot rewrite is
requested with the weakest-axis feedback appended.

Guardrails:
- Total extra latency is bounded by SELF_CRITIQUE_BUDGET_S; if the budget
  is exhausted we return the original draft untouched (never a regression).
- Any parse error, provider error, or timeout silently falls back to the
  original draft — critique is pure upside.
- Only runs when body is non-empty (skip repeat-suppressed, fallback, etc.)
- Reuses the same provider as composition (no extra auth / quota planning).

The critic prompt mirrors judge_simulator.py:LLMScorer.SYSTEM so the LLM is
optimizing against the exact evaluation surface.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

from ..config import (
    SELF_CRITIQUE_BUDGET_S,
    SELF_CRITIQUE_MIN_AXIS_SCORE,
)

logger = logging.getLogger(__name__)


CRITIC_SYSTEM = """You are a STRICT judge for the magicpin AI Challenge. You score merchant engagement messages on five axes, 0-10 each.

AXES:
1. SPECIFICITY: verifiable numbers / dates / named sources in the body.
2. CATEGORY_FIT: voice.tone alignment; use vocab_allowed; avoid taboos.
3. MERCHANT_FIT: includes the merchant name AND one payload-specific fact.
4. TRIGGER_RELEVANCE: cites the "why now" window explicitly.
5. ENGAGEMENT: one persuasion lever + low-friction CTA.

Respond with ONLY this JSON (no prose, no code fences):
{"specificity": <0-10>, "category_fit": <0-10>, "merchant_fit": <0-10>, "trigger_relevance": <0-10>, "engagement": <0-10>, "weakest_axis": "<name>", "fix_hint": "<one imperative sentence>"}"""


@dataclass
class CritiqueResult:
    scores: Dict[str, int]
    weakest_axis: str
    fix_hint: str
    min_score: int


def _parse_critique(raw: str) -> Optional[CritiqueResult]:
    """Extract the first JSON object; coerce axis scores to 0-10 ints."""
    match = re.search(r"\{[\s\S]*\}", raw)
    if not match:
        return None
    try:
        data = json.loads(match.group())
    except json.JSONDecodeError:
        return None
    axes = ("specificity", "category_fit", "merchant_fit", "trigger_relevance", "engagement")
    scores: Dict[str, int] = {}
    for k in axes:
        try:
            scores[k] = max(0, min(10, int(data.get(k, 5))))
        except (TypeError, ValueError):
            scores[k] = 5
    weakest = str(data.get("weakest_axis") or min(scores, key=scores.get))[:32]
    fix_hint = str(data.get("fix_hint") or "").strip()[:240]
    return CritiqueResult(
        scores=scores,
        weakest_axis=weakest,
        fix_hint=fix_hint,
        min_score=min(scores.values()),
    )


def _build_critic_user_prompt(body: str, formatted_context: str, trigger_kind: str) -> str:
    # Truncate context so the critique call stays snappy; scorer only needs the
    # essentials to rank axes.
    context_tail = formatted_context[:1800]
    return (
        f"SCORE THIS MESSAGE.\n\n"
        f"Trigger kind: {trigger_kind}\n\n"
        f"Context (truncated):\n{context_tail}\n\n"
        f'Body ({len(body)} chars): "{body}"'
    )


def critique(
    provider: Any,
    body: str,
    formatted_context: str,
    trigger_kind: str,
    deadline_s: float,
) -> Optional[CritiqueResult]:
    """Call the critic LLM. Returns None on any error or if past deadline."""
    if not body:
        return None
    now = time.perf_counter()
    if deadline_s - now < 0.5:
        return None
    user_prompt = _build_critic_user_prompt(body, formatted_context, trigger_kind)
    try:
        raw = provider.generate(CRITIC_SYSTEM, user_prompt)
    except Exception as e:
        logger.info("critique: provider error (%s); skipping", type(e).__name__)
        return None
    return _parse_critique(raw)


def build_revision_prompt(
    original_user_prompt: str,
    critique: CritiqueResult,
    draft_body: str,
) -> str:
    """Append rubric feedback to the original compose prompt for a one-shot fix."""
    axes_str = ", ".join(f"{k}={v}" for k, v in critique.scores.items())
    return (
        f"{original_user_prompt}\n\n"
        f"[YOUR PREVIOUS DRAFT]\n{draft_body}\n\n"
        f"[CRITIQUE]  axes: {axes_str}. "
        f"Weakest: {critique.weakest_axis} ({critique.scores.get(critique.weakest_axis, '?')}/10). "
        f"Fix: {critique.fix_hint}\n"
        "Return a revised JSON message that lifts the weakest axis to >=8 while "
        "keeping every other axis at or above its current score. Output JSON only."
    )


def should_critique(urgency: Any, urgency_threshold: int) -> bool:
    """Gate: only critique high-urgency drafts."""
    try:
        return int(urgency) >= urgency_threshold
    except (TypeError, ValueError):
        return False


def budget_deadline(started_at: float) -> float:
    """Monotonic deadline for the critique+revision round-trip."""
    return started_at + SELF_CRITIQUE_BUDGET_S


def min_axis_threshold() -> int:
    return SELF_CRITIQUE_MIN_AXIS_SCORE
