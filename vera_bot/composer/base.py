"""EngagementComposer — prompt selection, LLM call, and post-validation."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from ..config import COMPOSER_VERSION
from .prompts import select_prompt
from .providers.base import LLMProvider
from .templates import try_template_compose
from .validators import (
    detect_auto_reply,
    detect_explicit_intent,
    detect_hard_no,
    is_repeat,
    validate_outbound,
)

logger = logging.getLogger(__name__)


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
        trigger = bundle.get("trigger") or {}
        kind = trigger.get("kind") or "default"
        is_customer_facing = bundle.get("customer") is not None
        category_slug = (bundle.get("category") or {}).get("slug") or (bundle.get("merchant") or {}).get("category_slug")

        # Mechanical trigger kinds (templates.py) bypass the LLM entirely.
        templated = try_template_compose(bundle)
        if templated is not None:
            templated.body, report = validate_outbound(templated.body)
            templated.flags.extend(report.flags)
            if prior_outbound and is_repeat(templated.body, prior_outbound):
                templated.flags.append("repeat_suppressed")
                templated.body = ""
                templated.rationale = f"[{COMPOSER_VERSION}] verbatim repeat suppressed"
            return templated

        system_prompt, header = select_prompt(
            kind, is_customer_facing, is_reply=False, category_slug=category_slug,
        )
        user_prompt = f"{header}\n\nCompose the next WhatsApp message.\n\n{formatted_context}"

        comp = self._run(system_prompt, user_prompt, fallback_send_as="merchant_on_behalf" if is_customer_facing else "vera")
        comp.send_as = comp.send_as or ("merchant_on_behalf" if is_customer_facing else "vera")
        if not comp.suppression_key:
            comp.suppression_key = f"{trigger.get('id', 'unknown')}_{kind}"

        comp.body, report = validate_outbound(comp.body)
        comp.flags.extend(report.flags)

        if prior_outbound and is_repeat(comp.body, prior_outbound):
            # Drop the action this tick rather than spend a second LLM call on
            # a rephrase; the trigger can re-fire in a later window.
            comp.flags.append("repeat_suppressed")
            comp.body = ""
            comp.rationale = f"[{COMPOSER_VERSION}] verbatim repeat suppressed"

        return comp

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
        category_slug = (bundle.get("category") or {}).get("slug") or (bundle.get("merchant") or {}).get("category_slug")

        system_prompt, header = select_prompt(
            kind, is_customer_facing, is_reply=True,
            has_chat_history=True, category_slug=category_slug,
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
        try:
            raw = self.provider.generate(system_prompt, user_prompt)
            data = _parse_json_response(raw)
        except Exception as e:
            logger.exception("LLM composition failed: %s", e)
            return self._fallback(is_reply=is_reply, fallback_send_as=fallback_send_as)

        comp = Composition(
            body=str(data.get("body") or ""),
            cta=str(data.get("cta") or "open_ended"),
            send_as=str(data.get("send_as") or fallback_send_as),
            suppression_key=str(data.get("suppression_key") or ""),
            rationale=str(data.get("rationale") or f"[{COMPOSER_VERSION}] (no rationale)"),
        )
        if is_reply:
            comp.action = str(data.get("action") or "send")
            if comp.action == "wait":
                try:
                    comp.wait_seconds = int(data.get("wait_seconds") or 60)
                except (TypeError, ValueError):
                    comp.wait_seconds = 60

        if not comp.rationale.startswith(f"[{COMPOSER_VERSION}]"):
            comp.rationale = f"[{COMPOSER_VERSION}] {comp.rationale}".strip()
        return comp

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
