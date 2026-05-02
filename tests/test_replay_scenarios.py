"""Replay scenario tests — the judge's Phase 4 probes.

Brief §12 open challenges + challenge-testing-brief.md §4 Phase 4:
these validate the bot's multi-turn handling before judges probe it.

Scenarios:
  - auto_reply_hell     → canned auto-reply detection + warm exit
  - intent_transition   → explicit YES → action mode (no more qualifying)
  - hostile_customer    → abuse → warm exit
  - objection           → "too expensive" → handle, don't exit
  - stale_reply         → days-later reply → still engages cleanly
  - hard_no_opt_out     → "stop" → immediate end
  - turn_cap_exit       → 6+ turns without commit → end
  - auto_reply_canned   → single canned phrase → end
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from vera_bot.api.app import app


@pytest.fixture()
def client():
    c = TestClient(app)
    c.post("/v1/teardown")
    _seed_minimum(c)
    yield c
    c.post("/v1/teardown")


def _ctx(scope, cid, v, payload):
    return {
        "scope": scope,
        "context_id": cid,
        "version": v,
        "payload": payload,
        "delivered_at": "2026-04-29T00:00:00Z",
    }


def _seed_minimum(c: TestClient) -> None:
    c.post("/v1/context", json=_ctx("category", "restaurants", 1, {
        "slug": "restaurants",
        "voice": {"tone": "warm"},
    }))
    c.post("/v1/context", json=_ctx("merchant", "mx_1", 1, {
        "merchant_id": "mx_1", "category_slug": "restaurants",
        "identity": {"name": "Tandoor House", "city": "Bengaluru",
                     "locality": "Koramangala", "languages": ["en", "hi"]},
        "performance": {"views": 3000, "calls": 120, "ctr": 0.04},
    }))
    c.post("/v1/context", json=_ctx("trigger", "trg_1", 1, {
        "id": "trg_1", "kind": "perf_dip", "urgency": 4,
        "merchant_id": "mx_1", "payload": {"metric": "views"},
    }))


def _open_conv(c: TestClient) -> str:
    tick = c.post(
        "/v1/tick", json={"now": "2026-04-29T10:00:00Z", "available_triggers": ["trg_1"]}
    ).json()
    assert tick["actions"], "seed compose should have produced an action"
    return tick["actions"][0]["conversation_id"]


def _reply(c: TestClient, conv_id: str, message: str, turn: int) -> dict:
    return c.post("/v1/reply", json={
        "conversation_id": conv_id, "merchant_id": "mx_1", "from_role": "merchant",
        "message": message, "received_at": "2026-04-29T10:00:00Z", "turn_number": turn,
    }).json()


# ---- Auto-reply hell ---------------------------------------------------------


def test_auto_reply_hell_triple_verbatim_exits(client):
    conv = _open_conv(client)
    auto = "Thank you for contacting us, our team will revert shortly."
    r1 = _reply(client, conv, auto, 2)
    # Canned phrase should trip detect_auto_reply on the first hit.
    assert r1["action"] == "end", r1
    assert "auto-reply" in r1["rationale"].lower()


def test_auto_reply_canned_phrase_exits_single(client):
    """A single canned-phrase reply should still trip the detector."""
    conv = _open_conv(client)
    r = _reply(client, conv, "Thank you for contacting our team!", 2)
    assert r["action"] == "end"


# ---- Intent transition -------------------------------------------------------


def test_explicit_intent_yes_triggers_send_action_mode(client):
    conv = _open_conv(client)
    r = _reply(client, conv, "yes, let's do it", 2)
    # Should NOT end, should NOT be a wait — this is the intent-handoff
    # failure the brief calls out in §11.
    assert r["action"] == "send", r


def test_explicit_intent_hinglish_yes(client):
    conv = _open_conv(client)
    r = _reply(client, conv, "हाँ चलो", 2)
    assert r["action"] == "send", r


# ---- Hostile / opt-out -------------------------------------------------------


@pytest.mark.parametrize("msg", [
    "stop messaging me",
    "not interested",
    "unsubscribe",
    "please don't contact",
    "leave me alone",
    "बंद करो",
])
def test_hard_no_opt_out_ends_conversation(client, msg):
    conv = _open_conv(client)
    r = _reply(client, conv, msg, 2)
    assert r["action"] == "end", f"{msg!r} -> {r}"
    assert "opt-out" in r["rationale"].lower() or "exit" in r["rationale"].lower()


# ---- Turn cap ----------------------------------------------------------------


def test_turn_cap_ends_after_six_turns(client):
    conv = _open_conv(client)
    r = _reply(client, conv, "maybe later", 6)
    assert r["action"] == "end"
    assert "turn cap" in r["rationale"].lower()


# ---- Stale reply + objection -------------------------------------------------


def test_stale_reply_still_handled_cleanly(client):
    """Days-later reply: bot should still produce a valid action, not crash."""
    conv = _open_conv(client)
    r = client.post("/v1/reply", json={
        "conversation_id": conv, "merchant_id": "mx_1", "from_role": "merchant",
        "message": "circling back — still want to discuss",
        "received_at": "2026-05-15T10:00:00Z", "turn_number": 2,
    }).json()
    assert r["action"] in {"send", "wait", "end"}
    if r["action"] == "send":
        assert r.get("body")
        assert len(r["body"]) <= 320


def test_objection_too_expensive_does_not_end(client):
    """Pushback is not opt-out — should keep the conversation alive."""
    conv = _open_conv(client)
    r = _reply(client, conv, "sounds too expensive for me right now", 2)
    # "too expensive" is an objection, not a hard no. Bot should engage.
    assert r["action"] != "end", r


# ---- Off-topic pivot ---------------------------------------------------------


def test_off_topic_question_is_handled(client):
    conv = _open_conv(client)
    r = _reply(client, conv, "can you also help me with GST filing?", 2)
    # Bot should not hard-fail; it produces some action in the allowed set.
    assert r["action"] in {"send", "wait", "end"}
