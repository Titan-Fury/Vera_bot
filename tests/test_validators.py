"""Post-LLM validator tests."""
from __future__ import annotations

from vera_bot.composer.validators import (
    detect_auto_reply,
    detect_explicit_intent,
    detect_hard_no,
    enforce_length,
    enforce_taboos,
    ensure_offer_in_catalog,
    has_payload_anchor,
    is_repeat,
    strip_urls,
    validate_outbound,
)


def test_strip_urls_removes_all_forms():
    for raw, expected in [
        ("Visit https://magicpin.in today", "Visit today"),
        ("See www.example.com for more", "See for more"),
        ("ping flipkart.com", "ping"),
    ]:
        out, r = strip_urls(raw)
        assert "urls_stripped" in r.flags
        assert out.strip() == expected.strip()


def test_enforce_length_caps_at_320():
    out, r = enforce_length("x" * 400)
    assert len(out) <= 320 and "truncated" in r.flags


def test_enforce_length_sentence_boundary():
    body = "First sentence. Second sentence. " * 10
    out, _ = enforce_length(body)
    assert len(out) <= 320
    assert out.endswith(".") or out.endswith("…")


def test_validate_outbound_pipeline():
    raw = "Great news - check https://foo.com now. " + "padding " * 80
    out, r = validate_outbound(raw)
    assert len(out) <= 320
    assert "urls_stripped" in r.flags
    assert "http" not in out.lower()


def test_is_repeat_case_insensitive():
    assert is_repeat("Hello there", ["hello there"])
    assert not is_repeat("Hello there", ["Different message"])
    assert not is_repeat("", ["anything"])


def test_detect_hard_no():
    for m in ["Please stop", "not interested", "unsubscribe me", "बंद करो", "don't message me"]:
        assert detect_hard_no(m), m
    for m in ["tell me more", "sure, send it", ""]:
        assert not detect_hard_no(m), m


def test_detect_explicit_intent():
    for m in ["Yes please", "ok go ahead", "sounds good", "हाँ चलो", "let's do it", "sure"]:
        assert detect_explicit_intent(m), m
    for m in ["maybe later", "who is this"]:
        assert not detect_explicit_intent(m), m


def test_detect_auto_reply_triple_repeat():
    assert detect_auto_reply(["out", "out", "out"])


def test_detect_auto_reply_canned_phrase():
    assert detect_auto_reply(["Thank you for contacting our team!"])
    assert not detect_auto_reply(["Hi, who is this"])


def test_is_repeat_catches_fuzzy_rephrase():
    """0.92+ similarity should trip — catches "rephrased repeats"."""
    prior = ["Calls down 40% vs last 7d. Want to boost? Reply YES/NO."]
    # Tiny punctuation / casing tweak → should still flag as repeat.
    assert is_repeat("Calls down 40% vs last 7d! Want to boost? Reply yes/no", prior)
    # Genuinely different message → should NOT flag.
    assert not is_repeat("Views up 32% — want to extend the push?", prior)


def test_ensure_offer_in_catalog_strips_invented():
    catalog = [{"title": "Dental Cleaning @ ₹299"}]
    body = "Want to try our Diwali Glow Bundle @ ₹899? Reply YES/NO."
    out, r = ensure_offer_in_catalog(body, catalog, None)
    assert "invented_offer_dropped" in r.flags
    assert "Diwali Glow Bundle @ ₹899" not in out


def test_ensure_offer_in_catalog_allows_grounded():
    catalog = [{"title": "Dental Cleaning @ ₹299"}]
    body = "Dental Cleaning @ ₹299 is open Wed 6pm. Book it? Reply YES/NO."
    out, r = ensure_offer_in_catalog(body, catalog, None)
    assert "invented_offer_dropped" not in r.flags
    assert out == body


def test_ensure_offer_in_catalog_no_catalog_is_noop():
    out, r = ensure_offer_in_catalog("Haircut @ ₹99 available.", None, None)
    assert r.flags == []


def test_enforce_taboos_strips_and_flags():
    out, r = enforce_taboos("We guaranteed a cure for your cavity.", ["cure", "guaranteed"])
    assert "guaranteed" not in out.lower()
    assert "cure" not in out.lower()
    assert any(f.startswith("taboos_stripped:") for f in r.flags)


def test_enforce_taboos_empty_taboos_is_noop():
    out, r = enforce_taboos("Hello there", [])
    assert out == "Hello there"
    assert r.flags == []


def test_has_payload_anchor_number_match():
    bundle = {
        "trigger": {"payload": {"metric": "calls", "delta_pct": 0.28}},
        "merchant": {"identity": {"name": "Tandoor House"}},
    }
    assert has_payload_anchor("Calls up 28% — nice run", bundle)


def test_has_payload_anchor_named_entity():
    bundle = {
        "trigger": {"payload": {}},
        "merchant": {"identity": {"name": "Tandoor House"}},
        "category": {"digest": [{"title": "JIDA Oct 2026 fluoride trial"}]},
    }
    assert has_payload_anchor("JIDA Oct 2026: 38% better caries outcomes.", bundle)


def test_has_payload_anchor_miss():
    bundle = {
        "trigger": {"payload": {"metric": "calls", "delta_pct": 0.4}},
        "merchant": {"identity": {"name": "Tandoor House"}},
    }
    assert not has_payload_anchor("Generic nudge without any anchor.", bundle)
