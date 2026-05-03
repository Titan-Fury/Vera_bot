"""FastAPI application — the five spec endpoints plus optional /v1/teardown.

State is in-memory; run with a single uvicorn worker. All composition goes
through EngagementComposer; error semantics follow challenge-testing-brief §2.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Any, Dict

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

from ..adapter import ContextAggregator, format_context_for_prompt
from ..composer import EngagementComposer, make_provider
from ..composer.metrics import METRICS
from ..config import (
    APP_VERSION,
    COMPOSER_VERSION,
    CONTEXT_PAYLOAD_CAP_BYTES,
    DEFAULT_COMPOSE_TIMEOUT_S,
    PRECOMPUTE_STAGGER_MS,
    SETTINGS,
    TICK_ACTION_CAP,
    TICK_CONCURRENCY,
)
from ..store import ContextStore, ConversationStore, VALID_SCOPES
from .errors import register_error_handlers
from .schemas import (
    CtxBody,
    HealthResponse,
    MetadataResponse,
    ReplyBody,
    TickAction,
    TickBody,
    TickResponse,
)

logger = logging.getLogger(__name__)


app = FastAPI(title="Vera Engagement Bot", version=APP_VERSION)
register_error_handlers(app)

START_TIME = time.time()
context_store = ContextStore()
conversation_store = ConversationStore()
aggregator = ContextAggregator(context_store)
llm_provider = make_provider(SETTINGS)
composer = EngagementComposer(llm_provider)

# Precompute cache: trigger_id -> Task[Composition | None]. The judge pushes
# context deltas before /v1/tick (brief §280-282), so we start composing in
# the background the moment a bundle is resolvable; tick reads from cache.
_compose_cache: Dict[str, "asyncio.Task[Any]"] = {}
_cache_lock = asyncio.Lock()

# Single global budget for every outbound LLM call — shared by precompute and
# tick. Keeps bursty context ingest from hammering free-tier upstreams past
# their RPM cap. Size tracks TICK_CONCURRENCY so one env var controls all
# provider pressure.
_compose_semaphore = asyncio.Semaphore(TICK_CONCURRENCY)


async def _compose_through_budget(
    bundle_dict: Dict[str, Any],
    formatted: str,
    prior_outbound: Any = None,
) -> Any:
    """Run composer.compose inside the global concurrency budget + timeout."""
    async with _compose_semaphore:
        return await asyncio.wait_for(
            asyncio.to_thread(composer.compose, bundle_dict, formatted, prior_outbound),
            timeout=DEFAULT_COMPOSE_TIMEOUT_S,
        )


async def _background_compose(trigger_id: str) -> Any:
    bundle = aggregator.build(trigger_id=trigger_id)
    if not bundle.merchant:
        return None
    formatted = format_context_for_prompt(bundle)
    try:
        return await _compose_through_budget(bundle.as_dict(), formatted, None)
    except asyncio.TimeoutError:
        logger.warning("precompute: timed out for trigger_id=%s", trigger_id)
        return None
    except Exception:
        logger.exception("precompute: failed for trigger_id=%s", trigger_id)
        return None


async def _schedule_precompute(trigger_id: str) -> None:
    async with _cache_lock:
        existing = _compose_cache.get(trigger_id)
        if existing is not None and not existing.done():
            return
        if existing is not None and existing.done():
            # Re-run only if the prior attempt gave up with a missing bundle.
            try:
                if existing.result() is not None:
                    return
            except Exception:
                pass
        # Stagger precompute launches so a 25-trigger judge burst doesn't
        # fire 25 upstream LLM calls in the same millisecond.
        stagger_s = _next_precompute_delay()
        _compose_cache[trigger_id] = asyncio.create_task(
            _delayed_background_compose(trigger_id, stagger_s)
        )


# Simple monotonic counter gives each fresh precompute a larger delay than
# the previous one — effectively a token-bucket without external state. Reset
# whenever the cache is cleared so /v1/teardown returns us to a fresh rate.
_precompute_launch_counter: int = 0


def _next_precompute_delay() -> float:
    """Return a staggered launch delay in seconds."""
    global _precompute_launch_counter
    _precompute_launch_counter += 1
    stagger_s = (PRECOMPUTE_STAGGER_MS / 1000.0) * (_precompute_launch_counter % TICK_CONCURRENCY)
    return stagger_s


async def _delayed_background_compose(trigger_id: str, delay_s: float) -> Any:
    if delay_s > 0:
        await asyncio.sleep(delay_s)
    return await _background_compose(trigger_id)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


@app.middleware("http")
async def _payload_cap(request: Request, call_next):
    """Enforce the 500 KB body cap on /v1/context (spec §5)."""
    if request.url.path == "/v1/context" and request.method == "POST":
        cl = request.headers.get("content-length")
        if cl and cl.isdigit() and int(cl) > CONTEXT_PAYLOAD_CAP_BYTES:
            return JSONResponse(
                status_code=400,
                content={
                    "accepted": False,
                    "reason": "payload_too_large",
                    "details": {"limit_bytes": CONTEXT_PAYLOAD_CAP_BYTES},
                },
            )
    return await call_next(request)


@app.get("/v1/healthz", response_model=HealthResponse)
async def healthz():
    return HealthResponse(
        uptime_seconds=int(time.time() - START_TIME),
        contexts_loaded=context_store.counts(),
        composer_version=COMPOSER_VERSION,
        provider=getattr(llm_provider, "name", type(llm_provider).__name__),
    )


@app.get("/v1/metadata", response_model=MetadataResponse)
async def metadata():
    return MetadataResponse(
        team_name=SETTINGS.team_name,
        team_members=list(SETTINGS.team_members),
        model=(
            SETTINGS.llm_model
            or getattr(llm_provider, "model", None)
            or f"default-for-{getattr(llm_provider, 'name', 'unknown')}"
        ),
        approach=SETTINGS.approach,
        contact_email=SETTINGS.contact_email,
        version=APP_VERSION,
        submitted_at=SETTINGS.submitted_at,
    )


@app.get("/v1/metrics")
async def metrics_endpoint():
    """In-memory operational metrics (not part of the required spec surface).

    Exposes compose counts, fallback cascade stats, p50/p95 latency, 429
    counts per tier, and a ring buffer of the 20 most recent composes.
    Reviewer-facing observability; zero external deps.
    """
    snap = METRICS.snapshot()
    snap["provider_chain"] = getattr(llm_provider, "name", type(llm_provider).__name__)
    snap["composer_version"] = COMPOSER_VERSION
    return snap


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard():
    """Self-contained HTML operator dashboard. Auto-refreshes every 5s."""
    return HTMLResponse(_DASHBOARD_HTML)


_DASHBOARD_HTML = """<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta http-equiv="refresh" content="5">
<title>Vera — Operator Dashboard</title>
<style>
 body{font-family:ui-monospace,Menlo,Consolas,monospace;background:#0b0f14;color:#d7e3f4;margin:0;padding:20px;font-size:13px}
 h1{font-size:16px;margin:0 0 12px;color:#7cc4ff}
 h2{font-size:13px;color:#7cc4ff;margin:18px 0 6px;border-bottom:1px solid #1a2530;padding-bottom:4px}
 .grid{display:grid;grid-template-columns:1fr 1fr;gap:18px}
 table{border-collapse:collapse;width:100%}
 td,th{text-align:left;padding:4px 10px 4px 0;border-bottom:1px solid #14202b}
 .hot{color:#6de59e}.cold{color:#ff8080}.meta{color:#6a7c8c}
 pre{white-space:pre-wrap;word-break:break-word;background:#0f1620;padding:8px;border-radius:4px;margin:0;font-size:12px}
 .bar{display:inline-block;height:10px;background:#2d7cff;vertical-align:middle;margin-right:6px}
 .flags{color:#ffc864;font-size:11px}
</style></head><body>
<h1>Vera — Operator Dashboard <span class=meta id=v></span></h1>
<div class=grid>
  <div>
    <h2>Provider chain</h2>
    <div id=chain><span class=meta>loading…</span></div>
    <h2>Compose paths</h2>
    <table id=paths></table>
    <h2>Provider mix</h2>
    <table id=providers></table>
    <h2>Fallback cascade</h2>
    <table id=cascade></table>
    <h2>429s by tier</h2>
    <table id=t429></table>
    <h2>Latency (ms)</h2>
    <table id=lat></table>
  </div>
  <div>
    <h2>Recent composes (last 20)</h2>
    <div id=recent><span class=meta>loading…</span></div>
  </div>
</div>
<script>
async function tick(){
 const r=await fetch('/v1/metrics'); const m=await r.json();
 document.getElementById('v').textContent=`uptime ${m.uptime_seconds}s · total ${m.compose_total} · ${m.composer_version}`;
 document.getElementById('chain').innerHTML=`<code>${m.provider_chain}</code>`;
 const mkrows=(obj)=>Object.entries(obj).map(([k,v])=>`<tr><td>${k}</td><td>${v}</td></tr>`).join('');
 document.getElementById('paths').innerHTML=mkrows(m.compose_by_path);
 document.getElementById('providers').innerHTML=mkrows(m.compose_by_provider);
 document.getElementById('cascade').innerHTML=mkrows(m.fallback_cascade)||'<tr><td class=meta>no cascades</td></tr>';
 const t429=Object.entries(m.tier_429_counts).map(([k,v])=>{
  const cold=m.tier_cold_remaining_s[k]||0;
  const badge=cold>0?`<span class=cold>COLD ${cold}s</span>`:`<span class=hot>hot</span>`;
  return `<tr><td>${k}</td><td>${v}</td><td>${badge}</td></tr>`;}).join('');
 document.getElementById('t429').innerHTML=t429||'<tr><td class=meta>none</td></tr>';
 document.getElementById('lat').innerHTML=`<tr><td>p50</td><td>${m.latency_ms.p50}</td></tr><tr><td>p95</td><td>${m.latency_ms.p95}</td></tr><tr><td>samples</td><td>${m.latency_ms.samples}</td></tr><tr><td>template ratio</td><td>${m.template_ratio}</td></tr>`;
 const rec=m.recent.slice().reverse().map(x=>{
  const flags=x.flags&&x.flags.length?`<div class=flags>${x.flags.join(' · ')}</div>`:'';
  return `<pre><b>[${x.path}] ${x.kind}</b> · ${x.provider||'template'} · ${x.latency_ms}ms · ${x.body_len}c\\n${x.body_preview}</pre>`;}).join('');
 document.getElementById('recent').innerHTML=rec||'<span class=meta>no composes yet</span>';
}
tick();
</script></body></html>"""


@app.post("/v1/context")
async def push_context(body: CtxBody):
    if body.scope not in VALID_SCOPES:
        raise HTTPException(
            status_code=400,
            detail={"accepted": False, "reason": "invalid_scope", "details": {"scope": body.scope}},
        )

    accepted, current = await context_store.put(body.scope, body.context_id, body.version, body.payload)
    if not accepted:
        return JSONResponse(
            status_code=409,
            content={
                "accepted": False,
                "reason": "stale_version",
                "current_version": current,
            },
        )

    # Trigger a background compose whenever the new arrival completes a bundle:
    #   - trigger arrived with merchant already stored
    #   - merchant arrived — fan out to all pending triggers for it
    #   - customer arrived — fan out to pending customer-facing triggers
    if body.scope == "trigger":
        merchant_id = body.payload.get("merchant_id") if isinstance(body.payload, dict) else None
        if merchant_id and context_store.get("merchant", merchant_id):
            await _schedule_precompute(body.context_id)
    elif body.scope == "merchant":
        for tid, tpayload in context_store.iter_scope("trigger"):
            if tpayload.get("merchant_id") == body.context_id:
                await _schedule_precompute(tid)
    elif body.scope == "customer":
        for tid, tpayload in context_store.iter_scope("trigger"):
            if tpayload.get("customer_id") != body.context_id:
                continue
            mid = tpayload.get("merchant_id")
            if mid and context_store.get("merchant", mid):
                await _schedule_precompute(tid)

    return {
        "accepted": True,
        "ack_id": f"ack_{body.context_id}_v{body.version}",
        "stored_at": _now_iso(),
    }


@app.post("/v1/tick", response_model=TickResponse)
async def tick(body: TickBody):
    ordered = _order_triggers(body.available_triggers, body.now)[:TICK_ACTION_CAP]

    async def _compose_one(trigger_id: str):
        bundle = aggregator.build(trigger_id=trigger_id)
        if not bundle.merchant:
            return None

        merchant_id = bundle.merchant.get("merchant_id")
        if not merchant_id:
            logger.warning("tick: merchant missing merchant_id; skipping trigger_id=%s", trigger_id)
            return None

        conv_id = f"conv_{merchant_id}_{trigger_id}"

        comp = None
        cached_task = _compose_cache.get(trigger_id)
        if cached_task is not None:
            try:
                comp = await asyncio.wait_for(
                    asyncio.shield(cached_task), timeout=DEFAULT_COMPOSE_TIMEOUT_S
                )
            except asyncio.TimeoutError:
                logger.warning("tick: precompute still running past budget for trigger_id=%s", trigger_id)
            except ValueError:
                # Task was created on a different event loop (only reachable
                # under FastAPI TestClient, which spins a fresh loop per call).
                pass
            except Exception:
                logger.exception("tick: precompute raised for trigger_id=%s", trigger_id)

        if comp is None:
            formatted = format_context_for_prompt(bundle)
            try:
                comp = await _compose_through_budget(bundle.as_dict(), formatted, None)
            except asyncio.TimeoutError:
                logger.warning("tick: compose timed out for trigger_id=%s", trigger_id)
                return None
            except Exception:
                logger.exception("tick: compose failed for trigger_id=%s", trigger_id)
                return None

        if not comp.body.strip():
            logger.warning("tick: empty body for trigger_id=%s", trigger_id)
            return None

        kind = (bundle.trigger or {}).get("kind", "default")
        customer = bundle.customer or None
        customer_id = customer.get("customer_id") if customer else None

        # suppression_key must be unique per (merchant, trigger). The composer
        # emits a coarse prefix; we append the trigger id for uniqueness.
        supp_key = (
            f"{comp.suppression_key}:{trigger_id}" if comp.suppression_key
            else f"{merchant_id}:{trigger_id}"
        )

        flags = getattr(comp, "flags", None) or {}
        if flags:
            logger.info("tick: trigger_id=%s flags=%s", trigger_id, flags)

        action = TickAction(
            conversation_id=conv_id,
            merchant_id=merchant_id,
            customer_id=customer_id,
            send_as=comp.send_as,
            trigger_id=trigger_id,
            template_name=f"vera_{kind}_v1",
            template_params=_template_params_for(bundle, kind),
            body=comp.body,
            cta=comp.cta,
            suppression_key=supp_key,
            rationale=comp.rationale,
        )

        await conversation_store.record_outbound(
            conversation_id=conv_id,
            merchant_id=merchant_id,
            customer_id=customer_id,
            trigger_id=trigger_id,
            send_as=comp.send_as,
            body=comp.body,
        )
        return action

    results = await asyncio.gather(*(_compose_one(tid) for tid in ordered))
    actions: list[TickAction] = [a for a in results if a is not None][:TICK_ACTION_CAP]
    return TickResponse(actions=actions)


def _order_triggers(trigger_ids: list[str], now_iso: str) -> list[str]:
    """Sort by urgency DESC, drop expired. Unknown triggers keep urgency=1."""
    scored: list[tuple[int, str, str]] = []
    for tid in trigger_ids:
        t = context_store.get("trigger", tid) or {}
        exp = (t.get("expires_at") or "") if t else ""
        if exp and exp <= now_iso:
            continue
        urgency = int(t.get("urgency", 1)) if t else 1
        scored.append((-urgency, exp or "\uffff", tid))
    scored.sort()
    return [tid for _, _, tid in scored]


def _template_params_for(bundle, kind: str) -> list[str]:
    """Derive a readable template parameter list.

    Order: [customer_name?, merchant_name, locality?, metric?, kind]. The
    metric slot is populated for perf/digest/festival/competitor/milestone
    kinds so the judge sees context-rich templates.
    """
    m = bundle.merchant or {}
    c = bundle.customer or {}
    t = bundle.trigger or {}
    ident = m.get("identity", {}) or {}
    name = ident.get("name") or m.get("merchant_id") or ""
    locality = ident.get("locality") or ident.get("city") or ""

    payload = t.get("payload", {}) or {}
    metric = ""
    if kind in ("perf_dip", "perf_spike", "research_digest"):
        for key, val in payload.items():
            if isinstance(val, (int, float, str)) and str(val):
                metric = f"{key} {val}"
                break
    elif kind == "festival_upcoming":
        metric = str(payload.get("festival") or payload.get("name") or "")
    elif kind == "competitor_opened":
        metric = str(payload.get("competitor") or payload.get("distance_m") or "")
    elif kind == "milestone_reached":
        metric = str(payload.get("milestone") or payload.get("metric") or "")

    params: list[str] = []
    if c:
        params.append((c.get("identity", {}) or {}).get("name", ""))
    if name:
        params.append(name)
    if locality:
        params.append(locality)
    if metric:
        params.append(metric)
    params.append(kind)
    return [p for p in params if p]


@app.post("/v1/reply")
async def reply(body: ReplyBody):
    conv = await conversation_store.record_inbound(
        conversation_id=body.conversation_id,
        from_role=body.from_role,
        body=body.message,
        ts=body.received_at,
    )

    merchant_id = body.merchant_id or conv.meta.merchant_id
    customer_id = body.customer_id or conv.meta.customer_id
    trigger_id = conv.meta.trigger_id

    bundle = aggregator.build(trigger_id=trigger_id, merchant_id=merchant_id, customer_id=customer_id)
    formatted = format_context_for_prompt(bundle)

    chat_history = [{"from": t.role, "msg": t.body} for t in conv.turns]

    try:
        comp = await asyncio.wait_for(
            asyncio.to_thread(
                composer.compose_reply,
                bundle.as_dict(),
                formatted,
                chat_history,
                body.message,
                body.turn_number,
            ),
            timeout=DEFAULT_COMPOSE_TIMEOUT_S,
        )
    except asyncio.TimeoutError:
        logger.warning("reply: compose_reply timed out for conv=%s", body.conversation_id)
        return {"action": "wait", "wait_seconds": 60, "rationale": f"[{COMPOSER_VERSION}] compose timeout; retry later"}
    except Exception:
        logger.exception("reply: compose_reply failed for conv=%s", body.conversation_id)
        return {"action": "end", "rationale": f"[{COMPOSER_VERSION}] internal error; closing conversation"}

    # action=send with empty body would incur the §10 "malformed" penalty;
    # degrade to a clean end instead.
    if comp.action == "send" and not comp.body:
        logger.warning("reply: empty body after validation for conv=%s", body.conversation_id)
        return {
            "action": "end",
            "rationale": f"[{COMPOSER_VERSION}] composer returned empty body after validation",
        }

    if comp.action == "send" and comp.body:
        await conversation_store.record_outbound(
            conversation_id=body.conversation_id,
            merchant_id=merchant_id,
            customer_id=customer_id,
            trigger_id=trigger_id,
            send_as=("merchant_on_behalf" if bundle.customer else "vera"),
            body=comp.body,
        )

    return comp.to_reply()


@app.post("/v1/teardown")
async def teardown():
    """Optional per spec §11 — clears all state. Idempotent."""
    global _precompute_launch_counter
    await context_store.clear()
    await conversation_store.clear()
    async with _cache_lock:
        for task in _compose_cache.values():
            if not task.done():
                task.cancel()
        _compose_cache.clear()
        _precompute_launch_counter = 0
    return {"ok": True, "cleared_at": _now_iso()}


if __name__ == "__main__":  # pragma: no cover
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8080, workers=1)
