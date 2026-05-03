"""Post-LLM validators.

Each returns (possibly modified) value plus a list of applied flags suitable
for logging/audit. Validators never raise on user-facing content - they coerce
to a spec-compliant shape.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Any, Dict, Iterable, List, Sequence, Tuple

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


# Empty quote / placeholder artifacts the LLM sometimes emits when it sees a
# template slot in the prompt but can't fill it. Dropping these is safer than
# leaving "Offer '' now" in a customer-facing body.
_EMPTY_QUOTES_RE = re.compile(r"""\s['"‘’“”]\s*['"‘’“”]\s*""")
_PLACEHOLDER_RE = re.compile(r"\{[a-zA-Z_][a-zA-Z0-9_]*\}")


def strip_empty_artifacts(body: str) -> Tuple[str, ValidationReport]:
    """Remove empty ''/""/{var} placeholder artifacts and collapse whitespace."""
    r = ValidationReport()
    new = _EMPTY_QUOTES_RE.sub(" ", body)
    if new != body:
        r.add("stripped_empty_quotes")
    if _PLACEHOLDER_RE.search(new):
        new = _PLACEHOLDER_RE.sub("", new)
        r.add("stripped_placeholder")
    new = re.sub(r"\s{2,}", " ", new).strip()
    return new, r


def validate_outbound(body: str) -> Tuple[str, ValidationReport]:
    """Full pipeline: strip URLs, clean placeholders, then enforce length."""
    report = ValidationReport()
    body, r0 = strip_empty_artifacts(body)
    report.flags.extend(r0.flags)
    body, r1 = strip_urls(body)
    report.flags.extend(r1.flags)
    body, r2 = enforce_length(body)
    report.flags.extend(r2.flags)
    if not body.strip():
        report.add("empty_body")
    return body, report


def is_repeat(body: str, prior_outbound: Iterable[str], similarity_threshold: float = 0.92) -> bool:
    """Detect verbatim OR near-duplicate repeats.

    Brief §10 penalises "same body verbatim" but the judge's LLM may rephrase
    in subtle ways. We flag above `similarity_threshold` Levenshtein-ratio
    normalised similarity to catch "rephrased repeats" without a second LLM
    call.
    """
    needle = _norm_for_compare(body)
    if not needle:
        return False
    for p in prior_outbound:
        cand = _norm_for_compare(p or "")
        if not cand:
            continue
        if needle == cand:
            return True
        # SequenceMatcher ratio is a good proxy for normalised Levenshtein on
        # short WhatsApp bodies (<320 chars).
        if SequenceMatcher(None, needle, cand).ratio() >= similarity_threshold:
            return True
    return False


_WHITESPACE_RE = re.compile(r"\s+")
_PUNCT_RE = re.compile(r"[^\w\s]")


def _norm_for_compare(s: str) -> str:
    if not s:
        return ""
    s = _PUNCT_RE.sub(" ", s.lower())
    return _WHITESPACE_RE.sub(" ", s).strip()


# ---- offer-catalog grounding --------------------------------------------------

# Service+price patterns like "Dental Cleaning @ ₹299" or "Haircut @ ₹99".
_OFFER_LIKE_RE = re.compile(
    r"[A-Za-z][A-Za-z \-/&+]{2,40}\s*@\s*₹\s*[\d,]+",
    re.UNICODE,
)


def ensure_offer_in_catalog(
    body: str,
    offer_catalog: Sequence[Dict[str, Any]] | None,
    merchant_offers: Sequence[Dict[str, Any]] | None = None,
) -> Tuple[str, ValidationReport]:
    """Strip invented "Service @ ₹price" phrases not found in catalog or merchant offers.

    Brief §5.8: "Don't fabricate — no fake offers". Protects specificity
    + merchant-fit scores. Leaves pure money mentions ("₹299") alone; only
    challenges full Service+Price patterns.
    """
    r = ValidationReport()
    if not body:
        return body, r

    allowed_titles: set[str] = set()
    for item in list(offer_catalog or []) + list(merchant_offers or []):
        if not isinstance(item, dict):
            continue
        title = (item.get("title") or "").strip()
        if title:
            allowed_titles.add(_norm_for_compare(title))

    if not allowed_titles:
        # No catalog context — can't validate; trust the LLM.
        return body, r

    matches = _OFFER_LIKE_RE.findall(body)
    if not matches:
        return body, r

    invented: List[str] = []
    for m in matches:
        norm = _norm_for_compare(m)
        # A proposed offer is "grounded" if its normalized form appears as a
        # substring of any catalog title or vice-versa (tolerates tweaks like
        # missing articles).
        grounded = any(norm == t or norm in t or t in norm for t in allowed_titles)
        if not grounded:
            invented.append(m)

    if not invented:
        return body, r

    r.add("invented_offer_dropped")
    out = body
    for inv in invented:
        out = out.replace(inv, "")
    out = re.sub(r"\s{2,}", " ", out).strip(" ,.—-")
    # If we gutted the body, surface a canned ask instead.
    if len(out) < 30:
        out = "Want me to set up a matching offer from your catalog? Reply YES or NO."
        r.add("invented_offer_fallback")
    return out, r


# ---- taboo enforcement --------------------------------------------------------


def enforce_taboos(body: str, taboos: Iterable[str] | None) -> Tuple[str, ValidationReport]:
    """Strip category-voice taboos (e.g. "cure", "guaranteed" for dentists)."""
    r = ValidationReport()
    if not body or not taboos:
        return body, r
    out = body
    hits: List[str] = []
    for tab in taboos:
        if not tab:
            continue
        pattern = re.compile(r"\b" + re.escape(str(tab)) + r"\b", re.IGNORECASE)
        if pattern.search(out):
            out = pattern.sub("", out)
            hits.append(str(tab))
    if hits:
        out = re.sub(r"\s{2,}", " ", out).strip(" ,.—-")
        r.add(f"taboos_stripped:{','.join(sorted(set(hits)))}")
    return out, r


# ---- payload-fact citation check ---------------------------------------------

_NUMBERS_RE = re.compile(r"(?:₹\s?)?\b\d+(?:[,.]\d+)?%?\b")
_CAP_TOKEN_RE = re.compile(r"\b[A-Z][A-Za-z0-9]{2,}\b")  # named entities / acronyms


def _extract_anchor_tokens(value: Any, out: set[str]) -> None:
    """Recursively extract numeric + capitalised-word tokens from context values."""
    if value is None:
        return
    if isinstance(value, dict):
        for v in value.values():
            _extract_anchor_tokens(v, out)
        return
    if isinstance(value, (list, tuple, set)):
        for v in value:
            _extract_anchor_tokens(v, out)
        return
    # Floats between 0 and 1 are usually percentage ratios (delta_pct=0.28).
    # Add the "28" form so the LLM's natural "up 28%" phrasing matches.
    if isinstance(value, float) and 0.0 < abs(value) <= 1.0:
        pct = int(round(abs(value) * 100))
        if pct:
            out.add(str(pct))
    s = str(value)
    for n in _NUMBERS_RE.findall(s):
        clean = n.replace(" ", "").replace("₹", "").replace(",", "").rstrip("%")
        if clean:
            out.add(clean)
    for t in _CAP_TOKEN_RE.findall(s):
        if len(t) >= 3:
            out.add(t)


def has_payload_anchor(body: str, bundle: Dict[str, Any] | None) -> bool:
    """True if `body` cites at least one number/named-entity drawn from the bundle.

    Brief §5.5 "Specificity wins" + §11 "hallucinated data" anti-pattern. We
    don't require a match (too strict for warm customer messages), but the
    composer flags a miss so the engineer sees low-grounding outputs in the
    audit log.
    """
    if not body or not bundle:
        return False
    anchors: set[str] = set()
    _extract_anchor_tokens(bundle.get("trigger"), anchors)
    _extract_anchor_tokens(bundle.get("merchant"), anchors)
    _extract_anchor_tokens(bundle.get("customer"), anchors)
    # Only category-digest anchors (not the whole vertical taxonomy which
    # would spuriously match any capitalised vocab term).
    cat = bundle.get("category") or {}
    if isinstance(cat, dict):
        _extract_anchor_tokens(cat.get("digest"), anchors)
        _extract_anchor_tokens(cat.get("offer_catalog"), anchors)
        _extract_anchor_tokens(cat.get("peer_stats"), anchors)
        _extract_anchor_tokens(cat.get("seasonal_beats"), anchors)
        _extract_anchor_tokens(cat.get("trend_signals"), anchors)
    if not anchors:
        return False
    body_tokens_num = {m.replace(",", "").rstrip("%") for m in _NUMBERS_RE.findall(body)}
    body_tokens_cap = set(_CAP_TOKEN_RE.findall(body))
    if body_tokens_num & anchors:
        return True
    if body_tokens_cap & anchors:
        return True
    return False


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
