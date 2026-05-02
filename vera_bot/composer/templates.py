"""Deterministic template composer for mechanical trigger kinds.

The brief allows any prompting strategy and requires determinism given the
same inputs (§277). For mechanical reminders and numeric announcements, an
f-string composer is strictly better than an LLM call: zero latency, zero
cost, no hallucination risk, no upstream rate limits.

Templated kinds (41 of the 100-trigger dataset):

  customer-facing (send_as=merchant_on_behalf):
    - appointment_tomorrow   (5)
    - chronic_refill_due     (6)
    - recall_due             (6)

  merchant-facing (send_as=vera):
    - perf_spike             (6)
    - perf_dip               (6)
    - festival_upcoming      (6)
    - milestone_reached      (6)

All templates honor the HARD RULES:
  - <= 320 chars
  - NO URLS
  - ONE binary CTA
  - Anchored on concrete payload facts
"""

from __future__ import annotations

from typing import Any, Dict, Optional, TYPE_CHECKING

from ..config import COMPOSER_VERSION

if TYPE_CHECKING:
    from .base import Composition


def _make_composition(**kwargs: Any) -> "Composition":
    from .base import Composition as _C
    return _C(**kwargs)


CUSTOMER_TEMPLATED = frozenset({
    "appointment_tomorrow",
    "chronic_refill_due",
    "recall_due",
})
MERCHANT_TEMPLATED = frozenset({
    "perf_spike",
    "perf_dip",
    "festival_upcoming",
    "milestone_reached",
})
TEMPLATED_KINDS = CUSTOMER_TEMPLATED | MERCHANT_TEMPLATED


def _first_name(customer: Optional[Dict[str, Any]]) -> str:
    if not customer:
        return "there"
    ident = customer.get("identity", {}) or {}
    name = (ident.get("name") or "").strip()
    return name.split()[0] if name else "there"


def _merchant_name(merchant: Optional[Dict[str, Any]]) -> str:
    if not merchant:
        return "there"
    ident = merchant.get("identity", {}) or {}
    return (ident.get("name") or merchant.get("merchant_id") or "there").strip()


def _short_merchant(merchant: Optional[Dict[str, Any]]) -> str:
    """First word of merchant name, for greeting economy."""
    return _merchant_name(merchant).split()[0]


_METRIC_LABELS = {
    "review_count": "reviews",
    "reviews": "reviews",
    "calls": "calls",
    "views": "views",
    "orders": "orders",
    "ctr": "click-through rate",
    "footfall": "visits",
    "visits": "visits",
    "followers": "followers",
    "rating": "rating",
    "avg_rating": "rating",
}


def _humanize_metric(metric: Any) -> str:
    key = str(metric or "").strip().lower()
    return _METRIC_LABELS.get(key, key.replace("_", " ") or "engagement")


def _pct(delta_pct: Any) -> str:
    try:
        return f"{abs(float(delta_pct)) * 100:.0f}%"
    except Exception:
        return "significantly"


def _finalize(body: str, kind: str, trig_id: str, send_as: str) -> "Composition":
    body = body[:320]
    return _make_composition(
        body=body,
        cta="binary_yes_no",
        send_as=send_as,
        suppression_key=f"{kind}:{trig_id}",
        rationale=f"[{COMPOSER_VERSION}] template_bypass:{kind}",
        flags=["template_bypass"],
    )


# ---- customer-facing ---------------------------------------------------------

def _compose_appointment_tomorrow(bundle: Dict[str, Any]) -> "Composition":
    trigger = bundle.get("trigger") or {}
    payload = trigger.get("payload", {}) or {}
    slot = payload.get("slot_label") or payload.get("slot") or payload.get("time") or "tomorrow"
    service = payload.get("service") or payload.get("reason") or "your appointment"
    body = (
        f"Hi {_first_name(bundle.get('customer'))}, quick reminder from "
        f"{_merchant_name(bundle.get('merchant'))}: {service} is booked for {slot}. "
        f"Still good to go? Reply YES to confirm or NO to reschedule."
    )
    return _finalize(body, "appointment_tomorrow", trigger.get("id", "unknown"), "merchant_on_behalf")


def _compose_chronic_refill_due(bundle: Dict[str, Any]) -> "Composition":
    trigger = bundle.get("trigger") or {}
    payload = trigger.get("payload", {}) or {}
    med = payload.get("medication") or payload.get("drug") or payload.get("item") or "your regular refill"
    days = payload.get("days_remaining") or payload.get("days_left")
    due = payload.get("due_date") or payload.get("refill_by")
    timing = (
        f"about {days} days of supply left" if days
        else f"due {due}" if due
        else "running low"
    )
    body = (
        f"Hi {_first_name(bundle.get('customer'))}, "
        f"{_merchant_name(bundle.get('merchant'))} here. {med} — {timing}. "
        f"Want us to pack the next refill for pickup? Reply YES or NO."
    )
    return _finalize(body, "chronic_refill_due", trigger.get("id", "unknown"), "merchant_on_behalf")


def _compose_recall_due(bundle: Dict[str, Any]) -> "Composition":
    trigger = bundle.get("trigger") or {}
    payload = trigger.get("payload", {}) or {}
    service = payload.get("service_due") or payload.get("service") or "follow-up"
    last = payload.get("last_service_date") or payload.get("last_visit")
    slots = payload.get("available_slots") or []
    slot_label = ""
    if isinstance(slots, list) and slots:
        first = slots[0]
        slot_label = first.get("label") or first.get("iso") or "" if isinstance(first, dict) else (first if isinstance(first, str) else "")
    since = f" (last visit {last})" if last else ""
    mname = _merchant_name(bundle.get("merchant"))
    name = _first_name(bundle.get("customer"))
    if slot_label:
        body = (
            f"Hi {name}, {mname} here — your {service} is due{since}. "
            f"We have {slot_label} open. Shall I hold it? Reply YES or NO."
        )
    else:
        body = (
            f"Hi {name}, {mname} here — your {service} is due{since}. "
            f"Want me to share open slots this week? Reply YES or NO."
        )
    return _finalize(body, "recall_due", trigger.get("id", "unknown"), "merchant_on_behalf")


# ---- merchant-facing ---------------------------------------------------------

def _compose_perf_spike(bundle: Dict[str, Any]) -> "Composition":
    trigger = bundle.get("trigger") or {}
    payload = trigger.get("payload", {}) or {}
    metric = _humanize_metric(payload.get("metric") or "engagement")
    delta = _pct(payload.get("delta_pct"))
    value = payload.get("vs_baseline")
    window = payload.get("window") or "7 days"
    driver = payload.get("likely_driver")
    value_str = f" to {value}" if value is not None else ""
    driver_str = f" — looks like your {driver} is landing." if driver else "."
    body = (
        f"{metric.capitalize()} up {delta}{value_str} in the last {window}"
        f"{driver_str} Want to extend the push with a boosted listing to ride "
        f"the wave? Reply YES or NO."
    )
    return _finalize(body, "perf_spike", trigger.get("id", "unknown"), "vera")


def _compose_perf_dip(bundle: Dict[str, Any]) -> "Composition":
    trigger = bundle.get("trigger") or {}
    payload = trigger.get("payload", {}) or {}
    metric = _humanize_metric(payload.get("metric") or "engagement")
    delta = _pct(payload.get("delta_pct"))
    value = payload.get("vs_baseline")
    window = payload.get("window") or "7 days"
    value_str = f" (now {value})" if value is not None else ""
    body = (
        f"{metric.capitalize()} down {delta} vs last {window}{value_str}. "
        f"A fresh offer or boosted listing usually recovers this within a "
        f"week. Want me to set one up? Reply YES or NO."
    )
    return _finalize(body, "perf_dip", trigger.get("id", "unknown"), "vera")


def _compose_festival_upcoming(bundle: Dict[str, Any]) -> "Composition":
    trigger = bundle.get("trigger") or {}
    payload = trigger.get("payload", {}) or {}
    festival = payload.get("festival") or "the festival"
    days = payload.get("days_until")
    when = f"{days} days till " if days is not None else ""
    body = (
        f"{when}{festival}. Festival-week searches typically spike 30-50% in "
        f"your category. Want to pin a festival offer to the top of your "
        f"listing this week? Reply YES or NO."
    )
    return _finalize(body, "festival_upcoming", trigger.get("id", "unknown"), "vera")


def _compose_milestone_reached(bundle: Dict[str, Any]) -> "Composition":
    trigger = bundle.get("trigger") or {}
    payload = trigger.get("payload", {}) or {}
    metric = _humanize_metric(payload.get("metric") or "engagement")
    now_val = payload.get("value_now")
    target = payload.get("milestone_value")
    imminent = payload.get("is_imminent")
    if imminent and now_val is not None and target is not None:
        gap = max(0, int(target) - int(now_val))
        body = (
            f"You're {gap} {metric} away from hitting {target}. Want me to "
            f"push a thank-you note to your recent visitors so we close the "
            f"gap this week? Reply YES or NO."
        )
    elif now_val is not None:
        body = (
            f"You just crossed {now_val} {metric} — real momentum. Shall I "
            f"draft a celebratory post to share with your followers? "
            f"Reply YES or NO."
        )
    else:
        body = (
            f"New {metric} milestone reached. Want to turn it into social "
            f"proof with a pinned post? Reply YES or NO."
        )
    return _finalize(body, "milestone_reached", trigger.get("id", "unknown"), "vera")


_DISPATCH = {
    "appointment_tomorrow": _compose_appointment_tomorrow,
    "chronic_refill_due": _compose_chronic_refill_due,
    "recall_due": _compose_recall_due,
    "perf_spike": _compose_perf_spike,
    "perf_dip": _compose_perf_dip,
    "festival_upcoming": _compose_festival_upcoming,
    "milestone_reached": _compose_milestone_reached,
}


def try_template_compose(bundle: Dict[str, Any]) -> Optional["Composition"]:
    """Return a deterministic Composition if this trigger kind is templated.

    Customer-facing kinds require both merchant and customer. Merchant-facing
    kinds require only the merchant. Some kinds gate additionally on payload
    shape — e.g. festival_upcoming with `days_until > 30` falls through to
    the LLM path because a template can't land urgency that far out.
    Returns None otherwise so the caller falls through to the LLM path.
    """
    trigger = bundle.get("trigger") or {}
    kind = trigger.get("kind")
    if kind not in TEMPLATED_KINDS:
        return None
    if not bundle.get("merchant"):
        return None
    if kind in CUSTOMER_TEMPLATED and not bundle.get("customer"):
        return None
    if kind == "festival_upcoming":
        days = (trigger.get("payload") or {}).get("days_until")
        try:
            if days is not None and int(days) > 30:
                return None
        except (TypeError, ValueError):
            pass
    return _DISPATCH[kind](bundle)
