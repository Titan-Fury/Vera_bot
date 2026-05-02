"""Prompt selection + versioning tests."""
from __future__ import annotations

from vera_bot.composer.prompts import known_kinds, select_prompt


def test_every_known_kind_produces_distinct_block():
    base, _ = select_prompt("default", False, False)
    for kind in known_kinds():
        sp, _ = select_prompt(kind, False, False)
        assert f"TRIGGER KIND: {kind}" in sp, kind
        assert sp != base


def test_unknown_kind_falls_back_to_default():
    sp, _ = select_prompt("totally_made_up_kind", False, False)
    assert "TRIGGER KIND: <unlisted>" in sp


def test_reply_prompt_includes_reply_logic_and_wait_schema():
    sp, _ = select_prompt("perf_dip", False, True)
    assert "REPLY LOGIC:" in sp
    assert "action (send|wait|end)" in sp
    assert "wait_seconds" in sp


def test_customer_audience_switches_send_as():
    sp_m, _ = select_prompt("recall_due", False, False)
    sp_c, _ = select_prompt("recall_due", True, False)
    assert "send_as=vera" in sp_m
    assert "send_as=merchant_on_behalf" in sp_c


def test_base_rules_present():
    sp, _ = select_prompt("default", False, False)
    assert "HARD RULES" in sp
    assert "LENGTH: body <=320" in sp
    assert "NO URLS" in sp
