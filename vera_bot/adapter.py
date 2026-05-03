"""ContextAggregator - builds a 4-context bundle from the in-memory store."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from .store import ContextStore
from .composer.features import compute_features, format_features_block


@dataclass
class CompositionBundle:
    trigger: Optional[Dict[str, Any]] = None
    merchant: Optional[Dict[str, Any]] = None
    category: Optional[Dict[str, Any]] = None
    customer: Optional[Dict[str, Any]] = None

    def as_dict(self) -> Dict[str, Any]:
        return {
            "trigger": self.trigger,
            "merchant": self.merchant,
            "category": self.category,
            "customer": self.customer,
        }


class ContextAggregator:
    def __init__(self, store: ContextStore) -> None:
        self.store = store

    def build(
        self,
        trigger_id: Optional[str] = None,
        merchant_id: Optional[str] = None,
        customer_id: Optional[str] = None,
    ) -> CompositionBundle:
        bundle = CompositionBundle()

        if trigger_id:
            bundle.trigger = self.store.get("trigger", trigger_id)

        if not merchant_id and bundle.trigger:
            merchant_id = bundle.trigger.get("merchant_id") or bundle.trigger.get("payload", {}).get("merchant_id")

        if merchant_id:
            bundle.merchant = self.store.get("merchant", merchant_id)

        if bundle.merchant:
            category_slug = bundle.merchant.get("category_slug")
            if category_slug:
                bundle.category = self.store.get("category", category_slug)

        if bundle.trigger and bundle.trigger.get("customer_id"):
            customer_id = bundle.trigger.get("customer_id")
        if customer_id:
            bundle.customer = self.store.get("customer", customer_id)

        return bundle


def format_context_for_prompt(bundle: CompositionBundle | Dict[str, Any]) -> str:
    b = bundle.as_dict() if isinstance(bundle, CompositionBundle) else bundle
    lines: List[str] = ["=== CONTEXT BUNDLE ==="]

    trigger = b.get("trigger") or {}
    trigger_payload = trigger.get("payload") or {}
    wanted_digest_id = trigger_payload.get("top_item_id") or trigger_payload.get("digest_item_id")

    if b.get("category"):
        c = b["category"]
        lines.append("\n[CATEGORY CONTEXT]")
        lines.append(f"Vertical: {c.get('slug', 'N/A')}")
        voice = c.get("voice", {}) or {}
        lines.append(f"Voice/Tone: {voice.get('tone', 'professional')}")
        if voice.get("vocab_allowed"):
            lines.append(f"Allowed Vocab: {', '.join(voice['vocab_allowed'])}")
        if voice.get("taboos"):
            lines.append(f"Taboos: {', '.join(voice['taboos'])}")
        digests = c.get("digest", []) or []
        # If the trigger names a specific digest item, surface THAT one in full;
        # else fall back to the first item as a generic insight.
        chosen = None
        if wanted_digest_id:
            chosen = next((d for d in digests if d.get("id") == wanted_digest_id), None)
        if chosen is None and digests:
            chosen = digests[0]
        if chosen:
            lines.append(f"Referenced Insight: '{chosen.get('title','')}' ({chosen.get('source','')})")
            kf = chosen.get("key_findings") or chosen.get("body") or chosen.get("summary")
            if kf:
                lines.append(f"Key Finding: {kf}")
        peer = c.get("peer_stats", {}) or {}
        if peer:
            bits = [f"{k}={v}" for k, v in peer.items() if k != "scope" and v not in (None, "")]
            if bits:
                lines.append("Peer Stats: " + ", ".join(bits))
        # Offer catalog gives the LLM ready-made offer shapes to propose.
        offers_catalog = c.get("offer_catalog") or []
        if offers_catalog:
            labels = [f"{o.get('title','')}" for o in offers_catalog[:4] if o.get("title")]
            if labels:
                lines.append("Available Offer Shapes: " + " | ".join(labels))
        # Current / upcoming seasonal beat — grounds festival + seasonal kinds.
        beats = c.get("seasonal_beats") or []
        if beats:
            b0 = beats[0]
            lines.append(f"Seasonal Beat: {b0.get('month_range','')} — {b0.get('note','')}")
        # Trend signals — top-of-mind category searches.
        trends = c.get("trend_signals") or []
        if trends:
            t0 = trends[0]
            delta = t0.get("delta_yoy")
            delta_str = f" +{int(float(delta)*100)}% YoY" if delta is not None else ""
            lines.append(f"Top Trend Query: '{t0.get('query','')}'{delta_str}")

    if b.get("merchant"):
        m = b["merchant"]
        ident = m.get("identity", {}) or {}
        lines.append("\n[MERCHANT CONTEXT]")
        lines.append(f"Merchant ID: {m.get('merchant_id','N/A')}")
        lines.append(f"Name: {ident.get('name', 'N/A')}")
        lines.append(f"Location: {ident.get('locality', 'N/A')}, {ident.get('city', 'N/A')}")
        if ident.get("languages"):
            lines.append(f"Preferred Languages: {', '.join(ident['languages'])}")
        sub = m.get("subscription") or {}
        if sub:
            days = sub.get("days_remaining")
            lines.append(
                f"Subscription: {sub.get('plan','?')} ({sub.get('status','?')})"
                + (f", {days} days remaining" if days is not None else "")
            )
        perf = m.get("performance", {}) or {}
        if perf:
            lines.append(
                f"Performance (30d): {perf.get('views',0)} views, {perf.get('calls',0)} calls, CTR: {perf.get('ctr',0)}"
            )
            d7 = perf.get("delta_7d", {}) or {}
            if d7:
                lines.append(f"7-day Delta: views {d7.get('views_pct',0)*100:.0f}%")
        # Customer cohort aggregates ground dormant/winback/lapsed kinds.
        agg = m.get("customer_aggregate") or {}
        if agg:
            bits = [f"{k}={v}" for k, v in agg.items() if v not in (None, "")]
            if bits:
                lines.append("Customer Cohort: " + ", ".join(bits))
        # Recurring review themes — anchor for review_theme_emerged kind.
        themes = m.get("review_themes") or []
        if themes:
            top = themes[0]
            lines.append(
                f"Top Review Theme: '{top.get('theme','')}' "
                f"({top.get('sentiment','?')}, {top.get('occurrences_30d',0)} in 30d): "
                f"\"{top.get('common_quote','')}\""
            )
        active_offers = [o for o in (m.get("offers") or []) if o.get("status") == "active"]
        if active_offers:
            lines.append("Active Offers: " + " | ".join(o.get("title", "") for o in active_offers))
        if m.get("signals"):
            lines.append(f"Account Signals: {', '.join(m['signals'])}")

    if b.get("trigger"):
        t = b["trigger"]
        lines.append("\n[TRIGGER CONTEXT]")
        lines.append(f"Trigger Type: {t.get('kind', 'N/A')}")
        lines.append(f"Urgency (1-5): {t.get('urgency', 1)}")
        payload = t.get("payload") or {}
        if payload:
            lines.append(f"Trigger Details: {payload}")

    if b.get("customer"):
        cu = b["customer"]
        ident = cu.get("identity", {}) or {}
        lines.append("\n[CUSTOMER CONTEXT]")
        lines.append(f"Customer Name: {ident.get('name', 'N/A')}")
        lines.append(f"Language: {ident.get('language_pref', 'N/A')}")
        lines.append(f"Relationship State: {cu.get('state', 'N/A')}")
        rel = cu.get("relationship", {}) or {}
        if rel:
            lines.append(
                f"Last Visit: {rel.get('last_visit','N/A')} (Total Visits: {rel.get('visits_total',0)})"
            )
        if cu.get("preferences"):
            lines.append(f"Preferences: {cu['preferences']}")

    # Precomputed narrative features - judge-relevant derived facts the LLM
    # should cite instead of recomputing from raw numbers (percentile rank,
    # trend direction, retention gap, suggested offer, why-now window).
    try:
        feats = compute_features(b)
        block = format_features_block(feats)
        if block:
            lines.append(block)
    except Exception:  # never let feature-compute break composition
        pass

    return "\n".join(lines)
