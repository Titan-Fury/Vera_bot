# Vera Engagement Bot — Project Overview (Interview Edition)

A single-page, interview-ready walkthrough of what the system does, how
it's built, and why each decision was made. Pair this with
[`ARCHITECTURE.md`](./ARCHITECTURE.md) (per-module deep dive) and
[`RUNNING.md`](./RUNNING.md) (hands-on setup).

---

## 1. One-line pitch

**A stateless FastAPI service that composes policy-compliant WhatsApp
messages for magicpin merchants, using a pluggable multi-provider LLM
backend and a four-context (category / merchant / customer / trigger)
retrieval layer.**

It implements the 5-endpoint HTTP contract from the magicpin AI
Challenge brief and is judged by replaying a captured trace against
a live instance.

---

## 1b. What makes this submission different

Five things a reviewer scanning the repo should take away:

1. **Template bypass for mechanical triggers.** 7 trigger kinds
   (recall/refill/appointment, perf spike/dip, festival, milestone) skip
   the LLM entirely and compose through deterministic f-string builders.
   ~41 % of dataset triggers incur zero LLM latency, zero hallucination
   risk, and zero rate-limit exposure. See
   [ADR-002](./ARCHITECTURE.md#adr-002--template-bypass-for-mechanical-trigger-kinds).
2. **Circuit-broken provider chain.** `groq → gemini → openrouter-gemma →
   openrouter` with a Retry-After-aware circuit breaker; cold tiers
   skipped without a network call. See
   [ADR-005](./ARCHITECTURE.md#adr-005--circuit-breaker-with-retry-after).
3. **Hallucination-guarding validators.** Offer-catalog grounding strips
   invented `Service @ ₹price` phrases; category-voice taboos (`cure`,
   `guaranteed`) are enforced structurally; payload-fact anchoring is
   audited per compose. See
   [ADR-008](./ARCHITECTURE.md#adr-008--offer-catalog-grounding-guard) +
   [ADR-007](./ARCHITECTURE.md#adr-007--category-voice-blocks--taboo-enforcement) +
   [ADR-012](./ARCHITECTURE.md#adr-012--payload-fact-citation-audit).
4. **Few-shot-grounded prompts.** Each of the 22 kind blocks carries a
   9/10-quality example message, pushing the LLM toward fact-first
   openers, loss-aversion framing, and single-binary CTAs. See
   [ADR-009](./ARCHITECTURE.md#adr-009--few-shot-examples-per-trigger-kind).
5. **Operator observability.** A `/v1/metrics` JSON endpoint plus a
   self-contained `/dashboard` HTML page expose compose paths, provider
   mix, fallback cascade, 429 windows, p50/p95 latency, and a ring of
   the last 20 composes. Structured audit logs per compose. See
   [ADR-013](./ARCHITECTURE.md#adr-013--operator-observability-metrics--dashboard).

Replay-hardened: `tests/test_replay_scenarios.py` covers auto-reply
hell, intent transition (including Hinglish), hostile opt-out,
objection handling, stale replies, and turn-cap exits — the exact
Phase 4 probes the judge harness runs on top submissions.

---

## 2. Problem statement (what "good" looks like)

> magicpin has ~1M merchants. A human field rep can talk to maybe 50 a
> day. Write a bot that acts like a senior magicpin partner — warm,
> concise, data-grounded WhatsApp nudges — while respecting strict
> compliance rules (≤320 chars, no URLs, no spam, honor opt-outs).

Success is measured on five dimensions:
1. **Relevance** — does the message tie to the trigger and merchant?
2. **Warmth & clarity** — does it sound human, not templated?
3. **Compliance** — hard caps on length, URLs, repetition, opt-out.
4. **Protocol correctness** — HTTP contract adherence (200 / 400 / 409 / cap-at-20 / 10 s budget).
5. **Operational discipline** — idempotent, observable, reproducible.

---

## 3. High-level architecture

```
┌────────────────────────────────────────────────────────────────────┐
│                         Vera Engagement Bot                         │
│                                                                    │
│   HTTP (FastAPI + Uvicorn, single worker)                           │
│   ┌──────────────────────────────────────────────────────────┐     │
│   │ /v1/healthz  /v1/metadata  /v1/context                  │     │
│   │ /v1/tick     /v1/reply     /v1/teardown                 │     │
│   └────────┬─────────────────────────────────────────┬───────┘     │
│            │                                         │             │
│     Schemas (Pydantic v2)                    Error handlers        │
│            │                                   (400/409 JSON)      │
│   ┌────────▼─────────┐                                             │
│   │  ContextStore    │   asyncio.Lock + version checks             │
│   │  (in-memory)     │                                             │
│   └────────┬─────────┘                                             │
│            │                                                       │
│   ┌────────▼──────────┐   ┌────────────────────────┐               │
│   │ ContextAggregator │──▶│ format_context_for_    │               │
│   │ (4-context bundle)│   │   prompt (markdown)    │               │
│   └────────┬──────────┘   └───────────┬────────────┘               │
│            │                          │                            │
│   ┌────────▼──────────────────────────▼──────┐                     │
│   │          EngagementComposer (composer_v1)│                     │
│   │  ┌────────────┐  ┌─────────────────────┐ │                     │
│   │  │prompt libs │─▶│ LLMProvider (plug)  │ │                     │
│   │  │(per kind)  │  └─────────┬───────────┘ │                     │
│   │  └────────────┘            │             │                     │
│   │  post-LLM validators  ◀────┘             │                     │
│   │  (URL strip, length, repeat, intent)     │                     │
│   └──────────────────────────────────────────┘                     │
│            │                                                       │
│   ┌────────▼────────┐                                              │
│   │ ConversationStore│  per-conversation turn log + metadata       │
│   └─────────────────┘                                              │
└────────────────────────────────────────────────────────────────────┘
        │
        └──▶ External LLM: Gemini / OpenRouter / Groq / Together /
             Perplexity / Ollama / Dummy   (swappable via .env)
```

---

## 4. Tech stack at a glance

| Layer | Choice | Why |
|-------|--------|-----|
| Language | **Python 3.11+** | Fast iteration, first-class async, ubiquitous in the AI toolchain. |
| HTTP | **FastAPI + Uvicorn (ASGI)** | Async-native, automatic OpenAPI, pydantic integration. |
| Schemas | **Pydantic v2** (`Literal` unions, `Field` constraints) | Compile-time-feeling validation; powers the 400 handler. |
| Concurrency | **`asyncio` + `asyncio.Lock` + `asyncio.to_thread`** | Synchronous LLM SDKs wrapped in a thread for non-blocking I/O. |
| LLM clients | **`requests`** (single common HTTP client) | One dependency; retry/timeout logic lives in our code, not the SDK. |
| Config | **`python-dotenv`** + a frozen `Settings` dataclass | Immutable process snapshot; `.env` for dev, real env vars for prod. |
| Testing | **pytest + httpx TestClient** | Contract-level tests hit the full ASGI stack. |
| Packaging | **`pyproject.toml`** + Python module layout (`python -m vera_bot`) | Standard, reproducible, no custom scripts. |
| Container | **Dockerfile (slim, non-root, healthcheck)** | Drop-in for the judge's infra. |
| Observability | `logging_config.py` — structured INFO logs | Flags (`urls_stripped`, `truncated`, `fallback`) logged per action. |

---

## 5. Module map (what lives where)

```
vera_bot/
├── __init__.py            # __version__ = "0.2.1"
├── __main__.py            # `python -m vera_bot` → uvicorn with HOST/PORT
├── config.py              # Settings dataclass, COMPOSER_VERSION, caps
├── logging_config.py      # idempotent root-logger setup
├── adapter.py             # ContextAggregator + format_context_for_prompt
│
├── api/
│   ├── app.py             # 5 endpoints + /v1/teardown + middleware
│   ├── schemas.py         # Pydantic request/response models
│   └── errors.py          # 400 handler with bytes-safe serialization
│
├── store/
│   ├── context_store.py       # async-locked versioned KV, 4 scopes
│   └── conversation_store.py  # per-conversation turn log + metadata
│
└── composer/
    ├── base.py            # EngagementComposer (compose + compose_reply)
    ├── prompts.py         # per-trigger.kind prompt library (8 + default)
    ├── validators.py      # URL / length / repeat / intent / hard-no
    └── providers/
        ├── base.py
        ├── _openai_compat.py   # shared base for 4 providers
        ├── _retry.py           # exponential backoff on transient 5xx
        ├── factory.py          # provider selection
        ├── gemini.py           # Google REST v1beta
        ├── openrouter.py       # OpenAI-compatible
        ├── perplexity.py       # OpenAI-compatible
        ├── groq.py             # OpenAI-compatible
        ├── together.py         # OpenAI-compatible
        ├── ollama.py           # local /api/chat
        └── dummy.py            # canned response (tests)
```

---

## 6. The 5 endpoints — contract summary

| Method | Path | Purpose | Key codes |
|--------|------|---------|-----------|
| GET | `/v1/healthz` | Liveness + context counts | 200 |
| GET | `/v1/metadata` | Team/model identity (stable `submitted_at`) | 200 |
| POST | `/v1/context` | Push a versioned context record | 200 / 400 / 409 |
| POST | `/v1/tick` | Produce up to **20** proactive actions | 200 |
| POST | `/v1/reply` | Decide reply: `send` / `wait` / `end` | 200 |
| POST | `/v1/teardown` | Clear all in-memory state | 200 |

Spec source of truth: `challenge-testing-brief.md §2`.

### Error semantics

- **409 `stale_version`** — `/v1/context` with a `version` not greater
  than the stored one. Body: `{accepted:false, reason, current_version}`.
- **400 `invalid_payload`** — malformed body or unknown scope. Body:
  `{accepted:false, reason, details:{errors:[...]}}`. Errors are
  scrubbed of raw `bytes` / `ctx` so the JSON response always
  serializes (regression in v0.2.1).
- **400 `payload_too_large`** — >500 KB on `/v1/context` (middleware).

---

## 7. Request lifecycles (tell this story in an interview)

### 7.1 Proactive flow — `/v1/tick`

1. Judge pushes category, merchant, (optional) customer, and trigger
   contexts via `/v1/context`. Each is versioned; stale updates 409.
2. Judge POSTs `/v1/tick` with `{now, available_triggers:[...]}`.
3. `_order_triggers` sorts by **urgency DESC** and drops any whose
   `expires_at ≤ now`, then the loop caps at 20.
4. For each trigger: `ContextAggregator` assembles the 4-context
   bundle, which is rendered to markdown.
5. `EngagementComposer.compose`:
   a. Selects the system prompt via `select_prompt(trigger.kind, ...)`.
   b. Calls the LLM through `asyncio.to_thread` wrapped in an 8 s
      `asyncio.wait_for` (vs the 10 s judge budget).
   c. Parses JSON (tolerant to ```json fences and Perplexity-style
      prose wrappers).
   d. Runs validators: URL strip, sentence-boundary truncation at 320
      chars, verbatim-repeat suppression.
6. Handler appends to `ConversationStore` and emits a `TickAction`
   with: `conversation_id`, `template_name=vera_{kind}_v1`, rich
   `template_params` (customer, merchant, locality, metric, kind), a
   trigger-scoped `suppression_key`, `cta`, and composer rationale.

### 7.2 Reactive flow — `/v1/reply`

Decision tree (short-circuits before hitting the LLM):

```
auto-reply (e.g. "OOO, back Monday")  → action=end
explicit hard-no ("stop", "band karo")→ action=end
turn_number ≥ 6 without engagement    → action=end
explicit intent lock ("yes, tell me") → action=send (action mode)
otherwise                             → LLM with chat history
```

After LLM, the same validators run; a verbatim repeat flips the action
to `end` rather than looping.

---

## 8. The composer in detail

### 8.1 Per-trigger prompt routing

`composer/prompts.py::select_prompt(kind, is_customer_facing, is_reply, has_chat_history)`
returns `(system_prompt, header_hint)` for eight trigger kinds plus a
default:

| Kind | Stance |
|------|--------|
| `research_digest` | Share one data insight relevant to the merchant's category. |
| `perf_dip` | Empathetic diagnosis + one actionable lever. |
| `perf_spike` | Warm acknowledgement + question to sustain the trend. |
| `recall_due` | Nudge with loyalty angle. |
| `competitor_opened` | Neutral awareness + defensive tactic. |
| `festival_upcoming` | Timing-aware, concrete offer hook. |
| `dormant_with_vera` | Re-engagement; low pressure. |
| `milestone_reached` | Celebratory, next-goal oriented. |
| `default` | Generic warm nudge. |

Why per-kind prompts instead of one mega-prompt? **Specificity wins on
the rubric**: the judge tests each kind separately, so a focused 8-shot
library outperforms a jack-of-all-trades system prompt under a 10 s
budget.

### 8.2 Post-LLM validators (compliance belt)

| Check | Action on fail |
|-------|----------------|
| URLs / bare domains | Regex-stripped in place. |
| Length > 320 chars | Truncated at the nearest sentence boundary. |
| Verbatim repeat of a prior outbound | `/v1/tick`: suppress (empty body → action dropped). `/v1/reply`: flip to `action=end`. |
| Auto-reply pattern in inbound | Short-circuit to `end`. |
| Hard-no pattern (EN + Hindi + Devanagari) | Short-circuit to `end`. |
| Explicit intent pattern | Flag → composer moves to action/confirmation mode. |

The Hindi regexes intentionally avoid `\b` because Python's word
boundary doesn't match Devanagari script reliably.

### 8.3 Multi-provider layer

- Seven providers behind a two-method `LLMProvider` protocol
  (`name`, `generate(system, user) → str`).
- Four of them (OpenRouter, Perplexity, Groq, Together) are
  OpenAI-compatible and share `_openai_compat.py` — adding another such
  endpoint is a ~5-line file.
- **Retry**: `_retry.py` decorator — 2 attempts, 0.5 s / 1.5 s backoff,
  only for transient `5xx` / connection errors (never for 4xx).
- **Fallback**: on provider exception, composer returns a deterministic
  fallback `Composition` flagged `fallback` so the endpoint never
  500s.

---

## 9. State management

Everything is in-memory — the spec explicitly bounds the test session —
but laid out so it can be swapped for Redis / Snowflake tables without
touching the composer:

- `ContextStore`: `dict[(scope, context_id) → record]`; writes go
  through `asyncio.Lock`; `put()` returns `(accepted, current_version)`
  so the endpoint handler can choose 200 vs 409.
- `ConversationStore`: per `conversation_id`, stores the ordered turn
  log plus the **metadata** needed to rebuild the context bundle on
  `/v1/reply` (`merchant_id`, `customer_id`, `trigger_id`, `send_as`).
  This is why the reply path doesn't have to guess which merchant a
  conversation belongs to.

Single uvicorn worker is enforced — the `__main__.py` entry point
passes `workers=1`. Multi-worker would shard state silently.

---

## 10. Key engineering decisions & trade-offs

| Decision | Alternative considered | Why we chose this |
|----------|-----------------------|-------------------|
| In-memory stores | Redis / SQLite | Spec lifecycle is one test session; zero-dep is easier to evaluate. |
| Sync LLM clients in a thread | Native async clients per provider | Uniform interface, easier retry/timeout, no dependency on provider async SDKs. |
| Per-kind prompt library | One mega-prompt with kind as variable | Judging is per-kind; specificity wins. |
| Literal-typed `scope` | str | pydantic emits spec-compliant 400s automatically. |
| Post-LLM validator belt | Trust the LLM / prompt-only compliance | Defense in depth — compliance is scored, so we don't gamble. |
| `suppression_key` = composer_key + trigger_id | Just kind | Stops repeat-fires from colliding across triggers of the same kind. |
| Urgency-aware trigger ordering | FIFO | When the judge submits >20 triggers, we keep the highest-urgency ones. |
| Composer version string in rationale | Silent prompt updates | Auditability — the judge can bucket outputs by `composer_v1`, `v2`, … |

---

## 11. Observability

- INFO-level log on every produced action listing composer flags
  (`urls_stripped`, `truncated`, `fallback`, `repeat_suppressed`).
- Structured log format via `logging_config.py` (idempotent — safe to
  import multiple times).
- `/v1/healthz` exposes per-scope context counts so the judge can
  verify priming before triggering ticks.

---

## 12. Test strategy

33 tests, all green, run in ~0.3 s:

| Suite | Covers |
|-------|--------|
| `test_api_contract.py` | 200/400/409 semantics, 20-cap, urgency ordering, suppression-key uniqueness, bytes-body safe 400, reply shape (`send`/`wait`/`end`), teardown. |
| `test_validators.py` | URL strip, length cap, repeat detection, intent detection (incl. Hindi), auto-reply, hard-no. |
| `test_composer_routing.py` | Correct prompt selected per kind; composer version stamping. |
| `test_provider_factory.py` | Explicit vs auto-selected provider matrix incl. Ollama and dummy fallback. |

`tests/conftest.py` pins `LLM_PROVIDER=dummy` so the suite needs zero
network access and zero credentials.

---

## 13. What I'd tackle next (stretch roadmap)

These are deliberately **not** in the current submission because they
exceed the brief, but they are the natural next steps:

1. Persist `ConversationStore` to Redis / KV so a replay across process
   restarts works.
2. Promote `ContextStore` to a Snowflake table with row-level versioning
   for true multi-merchant scale.
3. Golden-output evaluator: for each test pair, run the composer and
   compare against a blessed reference using an LLM-as-judge grader.
4. Prompt A/B harness: flip `COMPOSER_VERSION`, run the same trace
   against two versions, diff the rationales.
5. Structured tracing (OpenTelemetry) per request so prompt latency,
   retry counts, and validator flags show up in one span tree.

---

## 14. Talk-track cheatsheet

Use these when walking an interviewer through the repo live:

1. **Start at** `vera_bot/api/app.py` — it is small and shows the full
   request lifecycle end-to-end.
2. **Then** `composer/base.py::compose` to show the LLM orchestration
   pattern (`asyncio.to_thread` + `wait_for` + post-validation).
3. **Then** `composer/prompts.py` — the per-kind dispatch is where most
   of the *product* thinking lives.
4. **Then** `composer/validators.py` — highlight the compliance belt;
   mention the Devanagari / `\b` gotcha.
5. **Finish** in `tests/test_api_contract.py` to show the contract is
   codified, not just documented.

Each stop is ≤ 30 lines of code.

---

## 15. Files to skim before the interview

| File | Why |
|------|-----|
| `docs/RUNNING.md` | You can answer "how do I run it?" in 30 seconds. |
| `docs/ARCHITECTURE.md` | Per-module walkthrough; supports deep dives. |
| `docs/CHANGELOG.md` | Shows you ship in small, auditable slices. |
| `docs/COMPLIANCE.md` | Spec-checklist → code location matrix. |
| `docs/PROMPTS.md` | The prompt library, catalogued per kind. |
| `README.md` | Quickstart + trade-offs + "context I would have liked". |
