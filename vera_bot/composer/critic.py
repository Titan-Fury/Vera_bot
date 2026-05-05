"""Self-critique / reflection loop - LLM-as-judge rubric-gated revision.

For high-urgency triggers the composer can pass its draft through a second
LLM call acting as the challenge's own judge.

v1 (default): 5-axis rubric (Specificity, Category Fit, Merchant Fit,
Trigger Relevance, Engagement). Single-pass rewrite if any axis falls below
SELF_CRITIQUE_MIN_AXIS_SCORE.

v2 (CRITIC_V2_ENABLED=1, additive): adds two axes - Compliance and
Conversion Clarity - and uses per-trigger-kind weighted scoring. Compliance
below CRITIC_COMPLIANCE_HARD_BLOCK is a HARD STOP (suppresses send entirely),
not a rewrite signal. Multi-draft pick-best is implemented in base.py via
CRITIC_NUM_DRAFTS.

Guardrails (unchanged):
- Total extra latency bounded by SELF_CRITIQUE_BUDGET_S
- Any parse error / provider error / timeout falls back to original draft
- Only runs on non-empty bodies
- Reuses the same provider as composition
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Tuple

from ..config import (
    CRITIC_COMPLIANCE_HARD_BLOCK,
    CRITIC_V2_ENABLED,
    CRITIC_WEIGHTED_THRESHOLD,
    SELF_CRITIQUE_BUDGET_S,
    SELF_CRITIQUE_MIN_AXIS_SCORE,
)

logger = logging.getLogger(__name__)


# ---- v1 axes (always present in output JSON) -------------------------------
V1_AXES: Tuple[str, ...] = (
    "specificity",
    "category_fit",
    "merchant_fit",
    "trigger_relevance",
    "engagement",
)

# ---- v2 axes (added when CRITIC_V2_ENABLED=1) ------------------------------
V2_EXTRA_AXES: Tuple[str, ...] = (
    "compliance",
    "conversion_clarity",
)

ALL_AXES_V2: Tuple[str, ...] = V1_AXES + V2_EXTRA_AXES


CRITIC_SYSTEM_V1 = """You are a STRICT judge for the magicpin AI Challenge. You score merchant engagement messages on five axes, 0-10 each.

AXES:
1. SPECIFICITY: verifiable numbers / dates / named sources in the body.
2. CATEGORY_FIT: voice.tone alignment; use vocab_allowed; avoid taboos.
3. MERCHANT_FIT: includes the merchant name AND one payload-specific fact.
4. TRIGGER_RELEVANCE: cites the "why now" window explicitly.
5. ENGAGEMENT: one persuasion lever + low-friction CTA.

Respond with ONLY this JSON (no prose, no code fences):
{"specificity": <0-10>, "category_fit": <0-10>, "merchant_fit": <0-10>, "trigger_relevance": <0-10>, "engagement": <0-10>, "weakest_axis": "<name>", "fix_hint": "<one imperative sentence>"}"""


CRITIC_SYSTEM_V2 = """You are a STRICT judge for merchant engagement messages on WhatsApp. You score each draft on SEVEN axes, 0-10 each.

AXES:
1. SPECIFICITY: verifiable numbers / dates / named sources in the body.
2. CATEGORY_FIT: voice.tone alignment; use vocab_allowed; avoid taboos.
3. MERCHANT_FIT: includes the merchant name AND one payload-specific fact.
4. TRIGGER_RELEVANCE: cites the "why now" window explicitly.
5. ENGAGEMENT: one persuasion lever + low-friction CTA.
6. COMPLIANCE: 10 = safe; 0 = unsafe. Penalize HEAVILY if the message contains: unsupported medical / health-cure claims, financial-return guarantees, comparative defamation against named competitors, exaggerated superlatives ("best in the world"), politically or religiously divisive language, anything that could cause a WhatsApp Business account ban or legal exposure. A clinic message saying "guaranteed cure" should score <=3. Default to 8-10 for clean messages.
7. CONVERSION_CLARITY: is the desired action unambiguous? Exactly ONE call-to-action? Low friction ("Reply YES" beats "Visit our site and fill the form")? 10 = perfectly clear single CTA; 0 = no CTA or multiple competing CTAs.

Respond with ONLY this JSON (no prose, no code fences):
{"specificity": <0-10>, "category_fit": <0-10>, "merchant_fit": <0-10>, "trigger_relevance": <0-10>, "engagement": <0-10>, "compliance": <0-10>, "conversion_clarity": <0-10>, "weakest_axis": "<name>", "fix_hint": "<one imperative sentence>"}"""


# ---- Per-trigger-kind weight maps (v2 only) --------------------------------
# Weights sum to ~1.0 across the 7 axes for each kind. Compliance always gets
# weight even if low, because hard-block is a separate signal layered on top.
_DEFAULT_WEIGHTS: Dict[str, float] = {
    "specificity": 0.20,
    "category_fit": 0.15,
    "merchant_fit": 0.15,
    "trigger_relevance": 0.15,
    "engagement": 0.15,
    "compliance": 0.10,
    "conversion_clarity": 0.10,
}

_KIND_WEIGHTS: Dict[str, Dict[str, float]] = {
    "appointment_tomorrow": {
        "specificity": 0.30, "trigger_relevance": 0.20, "engagement": 0.15,
        "merchant_fit": 0.10, "category_fit": 0.05,
        "compliance": 0.10, "conversion_clarity": 0.10,
    },
    "chronic_refill_due": {
        "specificity": 0.30, "compliance": 0.20, "trigger_relevance": 0.15,
        "merchant_fit": 0.10, "engagement": 0.10, "category_fit": 0.05,
        "conversion_clarity": 0.10,
    },
    "recall_due": {
        "specificity": 0.25, "trigger_relevance": 0.20, "engagement": 0.15,
        "merchant_fit": 0.15, "category_fit": 0.05,
        "compliance": 0.10, "conversion_clarity": 0.10,
    },
    "competitor_opened": {
        "merchant_fit": 0.25, "trigger_relevance": 0.20, "specificity": 0.15,
        "engagement": 0.15, "category_fit": 0.05,
        "compliance": 0.10, "conversion_clarity": 0.10,
    },
    "festival_upcoming": {
        "category_fit": 0.25, "engagement": 0.20, "trigger_relevance": 0.15,
        "merchant_fit": 0.10, "specificity": 0.10,
        "compliance": 0.10, "conversion_clarity": 0.10,
    },
    "perf_spike": {
        "specificity": 0.25, "merchant_fit": 0.20, "engagement": 0.15,
        "trigger_relevance": 0.15, "category_fit": 0.05,
        "compliance": 0.10, "conversion_clarity": 0.10,
    },
    "perf_dip": {
        "specificity": 0.25, "merchant_fit": 0.15, "engagement": 0.20,
        "trigger_relevance": 0.15, "category_fit": 0.05,
        "compliance": 0.10, "conversion_clarity": 0.10,
    },
    "milestone_reached": {
        "merchant_fit": 0.20, "specificity": 0.15, "engagement": 0.20,
        "category_fit": 0.10, "trigger_relevance": 0.15,
        "compliance": 0.10, "conversion_clarity": 0.10,
    },
    "research_digest": {
        "specificity": 0.30, "category_fit": 0.20, "merchant_fit": 0.10,
        "trigger_relevance": 0.10, "engagement": 0.10,
        "compliance": 0.10, "conversion_clarity": 0.10,
    },
    "competitor_opened_dentist": {
        "merchant_fit": 0.25, "trigger_relevance": 0.20, "specificity": 0.15,
        "engagement": 0.15, "category_fit": 0.05,
        "compliance": 0.10, "conversion_clarity": 0.10,
    },
}


def get_weights(trigger_kind: str) -> Dict[str, float]:
    """Return axis weights for a trigger kind; default if unknown."""
    return _KIND_WEIGHTS.get(trigger_kind, _DEFAULT_WEIGHTS)


@dataclass
class CritiqueResult:
    """Critique output. v1 fields preserved; v2 fields populated when enabled.

    Attributes:
        scores: axis -> 0-10. v1 has 5 keys, v2 has 7.
        weakest_axis: name of the axis with the lowest score.
        fix_hint: one imperative sentence from the judge.
        min_score: minimum across all axes (v1 contract).
        weighted_score: weighted sum across axes scaled 0-10 (v2 only; 0.0 in v1).
        compliance_violation: True if v2 compliance < hard_block threshold.
        is_v2: marks which schema this came from.
    """
    scores: Dict[str, int]
    weakest_axis: str
    fix_hint: str
    min_score: int
    weighted_score: float = 0.0
    compliance_violation: bool = False
    is_v2: bool = False


def _coerce_axis(value: Any, default: int = 5) -> int:
    try:
        return max(0, min(10, int(value)))
    except (TypeError, ValueError):
        return default


def _parse_critique(raw: str, trigger_kind: str = "") -> Optional[CritiqueResult]:
    """Extract first JSON object; coerce axis scores to 0-10 ints.

    Auto-detects whether the LLM returned v1 (5 axes) or v2 (7 axes) shape.
    Returns CritiqueResult with both min_score and weighted_score populated
    when the v2 axes are present.
    """
    match = re.search(r"\{[\s\S]*\}", raw)
    if not match:
        return None
    try:
        data = json.loads(match.group())
    except json.JSONDecodeError:
        return None

    # Always parse v1 axes
    scores: Dict[str, int] = {k: _coerce_axis(data.get(k, 5)) for k in V1_AXES}

    # Optionally parse v2 axes if present in the JSON
    has_v2 = all(k in data for k in V2_EXTRA_AXES)
    if has_v2:
        for k in V2_EXTRA_AXES:
            scores[k] = _coerce_axis(data.get(k, 5))

    weakest = str(data.get("weakest_axis") or min(scores, key=scores.get))[:32]
    fix_hint = str(data.get("fix_hint") or "").strip()[:240]
    min_score = min(scores.values())

    weighted = 0.0
    compliance_violation = False
    if has_v2:
        weights = get_weights(trigger_kind)
        weighted = sum(scores[k] * weights.get(k, 0.0) for k in scores) / max(
            1e-9, sum(weights.get(k, 0.0) for k in scores)
        )
        compliance_violation = scores.get("compliance", 10) < CRITIC_COMPLIANCE_HARD_BLOCK

    return CritiqueResult(
        scores=scores,
        weakest_axis=weakest,
        fix_hint=fix_hint,
        min_score=min_score,
        weighted_score=weighted,
        compliance_violation=compliance_violation,
        is_v2=has_v2,
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
    system_prompt = CRITIC_SYSTEM_V2 if CRITIC_V2_ENABLED else CRITIC_SYSTEM_V1
    try:
        raw = provider.generate(system_prompt, user_prompt)
    except Exception as e:
        logger.info("critique: provider error (%s); skipping", type(e).__name__)
        return None
    return _parse_critique(raw, trigger_kind)


def build_revision_prompt(
    original_user_prompt: str,
    critique: CritiqueResult,
    draft_body: str,
) -> str:
    """Append rubric feedback to the original compose prompt for a one-shot fix."""
    axes_str = ", ".join(f"{k}={v}" for k, v in critique.scores.items())
    weighted_note = (
        f" (weighted={critique.weighted_score:.1f})" if critique.is_v2 else ""
    )
    return (
        f"{original_user_prompt}\n\n"
        f"[YOUR PREVIOUS DRAFT]\n{draft_body}\n\n"
        f"[CRITIQUE]  axes: {axes_str}{weighted_note}. "
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


def passes_quality_gate(result: CritiqueResult) -> bool:
    """v1: min_score >= MIN_AXIS. v2: weighted_score >= WEIGHTED_THRESHOLD AND compliance OK.

    A failed quality gate triggers revision. Compliance hard-block is checked
    SEPARATELY by the caller (it suppresses the send instead of just rewriting).
    """
    if result.is_v2:
        if result.weighted_score < CRITIC_WEIGHTED_THRESHOLD:
            return False
        return True
    return result.min_score >= SELF_CRITIQUE_MIN_AXIS_SCORE


def pick_best(results: list) -> Optional[CritiqueResult]:
    """Multi-draft pick-best: return the result with highest score.

    Uses weighted_score for v2 results, min_score for v1. Ties broken by
    min_score (avoids picking a draft with one terrible axis).
    """
    valid = [r for r in results if r is not None]
    if not valid:
        return None
    return max(valid, key=lambda r: (r.weighted_score if r.is_v2 else r.min_score, r.min_score))
