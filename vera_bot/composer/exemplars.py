"""High-scoring few-shot exemplars per trigger kind.

These are curated bodies that the judge rubric scores 9+ across all five axes.
At compose time the composer retrieves one exemplar by trigger-kind (and
audience where it matters) and injects it as a `[STYLE EXEMPLAR]` block.

Classic in-context learning lifts narrow-task quality ~15-20% without any
model change. Exemplars are illustrative only — the LLM must still ground
every claim in the merchant's actual context bundle. We mark exemplars as
"style reference, do not copy numbers" in the prompt to prevent verbatim leak.

Keep bodies <=220 chars and anchored on a fake-but-plausible number so the
LLM can pattern-match the *shape* (fact-first opener, one CTA, specific ask)
without lifting the values.
"""

from __future__ import annotations

from typing import Dict, List, Optional, Tuple

# (audience, body). audience: "merchant" | "customer" | "any"
_EXEMPLARS: Dict[str, List[Tuple[str, str]]] = {
    "research_digest": [
        ("merchant",
         "38% lower caries recurrence with 3-month fluoride recall per JIDA Oct 2026. "
         "Your high-risk adult cohort of 124 pts could benefit. "
         "Want a draft recall SMS for them this week? Reply YES or NO."),
    ],
    "regulation_change": [
        ("merchant",
         "2026-12-15: DCI revises IOPA max dose to 1.0 mSv. "
         "Your current protocol sits at 1.4 mSv. "
         "Shall I draft an updated radiograph SOP for staff? Reply YES or NO."),
    ],
    "competitor_opened": [
        ("merchant",
         "Smile Studio opened 1.3 km away at Rs.199 cleaning. "
         "Your cleaning sits at Rs.299 with a 4.6 rating — matchable in one tap. "
         "Want a retention offer pinned to your top 50 repeat patients? Reply YES or NO."),
    ],
    "review_theme_emerged": [
        ("merchant",
         "4 reviews in 30d flag slow delivery; common quote: \"took 50 min\". "
         "A live-ETA feature usually flips this inside 2 weeks in pizza joints. "
         "Enable it for your next 100 orders? Reply YES or NO."),
    ],
    "winback_eligible": [
        ("merchant",
         "38 days since Pro expiry; 24 lapsed customers added in the gap. "
         "Hair Spa at Rs.499 (vs Rs.699) this week typically converts 18%. "
         "Reactivate and push it? Reply YES or NO."),
    ],
    "customer_lapsed_hard": [
        ("customer",
         "Rashmi, 57 days since your last workout at PowerHouse. "
         "We remember your focus on weight loss — 3 free trial classes open this week. "
         "Shall I block Wed 7pm for you? Reply YES or NO."),
    ],
    "customer_lapsed_soft": [
        ("customer",
         "Hi Priya, 42 days since your last cleaning at Dr. Meera's. "
         "Your 6-month recall is now due. Sat 10am is open — shall I hold it? "
         "Reply YES or NO."),
    ],
    "trial_followup": [
        ("customer",
         "Karthik, thanks for your Zen Yoga trial on 22 April. "
         "55% of our trial students join the 30-day plan; yours is ready at Rs.1,800. "
         "Start Monday? Reply YES or NO."),
    ],
    "wedding_package_followup": [
        ("customer",
         "Kavya, 196 days to your wedding. Our 30-day Bridal Skin Prep is "
         "most popular 6 months out — Saturday slots still open. "
         "Shall I block yours? Reply YES or NO."),
    ],
    "renewal_due_soon": [
        ("merchant",
         "12 days till Pro renewal. Pro drove 4 calls from 980 views this quarter (CTR 0.018). "
         "Lapsed plans lose ~22% of calls in week 1. "
         "Renew now? Reply YES or NO."),
    ],
    "supply_alert": [
        ("merchant",
         "Recall: Atorvastatin batches AT2024-1102, AT2024-1108 from MfrZ. "
         "Quarantine immediately per CDSCO. "
         "Have you actioned this and flagged affected patients? Reply YES or NO."),
    ],
    "gbp_unverified": [
        ("merchant",
         "Your Google Business Profile is unverified — blocks ~30% of potential views. "
         "Verification takes 3 min via postcard or phone. "
         "Want me to send the step-by-step? Reply YES or NO."),
    ],
    "curious_ask_due": [
        ("merchant",
         "Quick read: which service drove the most walk-ins at Studio11 this week? "
         "I'll tailor the next nudge around it."),
    ],
    "dormant_with_vera": [
        ("merchant",
         "Olaplex No.9 just launched in India — bond protector for colour-treated hair. "
         "Your 26% view-lift vs peers says demand is there. "
         "Want a launch-week package outline? Reply YES or NO."),
    ],
    "category_seasonal": [
        ("merchant",
         "ORS demand up 40%, sunscreen +38%, anti-fungal +45% this summer vs baseline. "
         "Your current SKU list misses 2 of these. "
         "Shall I draft a reorder list? Reply YES or NO."),
    ],
    "seasonal_perf_dip": [
        ("merchant",
         "Views down 30% over the last 7 days — typical Apr-Jun dip in your category. "
         "A summer 20%-off trial usually recovers 60% of the gap. "
         "Set one up? Reply YES or NO."),
    ],
}


def get_exemplar(trigger_kind: str, is_customer_facing: bool) -> Optional[str]:
    """Return the best-matching exemplar body, or None.

    Prefer an exact audience match (customer vs merchant); fall back to "any"
    or the first available entry if only one side exists.
    """
    items = _EXEMPLARS.get(trigger_kind)
    if not items:
        return None
    want = "customer" if is_customer_facing else "merchant"
    for aud, body in items:
        if aud == want or aud == "any":
            return body
    # Last resort: return whatever we have.
    return items[0][1] if items else None


def format_exemplar_block(body: str) -> str:
    """Render exemplar with a strong "style only, do not copy" guard."""
    return (
        "\n[STYLE EXEMPLAR]  (match the SHAPE — fact-first opener, single CTA, "
        "specific ask. Do NOT copy numbers; use the merchant's real data.)\n"
        f"<<<\n{body}\n>>>"
    )
