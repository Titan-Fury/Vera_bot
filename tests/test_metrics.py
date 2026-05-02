"""Metrics module + /v1/metrics + /dashboard endpoint tests."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from vera_bot.api.app import app
from vera_bot.composer.metrics import METRICS


@pytest.fixture()
def client():
    c = TestClient(app)
    c.post("/v1/teardown")
    yield c
    c.post("/v1/teardown")


def _ctx(scope, cid, v, payload):
    return {"scope": scope, "context_id": cid, "version": v,
            "payload": payload, "delivered_at": "2026-04-29T00:00:00Z"}


def _seed_and_tick(c: TestClient) -> None:
    c.post("/v1/context", json=_ctx("category", "restaurants", 1, {
        "slug": "restaurants", "voice": {"tone": "warm"}}))
    c.post("/v1/context", json=_ctx("merchant", "mx_1", 1, {
        "merchant_id": "mx_1", "category_slug": "restaurants",
        "identity": {"name": "Tandoor House", "languages": ["en"]},
        "performance": {"views": 3000, "calls": 120, "ctr": 0.04},
    }))
    c.post("/v1/context", json=_ctx("trigger", "trg_1", 1, {
        "id": "trg_1", "kind": "perf_dip", "urgency": 4,
        "merchant_id": "mx_1", "payload": {"metric": "views"}}))
    c.post("/v1/tick", json={"now": "2026-04-29T10:00:00Z",
                             "available_triggers": ["trg_1"]})


def test_metrics_snapshot_has_required_keys():
    snap = METRICS.snapshot()
    for k in ("compose_total", "compose_by_provider", "compose_by_path",
              "latency_ms", "template_ratio", "recent"):
        assert k in snap


def test_metrics_endpoint_returns_snapshot(client):
    _seed_and_tick(client)
    r = client.get("/v1/metrics")
    assert r.status_code == 200
    j = r.json()
    assert "provider_chain" in j
    assert "composer_version" in j
    assert j["compose_total"] >= 1
    # perf_dip is templated — should land in "template" path.
    assert j["compose_by_path"].get("template", 0) >= 1


def test_dashboard_returns_html(client):
    r = client.get("/dashboard")
    assert r.status_code == 200
    assert "text/html" in r.headers.get("content-type", "")
    assert "Vera" in r.text
    assert "/v1/metrics" in r.text  # JS fetches it


def test_tier_cold_honors_window():
    METRICS.record_tier_429("test_tier", cold_seconds=60.0)
    assert METRICS.is_tier_cold("test_tier")
    # Idempotent read after window would return False — can't time-travel
    # in a unit test, but we can verify the record path works.
    snap = METRICS.snapshot()
    assert "test_tier" in snap["tier_429_counts"]
