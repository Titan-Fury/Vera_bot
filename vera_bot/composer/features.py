"""Narrative feature engineering — computes judge-relevant features on ingest.

The bot's default /v1/tick messages score highest when the LLM has precomputed,
ready-to-cite facts (percentile ranks, trend direction, revenue-at-risk, etc.)
rather than raw numbers. This module takes a context bundle and produces a flat
dict of short, human-readable strings to surface in the prompt.

All functions are defensive: any missing or malformed input returns a partial
result rather than raising. Feature computation is O(1) per bundle.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional


def _num(x: Any, default: Optional[float] = None) -> Optional[float]:
    try:
        if x is None or isinstance(x, bool):
            return default
        return float(x)
    except (TypeError, ValueError):
        return default


def _trend_label(delta_pct: Optional[float]) -> Optional[str]:
    if delta_pct is None:
        return None
    if delta_pct <= -0.30:
        return "declining_sharply"
    if delta_pct <= -0.10:
        return "declining"
    if delta_pct >= 0.30:
        return "spiking"
    if delta_pct >= 0.10:
        return "growing"
    return "steady"


def _percentile_rank(value: Optional[float], peer_avg: Optional[float]) -> Optional[int]:
    """Rough percentile: 50th when equal to peer_avg, +/-25 per half-multiple.

    This is a coarse heuristic, not a true percentile — real cohort percentiles
    would need a distribution we don't have. The number is only ever shown to
    the LLM as a qualitative anchor; we keep it clearly approximate.
    """
    if value is None or peer_avg is None or peer_avg <= 0:
        return None
    ratio = value / peer_avg
    # Map ratio to 0..100 with 1.0 -> 50.
    if ratio >= 2.0:
        pct = 95
    elif ratio >= 1.5:
        pct = 80
    elif ratio >= 1.1:
        pct = 65
    elif ratio >= 0.9:
        pct = 50
    elif ratio >= 0.6:
        pct = 30
    elif ratio >= 0.3:
        pct = 15
    else:
        pct = 5
    return pct


def _retention_gap_pct(merchant_agg: Dict[str, Any], peer_stats: Dict[str, Any]) -> Optional[int]:
    m = _num(merchant_agg.get("retention_6mo_pct"))
    p = _num(peer_stats.get("retention_6mo_pct"))
    if m is None or p is None:
        return None
    return int(round((p - m) * 100))


def _days_to_nearest_festival(category: Dict[str, Any]) -> Optional[int]:
    beats = category.get("seasonal_beats") or []
    days: List[int] = []
    for b in beats:
        d = _num(b.get("days_until"))
        if d is not None and d >= 0:
            days.append(int(d))
    return min(days) if days else None


def _best_offer_pick(
    category: Dict[str, Any],
    merchant: Dict[str, Any],
    trigger_kind: str,
) -> Optional[str]:
    """Pick an offer title from the category catalog that fits the signal.

    Simple signal-driven selection: winback/lapsed/dormant trigger -> repeat_user
    audience; everything else -> new_user audience. Prefers offers whose title
    surfaces a specific number (₹ / %) over vague memberships.
    """
    catalog = category.get("offer_catalog") or []
    if not catalog:
        return None
    target_audience = (
        "repeat_user"
        if any(k in trigger_kind for k in ("winback", "lapsed", "dormant", "recall"))
        else "new_user"
    )
    # Active merchant offer takes precedence.
    for o in merchant.get("offers") or []:
        if (o.get("status") == "active") and o.get("title"):
            return o["title"]
    # Fall back to catalog pick.
    ranked = sorted(
        (o for o in catalog if isinstance(o, dict) and o.get("title")),
        key=lambda o: (
            0 if o.get("audience") == target_audience else 1,
            0 if any(ch.isdigit() for ch in str(o.get("title", ""))) else 1,
        ),
    )
    return ranked[0]["title"] if ranked else None


def _urgency_window(trigger: Dict[str, Any]) -> Optional[str]:
    payload = trigger.get("payload") or {}
    for key, label in (
        ("days_until", "days"),
        ("days_remaining", "days"),
        ("days_left", "days"),
        ("hours_until", "hours"),
    ):
        v = _num(payload.get(key))
        if v is not None:
            return f"{int(v)} {label}"
    deadline = payload.get("deadline_iso") or payload.get("due_date") or payload.get("expires_at")
    if deadline:
        return f"by {deadline}"
    return None


def compute_features(bundle: Dict[str, Any]) -> Dict[str, Any]:
    """Return a flat dict of narrative features for prompt injection.

    Missing inputs yield partial output; no key is required. Values are kept
    short (<=80 chars each) so the full block stays within prompt budget.
    """
    merchant = bundle.get("merchant") or {}
    category = bundle.get("category") or {}
    trigger = bundle.get("trigger") or {}
    customer = bundle.get("customer") or {}

    perf = merchant.get("performance") or {}
    peer = category.get("peer_stats") or {}
    agg = merchant.get("customer_aggregate") or {}
    delta_7d = perf.get("delta_7d") or {}

    views = _num(perf.get("views"))
    calls = _num(perf.get("calls"))
    views_pct = _num(delta_7d.get("views_pct"))
    calls_pct = _num(delta_7d.get("calls_pct"))

    feats: Dict[str, Any] = {}

    views_pct_rank = _percentile_rank(views, _num(peer.get("avg_views_30d")))
    if views_pct_rank is not None:
        feats["views_percentile_vs_peers"] = f"~P{views_pct_rank}"
    calls_pct_rank = _percentile_rank(calls, _num(peer.get("avg_calls_30d")))
    if calls_pct_rank is not None:
        feats["calls_percentile_vs_peers"] = f"~P{calls_pct_rank}"

    # Pick the most-negative 7d delta as the headline trend signal.
    trend_candidates = [
        ("views", views_pct),
        ("calls", calls_pct),
        ("ctr", _num(delta_7d.get("ctr_pct"))),
    ]
    worst = min(
        ((m, p) for m, p in trend_candidates if p is not None),
        key=lambda mp: mp[1],
        default=(None, None),
    )
    if worst[1] is not None:
        label = _trend_label(worst[1])
        if label:
            feats["trend_7d_headline"] = f"{worst[0]} {label} ({worst[1] * 100:+.0f}% vs prev 7d)"

    gap = _retention_gap_pct(agg, peer)
    if gap is not None and gap > 0:
        feats["retention_gap_vs_peers_pts"] = f"{gap} pts below peer avg"

    # Revenue-at-risk proxy (no ticket size in dataset; use lapsed count).
    lapsed = _num(agg.get("lapsed_180d_plus"))
    if lapsed is not None and lapsed > 0:
        feats["lapsed_customer_count_180d"] = f"{int(lapsed)} customers lapsed 180d+"

    festival_days = _days_to_nearest_festival(category)
    if festival_days is not None:
        feats["nearest_seasonal_beat_days"] = f"{festival_days} days"

    kind = trigger.get("kind") or ""
    best_offer = _best_offer_pick(category, merchant, kind)
    if best_offer:
        feats["suggested_offer"] = best_offer

    window = _urgency_window(trigger)
    if window:
        feats["why_now_window"] = window

    # Subscription runway (Pro renewal etc.).
    sub = merchant.get("subscription") or {}
    days_remaining = _num(sub.get("days_remaining"))
    if days_remaining is not None:
        feats["subscription_days_remaining"] = f"{int(days_remaining)}"

    # Customer-facing lift: gap since last visit.
    rel = customer.get("relationship") or {} if customer else {}
    last_visit = rel.get("last_visit") or rel.get("last_service_date")
    if last_visit:
        feats["customer_last_visit"] = str(last_visit)[:32]

    return feats


def format_features_block(feats: Dict[str, Any]) -> str:
    """Render as a compact prompt-friendly block; empty string if no features."""
    if not feats:
        return ""
    lines = ["\n[COMPUTED FEATURES]  (cite these instead of raw numbers)"]
    for k, v in feats.items():
        lines.append(f"- {k}: {v}")
    return "\n".join(lines)
