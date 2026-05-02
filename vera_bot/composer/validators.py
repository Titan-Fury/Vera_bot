"""Post-LLM validators.

Each returns (possibly modified) value plus a list of applied flags suitable
for logging/audit. Validators never raise on user-facing content - they coerce
to a spec-compliant shape.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable, List, Tuple

from ..config import MAX_BODY_CHARS

# Very permissive URL regex - catches raw domains, http(s), and www forms.
_URL_RE = re.compile(
    r"""\b(
        (?:https?://|www\.)\S+
        |
        [A-Za-z0-9][A-Za-z0-9\-]{0,62}
        (?:\.(?:com|in|co|org|net|io|ai|app|store|shop|biz|info|me|ly))
        (?:/\S*)?
    )\b""",
    re.IGNORECASE | re.VERBOSE,
)

_SENTENCE_BREAK = re.compile(r"(?<=[.!?।])\s+")

_HARD_NO_PATTERNS = [
    r"\bstop\b",
    r"\bunsubscribe\b",
    r"\bnot\s+interested\b",
    r"\bdo\s*n['o]?t\s+(?:message|contact|call)\b",
    r"\bleave\s+me\s+alone\b",
    r"बंद\s*कर",
    r"परेशान",
    r"मत\s+भेज",
    r"\bremove\s+me\b",
]

_INTENT_YES_PATTERNS = [
    r"\byes\b",
    r"\bok(?:ay)?\b",
    r"\bsure\b",
    r"\bgo\s+ahead\b",
    r"\blet['s]*\s+do\s+it\b",
    r"\bsounds?\s+good\b",
    r"हाँ",
    r"हां",
    r"ज़रूर",
    r"चलो",
    r"करो",
    r"\bshuru\s+kar",
    r"\bstart\b",
]

_AUTO_REPLY_PHRASES = [
    "thank you for contacting",
    "out of office",
    "currently unavailable",
    "automated response",
    "automated reply",
    "this is an automated",
    "away from desk",
    "will get back to you",
    "team tak pahuncha",
]


@dataclass
class ValidationReport:
    flags: List[str] = field(default_factory=list)

    def add(self, flag: str) -> None:
        self.flags.append(flag)


def strip_urls(body: str) -> Tuple[str, ValidationReport]:
    r = ValidationReport()
    new = _URL_RE.sub("", body)
    if new != body:
        r.add("urls_stripped")
    # Collapse leftover double spaces from stripping.
    new = re.sub(r"\s{2,}", " ", new).strip()
    return new, r


def enforce_length(body: str, cap: int = MAX_BODY_CHARS) -> Tuple[str, ValidationReport]:
    r = ValidationReport()
    if len(body) <= cap:
        return body, r
    r.add("truncated")
    # Try sentence-boundary truncation first.
    sentences = _SENTENCE_BREAK.split(body)
    out = ""
    for s in sentences:
        if len(out) + len(s) + 1 > cap:
            break
        out = (out + " " + s).strip() if out else s
    if not out:
        out = body[: cap - 1].rstrip() + "…"
    if len(out) > cap:
        out = out[: cap - 1].rstrip() + "…"
    return out, r


def validate_outbound(body: str) -> Tuple[str, ValidationReport]:
    """Full pipeline: strip URLs, then enforce length."""
    report = ValidationReport()
    body, r1 = strip_urls(body)
    report.flags.extend(r1.flags)
    body, r2 = enforce_length(body)
    report.flags.extend(r2.flags)
    if not body.strip():
        report.add("empty_body")
    return body, report


def is_repeat(body: str, prior_outbound: Iterable[str]) -> bool:
    needle = body.strip().lower()
    if not needle:
        return False
    return any(needle == (p or "").strip().lower() for p in prior_outbound)


def detect_hard_no(message: str) -> bool:
    if not message:
        return False
    m = message.lower()
    return any(re.search(p, m) for p in _HARD_NO_PATTERNS)


def detect_explicit_intent(message: str) -> bool:
    if not message:
        return False
    m = message.lower()
    return any(re.search(p, m) for p in _INTENT_YES_PATTERNS)


def detect_auto_reply(user_messages: List[str]) -> bool:
    """Return True if we detect an auto-responder pattern in recent messages."""
    if not user_messages:
        return False
    # 1) 3x verbatim
    if len(user_messages) >= 3 and user_messages[-1] == user_messages[-2] == user_messages[-3]:
        return True
    # 2) canned phrase in the latest message
    latest = user_messages[-1].lower()
    if any(p in latest for p in _AUTO_REPLY_PHRASES):
        return True
    return False
