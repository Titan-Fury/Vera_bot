"""HTTP contract tests - spec: challenge-testing-brief.md §2-5."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from vera_bot.api.app import app


@pytest.fixture()
def client():
    c = TestClient(app)
    c.post("/v1/teardown")
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


def test_healthz(client):
    r = client.get("/v1/healthz")
    assert r.status_code == 200
    j = r.json()
    assert j["status"] == "ok"
    assert j["composer_version"] == "composer_v1"


def test_metadata_is_stable(client):
    a = client.get("/v1/metadata").json()
    b = client.get("/v1/metadata").json()
    assert a["submitted_at"] == b["submitted_at"]
    assert a["version"] == "0.2.1"


def test_context_accept_and_stale(client):
    r = client.post("/v1/context", json=_ctx("category", "restaurants", 1, {"slug": "restaurants"}))
    assert r.status_code == 200 and r.json()["accepted"] is True
    r = client.post("/v1/context", json=_ctx("category", "restaurants", 1, {"slug": "restaurants"}))
    assert r.status_code == 409
    j = r.json()
    assert j["reason"] == "stale_version" and j["current_version"] == 1


def test_context_invalid_scope_400(client):
    r = client.post("/v1/context", json=_ctx("bogus", "x", 1, {}))
    assert r.status_code == 400
    assert r.json()["reason"] == "invalid_payload"


def test_context_malformed_400(client):
    r = client.post("/v1/context", json={"scope": "merchant"})
    assert r.status_code == 400
    j = r.json()
    assert j["reason"] == "invalid_payload" and "errors" in j["details"]


def _seed_minimum(client):
    client.post("/v1/context", json=_ctx("category", "restaurants", 1, {"slug": "restaurants", "voice": {"tone": "warm"}}))
    client.post("/v1/context", json=_ctx("merchant", "mx_1", 1, {
        "merchant_id": "mx_1", "category_slug": "restaurants",
        "identity": {"name": "Tandoor House", "city": "Bengaluru", "locality": "Koramangala", "languages": ["en"]},
        "performance": {"views": 3000, "calls": 120, "ctr": 0.04},
    }))
    client.post("/v1/context", json=_ctx("trigger", "trg_1", 1, {
        "id": "trg_1", "kind": "perf_dip", "urgency": 4, "merchant_id": "mx_1", "payload": {"metric": "views"}
    }))


def test_tick_produces_valid_action(client):
    _seed_minimum(client)
    r = client.post("/v1/tick", json={"now": "now", "available_triggers": ["trg_1"]})
    assert r.status_code == 200
    actions = r.json()["actions"]
    assert len(actions) == 1
    a = actions[0]
    assert a["merchant_id"] == "mx_1"
    assert a["trigger_id"] == "trg_1"
    assert a["template_name"] == "vera_perf_dip_v1"
    assert a["template_params"] != ["placeholder"] and a["template_params"]
    assert len(a["body"]) <= 320
    assert "http" not in a["body"].lower()


def test_tick_caps_at_20_actions(client):
    _seed_minimum(client)
    for i in range(2, 26):
        client.post("/v1/context", json=_ctx("trigger", f"trg_{i}", 1, {
            "id": f"trg_{i}", "kind": "perf_dip", "urgency": 4, "merchant_id": "mx_1", "payload": {}
        }))
    trig_ids = [f"trg_{i}" for i in range(1, 26)]
    r = client.post("/v1/tick", json={"now": "now", "available_triggers": trig_ids})
    assert len(r.json()["actions"]) == 20


def test_reply_end_on_hard_no(client):
    _seed_minimum(client)
    tick = client.post("/v1/tick", json={"now": "now", "available_triggers": ["trg_1"]}).json()
    conv_id = tick["actions"][0]["conversation_id"]
    r = client.post("/v1/reply", json={
        "conversation_id": conv_id, "merchant_id": "mx_1", "from_role": "merchant",
        "message": "stop messaging me", "received_at": "now", "turn_number": 2,
    })
    assert r.json()["action"] == "end"


def test_reply_send_shape(client):
    _seed_minimum(client)
    tick = client.post("/v1/tick", json={"now": "now", "available_triggers": ["trg_1"]}).json()
    conv_id = tick["actions"][0]["conversation_id"]
    r = client.post("/v1/reply", json={
        "conversation_id": conv_id, "merchant_id": "mx_1", "from_role": "merchant",
        "message": "tell me more", "received_at": "now", "turn_number": 2,
    })
    j = r.json()
    assert j["action"] in {"send", "wait", "end"}
    if j["action"] == "send":
        assert "body" in j and len(j["body"]) <= 320


def test_tick_prioritizes_urgency_and_drops_expired(client):
    _seed_minimum(client)
    client.post("/v1/context", json=_ctx("trigger", "trg_high", 1, {
        "id": "trg_high", "kind": "perf_spike", "urgency": 9, "merchant_id": "mx_1", "payload": {"views": "+30%"}
    }))
    client.post("/v1/context", json=_ctx("trigger", "trg_exp", 1, {
        "id": "trg_exp", "kind": "perf_dip", "urgency": 10, "merchant_id": "mx_1",
        "expires_at": "2000-01-01T00:00:00Z", "payload": {"views": "-99%"}
    }))
    r = client.post("/v1/tick", json={
        "now": "2026-04-29T10:00:00Z",
        "available_triggers": ["trg_1", "trg_exp", "trg_high"],
    })
    ids = [a["trigger_id"] for a in r.json()["actions"]]
    assert ids[0] == "trg_high"
    assert "trg_exp" not in ids


def test_suppression_key_is_trigger_scoped(client):
    _seed_minimum(client)
    r = client.post("/v1/tick", json={"now": "now", "available_triggers": ["trg_1"]}).json()
    a = r["actions"][0]
    assert a["suppression_key"].endswith(":trg_1")


def test_malformed_bytes_body_safe_400(client):
    r = client.post("/v1/context", data=b"not json", headers={"Content-Type": "application/json"})
    assert r.status_code == 400
    j = r.json()
    assert j["reason"] == "invalid_payload" and "errors" in j["details"]


def test_teardown_clears_state(client):
    _seed_minimum(client)
    r = client.post("/v1/teardown")
    assert r.status_code == 200 and r.json()["ok"] is True
    counts = client.get("/v1/healthz").json()["contexts_loaded"]
    assert all(v == 0 for v in counts.values())
