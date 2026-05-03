"""EngagementComposer — prompt selection, LLM call, and post-validation."""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ValidationError, field_validator

from ..config import (
    COMPOSER_VERSION,
    SELF_CRITIQUE_ENABLED,
    SELF_CRITIQUE_MAX_COMPOSE_ELAPSED_S,
    SELF_CRITIQUE_URGENCY_THRESHOLD,
)
from .critic import (
    budget_deadline as _critic_budget_deadline,
    build_revision_prompt as _build_revision_prompt,
    critique as _critique,
    min_axis_threshold as _critic_min_axis,
    should_critique as _should_critique,
)
from .metrics import METRICS
from .exemplars import format_exemplar_block, get_exemplar
from .prompts import select_prompt
from .providers.base import LLMProvider
from .templates import try_template_compose
from .validators import (
    detect_auto_reply,
    detect_explicit_intent,
    detect_hard_no,
    ensure_offer_in_catalog,
    enforce_taboos,
    has_payload_anchor,
    is_repeat,
    validate_outbound,
)

logger = logging.getLogger(__name__)
_audit_logger = logging.getLogger("vera_bot.audit")


@dataclass
class Composition:
    body: str = ""
    cta: str = "open_ended"
    send_as: str = "vera"
    suppression_key: str = ""
    rationale: str = ""
    action: Optional[str] = None  # set on replies
    wait_seconds: Optional[int] = None
    flags: List[str] = field(default_factory=list)

    def to_outbound(self) -> Dict[str, Any]:
        return {
            "body": self.body,
            "cta": self.cta,
            "send_as": self.send_as,
            "suppression_key": self.suppression_key,
            "rationale": self.rationale,
        }

    def to_reply(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {"action": self.action or "send", "rationale": self.rationale}
        if out["action"] == "send":
            out["body"] = self.body
            out["cta"] = self.cta
        elif out["action"] == "wait":
            out["wait_seconds"] = int(self.wait_seconds or 60)
        return out


def _strip_code_fence(raw: str) -> str:
    s = raw.strip()
    if s.startswith("```"):
        first_nl = s.find("\n")
        if first_nl != -1:
            s = s[first_nl + 1 :]
        if s.endswith("```"):
            s = s[: -3]
    return s.strip()


def _parse_json_response(raw: str) -> Dict[str, Any]:
    try:
        return json.loads(_strip_code_fence(raw))
    except json.JSONDecodeError:
        # Some providers wrap JSON in prose; fall back to the first {...} span.
        s = _strip_code_fence(raw)
        start = s.find("{")
        end = s.rfind("}")
        if start != -1 and end != -1 and end > start:
            return json.loads(s[start : end + 1])
        raise


_ALLOWED_CTA = {"open_ended", "binary_yes_no", "none"}
_ALLOWED_SEND_AS = {"vera", "merchant_on_behalf"}
_ALLOWED_ACTION = {"send", "wait", "end"}


class CompositionJSON(BaseModel):
    """Schema enforced on LLM output. One-shot retry if invalid."""

    body: str = ""
    cta: str = "open_ended"
    send_as: Optional[str] = None
    suppression_key: str = ""
    rationale: str = ""
    action: Optional[str] = None
    wait_seconds: Optional[int] = None

    @field_validator("cta")
    @classmethod
    def _cta_must_be_allowed(cls, v: str) -> str:
        if v not in _ALLOWED_CTA:
            return "open_ended"
        return v

    @field_validator("send_as")
    @classmethod
    def _send_as_allowed(cls, v: Optional[str]) -> Optional[str]:
        if v is None or v in _ALLOWED_SEND_AS:
            return v
        return None

    @field_validator("action")
    @classmethod
    def _action_allowed(cls, v: Optional[str]) -> Optional[str]:
        if v is None or v in _ALLOWED_ACTION:
            return v
        return "send"


_RETRY_SCHEMA_HINT = (
    "Previous output was malformed. Return ONLY valid JSON matching this "
    "schema and no prose: "
    '{"body": "<= 320 chars", "cta": "open_ended|binary_yes_no|none", '
    '"send_as": "vera|merchant_on_behalf", "suppression_key": "...", '
    '"rationale": "..."'
    ' , "action": "send|wait|end (replies only)", "wait_seconds": 60}'
)


class EngagementComposer:
    def __init__(self, provider: LLMProvider) -> None:
        self.provider = provider

    # ---- Proactive composition (/v1/tick) ------------------------------------

    def compose(
        self,
        bundle: Dict[str, Any],
        formatted_context: str,
        prior_outbound: Optional[List[str]] = None,
    ) -> Composition:
        t_start = time.perf_counter()
        trigger = bundle.get("trigger") or {}
        kind = trigger.get("kind") or "default"
        is_customer_facing = bundle.get("customer") is not None
        category = bundle.get("category") or {}
        merchant = bundle.get("merchant") or {}
        customer = bundle.get("customer") or {}
        category_slug = category.get("slug") or merchant.get("category_slug")
        languages = self._resolve_languages(merchant, customer)
        trigger_id = trigger.get("id") or "unknown"

        # Mechanical trigger kinds (templates.py) bypass the LLM entirely.
        templated = try_template_compose(bundle)
        if templated is not None:
            templated.body, report = validate_outbound(templated.body)
            templated.flags.extend(report.flags)
            if prior_outbound and is_repeat(templated.body, prior_outbound):
                templated.flags.append("repeat_suppressed")
                templated.body = ""
                templated.rationale = f"[{COMPOSER_VERSION}] verbatim repeat suppressed"
            self._audit(trigger_id, kind, "template", None, templated, t_start)
            METRICS.record_compose(kind=kind, path="template", provider=None,
                                   latency_ms=(time.perf_counter()-t_start)*1000, body=templated.body)
            return templated

        system_prompt, header = select_prompt(
            kind, is_customer_facing, is_reply=False, category_slug=category_slug,
            languages=languages,
        )
        exemplar_body = get_exemplar(kind, is_customer_facing)
        exemplar_block = format_exemplar_block(exemplar_body) if exemplar_body else ""
        user_prompt = (
            f"{header}\n\nCompose the next WhatsApp message."
            f"{exemplar_block}\n\n{formatted_context}"
        )

        comp = self._run(system_prompt, user_prompt,
                         fallback_send_as="merchant_on_behalf" if is_customer_facing else "vera")
        comp.send_as = comp.send_as or ("merchant_on_behalf" if is_customer_facing else "vera")
        if not comp.suppression_key:
            comp.suppression_key = f"{trigger_id}_{kind}"

        # Self-critique gate: on high-urgency triggers only, score the draft
        # against the challenge's own 5-axis rubric and trigger a bounded
        # one-shot revision if any axis is weak. Strictly additive — any
        # failure path returns the original draft untouched.
        urgency = trigger.get("urgency")
        if (
            SELF_CRITIQUE_ENABLED
            and comp.body
            and "fallback" not in comp.flags
            and _should_critique(urgency, SELF_CRITIQUE_URGENCY_THRESHOLD)
        ):
            self._maybe_revise(
                comp=comp,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                formatted_context=formatted_context,
                trigger_kind=kind,
                t_compose_start=t_start,
            )

        comp.body, report = validate_outbound(comp.body)
        comp.flags.extend(report.flags)

        # Offer-catalog grounding guard (brief §5.8 "Don't fabricate").
        comp.body, offer_report = ensure_offer_in_catalog(
            comp.body,
            category.get("offer_catalog"),
            merchant.get("offers"),
        )
        comp.flags.extend(offer_report.flags)

        # Category-voice taboo enforcement (e.g. "cure"/"guaranteed" for dentists).
        voice = (category.get("voice") or {})
        comp.body, taboo_report = enforce_taboos(comp.body, voice.get("taboos"))
        comp.flags.extend(taboo_report.flags)

        # Payload-fact anchoring signal (audit only — we don't coerce).
        if comp.body and not has_payload_anchor(comp.body, bundle):
            comp.flags.append("no_payload_anchor")

        if prior_outbound and is_repeat(comp.body, prior_outbound):
            # Drop the action this tick rather than spend a second LLM call on
            # a rephrase; the trigger can re-fire in a later window.
            comp.flags.append("repeat_suppressed")
            comp.body = ""
            comp.rationale = f"[{COMPOSER_VERSION}] verbatim repeat suppressed"

        path = "fallback" if "fallback" in comp.flags else "llm"
        self._audit(trigger_id, kind, path, getattr(self.provider, "name", None), comp, t_start)
        METRICS.record_compose(kind=kind, path=path,
                               provider=getattr(self.provider, "name", None),
                               latency_ms=(time.perf_counter()-t_start)*1000,
                               body=comp.body)
        return comp

    @staticmethod
    def _resolve_languages(merchant: Dict[str, Any], customer: Dict[str, Any]) -> List[str]:
        langs: List[str] = []
        ident = (merchant or {}).get("identity") or {}
        for l in ident.get("languages") or []:
            if l:
                langs.append(str(l))
        if customer:
            cident = customer.get("identity") or {}
            pref = cident.get("language_pref")
            if pref:
                langs.append(str(pref))
        return langs

    @staticmethod
    def _audit(trigger_id: str, kind: str, path: str, provider: Optional[str],
               comp: "Composition", t_start: float) -> None:
        record = {
            "event": "compose",
            "trigger_id": trigger_id,
            "kind": kind,
            "path": path,
            "provider": provider,
            "latency_ms": int((time.perf_counter() - t_start) * 1000),
            "body_len": len(comp.body or ""),
            "flags": list(comp.flags or []),
            "cta": comp.cta,
            "send_as": comp.send_as,
        }
        try:
            _audit_logger.info(json.dumps(record, ensure_ascii=False))
        except Exception:  # pragma: no cover — never let audit break composition
            pass

    # ---- Reply (/v1/reply) ---------------------------------------------------

    def compose_reply(
        self,
        bundle: Dict[str, Any],
        formatted_context: str,
        chat_history: List[Dict[str, str]],
        inbound_message: str,
        turn_number: int,
    ) -> Composition:
        user_msgs = [t["msg"] for t in chat_history if t.get("from") not in {"vera", "merchant_on_behalf"}]

        # Decision tree (see docs/COMPLIANCE.md).
        if detect_auto_reply(user_msgs):
            return Composition(action="end", rationale=f"[{COMPOSER_VERSION}] auto-reply detected; warm exit")

        if detect_hard_no(inbound_message):
            return Composition(action="end", rationale=f"[{COMPOSER_VERSION}] explicit opt-out; warm exit")

        if turn_number >= 6:
            return Composition(action="end", rationale=f"[{COMPOSER_VERSION}] turn cap reached without engagement")

        trigger = bundle.get("trigger") or {}
        kind = trigger.get("kind") or "default"
        is_customer_facing = bundle.get("customer") is not None
        intent_locked = detect_explicit_intent(inbound_message)
        category = bundle.get("category") or {}
        merchant = bundle.get("merchant") or {}
        customer = bundle.get("customer") or {}
        category_slug = category.get("slug") or merchant.get("category_slug")
        languages = self._resolve_languages(merchant, customer)

        system_prompt, header = select_prompt(
            kind, is_customer_facing, is_reply=True,
            has_chat_history=True, category_slug=category_slug,
            languages=languages,
        )
        history_str = "\n[CHAT HISTORY]\n" + "\n".join(
            f"{t.get('from','?')}: {t.get('msg','')}" for t in chat_history
        )
        intent_note = (
            "\n[INTENT LOCK] Counterpart explicitly accepted. Move to action/confirmation mode."
            if intent_locked
            else ""
        )
        user_prompt = f"{header}\n\n{formatted_context}{history_str}\nLast inbound: {inbound_message}{intent_note}"

        comp = self._run(system_prompt, user_prompt, is_reply=True, fallback_send_as="vera")

        if comp.action == "send":
            comp.body, report = validate_outbound(comp.body)
            comp.flags.extend(report.flags)
            prior_outbound = [t["msg"] for t in chat_history if t.get("from") in {"vera", "merchant_on_behalf"}]
            if is_repeat(comp.body, prior_outbound):
                comp.flags.append("repeat_blocked")
                comp.action = "end"
                comp.rationale = f"[{COMPOSER_VERSION}] avoided verbatim repeat; ending"
        elif comp.action == "wait":
            comp.wait_seconds = max(30, min(int(comp.wait_seconds or 60), 600))

        return comp

    # ---- Internals -----------------------------------------------------------

    def _run(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        is_reply: bool = False,
        fallback_send_as: str = "vera",
    ) -> Composition:
        t_start = time.perf_counter()
        data = self._call_and_validate(system_prompt, user_prompt, t_start)
        if data is None:
            return self._fallback(is_reply=is_reply, fallback_send_as=fallback_send_as)

        comp = Composition(
            body=data.body or "",
            cta=data.cta or "open_ended",
            send_as=data.send_as or fallback_send_as,
            suppression_key=data.suppression_key or "",
            rationale=data.rationale or f"[{COMPOSER_VERSION}] (no rationale)",
        )
        if is_reply:
            comp.action = data.action or "send"
            if comp.action == "wait":
                try:
                    comp.wait_seconds = int(data.wait_seconds or 60)
                except (TypeError, ValueError):
                    comp.wait_seconds = 60

        if not comp.rationale.startswith(f"[{COMPOSER_VERSION}]"):
            comp.rationale = f"[{COMPOSER_VERSION}] {comp.rationale}".strip()
        return comp

    # Budget gate for the one-shot retry — keeps us inside the /v1/tick 10s
    # timeout even when the first generate() burned most of the window.
    _RETRY_MIN_BUDGET_S = 2.5
    # A compose() call has ~8s realistic budget (tick=10s minus housekeeping).
    # Anything slower than this and we should give up, not retry.
    _RETRY_SOFT_DEADLINE_S = 8.0

    def _call_and_validate(
        self, system_prompt: str, user_prompt: str, t_start: float,
    ) -> Optional[CompositionJSON]:
        try:
            raw = self.provider.generate(system_prompt, user_prompt)
            return CompositionJSON(**_parse_json_response(raw))
        except (json.JSONDecodeError, ValidationError) as e:
            elapsed = time.perf_counter() - t_start
            remaining = self._RETRY_SOFT_DEADLINE_S - elapsed
            if remaining < self._RETRY_MIN_BUDGET_S:
                logger.info(
                    "LLM JSON invalid and no budget for retry "
                    "(elapsed=%.2fs, need>=%.2fs): %s",
                    elapsed, self._RETRY_MIN_BUDGET_S, str(e)[:200],
                )
                return None
            try:
                retry_prompt = f"{user_prompt}\n\n[RETRY] {_RETRY_SCHEMA_HINT}"
                raw = self.provider.generate(system_prompt, retry_prompt)
                return CompositionJSON(**_parse_json_response(raw))
            except Exception as retry_err:
                logger.warning(
                    "LLM retry also failed (%s); falling back",
                    type(retry_err).__name__,
                )
                return None
        except Exception as e:
            logger.exception("LLM composition failed: %s", e)
            return None

    def _fallback(self, *, is_reply: bool, fallback_send_as: str) -> Composition:
        if is_reply:
            return Composition(
                action="send",
                body="Got it — noting this and coming back shortly.",
                cta="open_ended",
                rationale=f"[{COMPOSER_VERSION}] fallback reply (provider error)",
                flags=["fallback"],
            )
        return Composition(
            body="Quick note: I have an update tailored to your account. Ok to share?",
            cta="binary_yes_no",
            send_as=fallback_send_as,
            suppression_key="fallback",
            rationale=f"[{COMPOSER_VERSION}] fallback composition (provider error)",
            flags=["fallback"],
        )

    # ---- Self-critique revision (bounded, opt-in via config) ----------------

    def _maybe_revise(
        self,
        *,
        comp: Composition,
        system_prompt: str,
        user_prompt: str,
        formatted_context: str,
        trigger_kind: str,
        t_compose_start: float,
    ) -> None:
        """Score draft vs rubric; rewrite once if any axis is weak. Never regresses."""
        # Hard gate: if initial compose already burned most of our budget, skip
        # critique entirely. The outer /v1/tick wait_for is 9s; a second LLM
        # call on top of a 5s first compose will regularly trip it.
        elapsed_already = time.perf_counter() - t_compose_start
        if elapsed_already >= SELF_CRITIQUE_MAX_COMPOSE_ELAPSED_S:
            comp.flags.append(f"critique_skipped_slow_compose({elapsed_already:.1f}s)")
            return

        t_critic_start = time.perf_counter()
        deadline = _critic_budget_deadline(t_critic_start)

        draft = comp.body
        result = _critique(
            provider=self.provider,
            body=draft,
            formatted_context=formatted_context,
            trigger_kind=trigger_kind,
            deadline_s=deadline,
        )
        if result is None:
            comp.flags.append("critique_skipped")
            return

        min_score = result.min_score
        comp.flags.append(f"critique_min={min_score}")

        if min_score >= _critic_min_axis():
            # Draft is already strong enough.
            return

        # Budget check before spending another LLM call.
        if time.perf_counter() >= deadline:
            comp.flags.append("revision_skipped_budget")
            return

        revision_prompt = _build_revision_prompt(user_prompt, result, draft)
        try:
            revised = self._call_and_validate(system_prompt, revision_prompt, t_compose_start)
        except Exception as e:  # pragma: no cover — defensive
            logger.info("revision: provider error (%s)", type(e).__name__)
            comp.flags.append("revision_error")
            return

        if revised is None or not (revised.body or "").strip():
            comp.flags.append("revision_empty")
            return

        # Only accept revision if it looks like a real rewrite (not identical
        # or near-identical to the draft) to avoid swapping body with itself.
        if revised.body.strip() == draft.strip():
            comp.flags.append("revision_identical")
            return

        comp.body = revised.body
        if revised.cta:
            comp.cta = revised.cta
        if revised.send_as:
            comp.send_as = revised.send_as
        comp.flags.append("revised")
