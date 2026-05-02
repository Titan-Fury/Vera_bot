"""Post-LLM validator tests."""
from __future__ import annotations

from vera_bot.composer.validators import (
    detect_auto_reply,
    detect_explicit_intent,
    detect_hard_no,
    enforce_length,
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
