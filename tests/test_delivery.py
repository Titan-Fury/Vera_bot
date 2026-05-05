"""Tests for the delivery layer: safety guards, console provider, factory."""

from __future__ import annotations

import os
import time
import tempfile
from pathlib import Path

import pytest

from vera_bot.delivery.console import ConsoleProvider
from vera_bot.delivery.safety import (
    OptOutStore,
    RateLimiter,
    SafetyGuard,
)


# ============================================================================
# OptOutStore
# ============================================================================

def test_opt_out_store_add_and_check(tmp_path: Path):
    store = OptOutStore(tmp_path / "opt_out.jsonl")
    assert not store.is_opted_out("+919876543210")
    store.add("+919876543210", reason="user_request")
    assert store.is_opted_out("+919876543210")
    # Idempotent
    store.add("+919876543210", reason="duplicate")
    # Survives reload
    store2 = OptOutStore(tmp_path / "opt_out.jsonl")
    assert store2.is_opted_out("+919876543210")


def test_opt_out_normalizes_phone(tmp_path: Path):
    store = OptOutStore(tmp_path / "opt_out.jsonl")
    store.add("+91 98765 43210")
    # Both forms should hit the same record
    assert store.is_opted_out("+919876543210")
    assert store.is_opted_out("919876543210")
    assert store.is_opted_out("9876543210") is False  # different number


def test_opt_out_detect_keywords():
    assert OptOutStore.detect_in_message("Please STOP messaging me")
    assert OptOutStore.detect_in_message("unsubscribe")
    assert OptOutStore.detect_in_message("मत भेज मुझे")
    assert not OptOutStore.detect_in_message("yes I want more info")


# ============================================================================
# RateLimiter
# ============================================================================

def test_rate_limiter_per_recipient_blocks_within_gap():
    rl = RateLimiter(min_gap_seconds_per_recipient=60, daily_cap=100)
    now = 1000.0
    v = rl.check("+919876543210", now=now)
    assert v.allowed
    rl.record("+919876543210", now=now)

    # Second send 30s later should be blocked
    v2 = rl.check("+919876543210", now=now + 30)
    assert not v2.allowed
    assert "per_recipient_rate" in v2.reason
    assert v2.retry_after_s > 0

    # 70s later allowed again
    v3 = rl.check("+919876543210", now=now + 70)
    assert v3.allowed


def test_rate_limiter_daily_cap():
    rl = RateLimiter(min_gap_seconds_per_recipient=0, daily_cap=3)
    now = 1000.0
    for i in range(3):
        v = rl.check(f"+91987654321{i}", now=now + i)
        assert v.allowed
        rl.record(f"+91987654321{i}", now=now + i)

    # 4th send hits daily cap
    v4 = rl.check("+919876543299", now=now + 4)
    assert not v4.allowed
    assert "daily_cap_reached" in v4.reason


def test_rate_limiter_prunes_after_24h():
    rl = RateLimiter(min_gap_seconds_per_recipient=0, daily_cap=2)
    now = 1000.0
    rl.check("+919876543210", now=now)
    rl.record("+919876543210", now=now)
    rl.check("+919876543299", now=now)
    rl.record("+919876543299", now=now)
    # Cap hit at 'now'
    assert not rl.check("+919876543200", now=now).allowed
    # 25h later, both old sends pruned, new one allowed
    later = now + 86400 + 3600
    assert rl.check("+919876543200", now=later).allowed


# ============================================================================
# SafetyGuard
# ============================================================================

def test_safety_guard_blocks_on_opt_out(tmp_path: Path):
    store = OptOutStore(tmp_path / "opt_out.jsonl")
    rl = RateLimiter(min_gap_seconds_per_recipient=0, daily_cap=100)
    guard = SafetyGuard(opt_out=store, rate_limit=rl)

    store.add("+919876543210")
    v = guard.check("+919876543210")
    assert not v.allowed
    assert v.blocked_by == "opt_out"


def test_safety_guard_blocks_on_rate_limit(tmp_path: Path):
    store = OptOutStore(tmp_path / "opt_out.jsonl")
    rl = RateLimiter(min_gap_seconds_per_recipient=3600, daily_cap=100)
    guard = SafetyGuard(opt_out=store, rate_limit=rl)

    assert guard.check("+919876543210").allowed
    guard.record_send("+919876543210")
    blocked = guard.check("+919876543210")
    assert not blocked.allowed
    assert blocked.blocked_by == "rate_limit"


def test_safety_guard_opt_out_recipient_persists(tmp_path: Path):
    store = OptOutStore(tmp_path / "opt_out.jsonl")
    rl = RateLimiter(min_gap_seconds_per_recipient=0, daily_cap=100)
    guard = SafetyGuard(opt_out=store, rate_limit=rl)

    guard.opt_out_recipient("+919876543210", "test")
    assert not guard.check("+919876543210").allowed


# ============================================================================
# ConsoleProvider
# ============================================================================

def test_console_provider_send_text(capsys):
    p = ConsoleProvider()
    r = p.send_text("+919876543210", "Hello world", context={"trigger_id": "t1"})
    assert r.success
    assert r.provider == "console"
    assert r.message_id is not None
    out = capsys.readouterr().out
    assert "Hello world" in out
    assert "+919876543210" in out


def test_console_provider_send_template(capsys):
    p = ConsoleProvider()
    r = p.send_template("+919876543210", "hello_world", "en_US")
    assert r.success
    assert r.body and "template:hello_world" in r.body


def test_console_provider_health():
    p = ConsoleProvider()
    h = p.health()
    assert h["ok"] is True
    assert h["provider"] == "console"


# ============================================================================
# Factory
# ============================================================================

def test_factory_defaults_to_console(monkeypatch):
    from vera_bot.delivery.factory import get_provider

    monkeypatch.delenv("WHATSAPP_PROVIDER", raising=False)
    monkeypatch.delenv("WHATSAPP_DRY_RUN", raising=False)
    p = get_provider()
    assert p.name == "console"


def test_factory_dry_run_overrides_cloud(monkeypatch):
    from vera_bot.delivery.factory import get_provider

    monkeypatch.setenv("WHATSAPP_PROVIDER", "cloud")
    monkeypatch.setenv("WHATSAPP_CLOUD_TOKEN", "fake")
    monkeypatch.setenv("WHATSAPP_CLOUD_PHONE_NUMBER_ID", "12345")
    monkeypatch.setenv("WHATSAPP_DRY_RUN", "1")
    p = get_provider()
    assert p.name == "console"


def test_factory_cloud_falls_back_to_console_without_creds(monkeypatch):
    from vera_bot.delivery.factory import get_provider

    monkeypatch.setenv("WHATSAPP_PROVIDER", "cloud")
    monkeypatch.delenv("WHATSAPP_CLOUD_TOKEN", raising=False)
    monkeypatch.delenv("WHATSAPP_CLOUD_PHONE_NUMBER_ID", raising=False)
    monkeypatch.delenv("WHATSAPP_DRY_RUN", raising=False)
    p = get_provider()
    assert p.name == "console"


def test_factory_unknown_provider_falls_back(monkeypatch):
    from vera_bot.delivery.factory import get_provider

    monkeypatch.setenv("WHATSAPP_PROVIDER", "ringring")
    monkeypatch.delenv("WHATSAPP_DRY_RUN", raising=False)
    p = get_provider()
    assert p.name == "console"


# ============================================================================
# WhatsApp Cloud provider validation (no live call)
# ============================================================================

def test_whatsapp_cloud_rejects_invalid_phone(monkeypatch):
    from vera_bot.delivery.whatsapp_cloud import WhatsAppCloudProvider
    p = WhatsAppCloudProvider(token="fake", phone_number_id="123")
    r = p.send_text("not-a-phone", "body")
    assert not r.success
    assert r.error_code == "invalid_phone"


def test_whatsapp_cloud_rejects_empty_body():
    from vera_bot.delivery.whatsapp_cloud import WhatsAppCloudProvider
    p = WhatsAppCloudProvider(token="fake", phone_number_id="123")
    r = p.send_text("+919876543210", "  ")
    assert not r.success
    assert r.error_code == "empty_body"
