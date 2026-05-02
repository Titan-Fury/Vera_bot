# Architecture

Module-by-module walkthrough. All paths are relative to repo root. Spec refs
are to `challenge-testing-brief.md` (CTB) and `engagement-design.md` (ED).

## Process layout

```
python -m vera_bot
   → vera_bot/__main__.py          (configures logging, launches uvicorn)
      → vera_bot/api/app.py        (FastAPI app; 6 endpoints)
         ├─ vera_bot/store/         in-memory state (async-locked)
         ├─ vera_bot/adapter.py     4-context bundle builder + prompt formatter
         └─ vera_bot/composer/      per-kind prompt dispatch + validators + providers
```

Single-worker by contract. All state is in-process; `/v1/teardown` wipes both
stores.

## vera_bot/config.py

Typed frozen `Settings` dataclass loaded once at import. Reads from env (and
optional `.env`). Exposes protocol constants:

| Constant                      | Value        | Spec ref       |
|-------------------------------|--------------|----------------|
| `COMPOSER_VERSION`            | `composer_v1`| ED §Composer   |
| `MAX_BODY_CHARS`              | 320          | CTB §10        |
| `TICK_ACTION_CAP`             | 20           | CTB §5         |
| `CONTEXT_PAYLOAD_CAP_BYTES`   | 500 KB       | CTB §5         |
| `DEFAULT_COMPOSE_TIMEOUT_S`   | 8.0 s        | CTB §5 (10 s)  |

`SETTINGS.submitted_at` is captured at process start so `/v1/metadata` is
stable across requests.

## vera_bot/api/

- `app.py` — FastAPI instance and wiring (store, aggregator, provider,
  composer). Implements all 6 endpoints. `tick` and `reply` push composition
  into a thread via `asyncio.to_thread` with an 8 s `wait_for` so the 10 s
  per-call judge budget is respected.
- `schemas.py` — Pydantic models for requests/responses. `CtxBody.scope` is a
  `Literal`, so unknown scopes fail at validation → 400.
- `errors.py` — `RequestValidationError` handler → 400 with
  `{accepted:false, reason:"invalid_payload", details:{errors:[...]}}`.

Middleware `_payload_cap` enforces the 500 KB body size cap on `/v1/context`
via `Content-Length` pre-check.

## vera_bot/store/

- `context_store.py` — async-locked dict keyed by `(scope, context_id)`.
  `put()` returns `(accepted, current_version)` — higher version always wins;
  equal-or-lower version yields `accepted=False`, surfaced as HTTP 409 in the
  API layer.
- `conversation_store.py` — append-only conversation log with per-conv
  metadata (`merchant_id`, `customer_id`, `trigger_id`, last `send_as`).
  Used by `/v1/reply` to rebuild bundles without guessing.

## vera_bot/adapter.py

- `ContextAggregator.build(trigger_id, merchant_id, customer_id)` pulls from
  the store and assembles the four-context bundle per ED §Composer. When
  `merchant_id` isn't supplied, it's extracted from the trigger payload.
  Category is resolved via `merchant.category_slug`.
- `format_context_for_prompt(bundle)` serializes the bundle into the compact
  string fed to the LLM.

## vera_bot/composer/

- `prompts.py` — `select_prompt(trigger_kind, is_customer_facing, is_reply)`
  returns `(system_prompt, header_hint)`. Eight kind blocks (plus `default`),
  each with its own persuasion lever; see `docs/PROMPTS.md`.
- `validators.py` — post-LLM coercion: `strip_urls`, `enforce_length`
  (sentence-boundary truncation at 320), `is_repeat` (verbatim match in
  conversation history), `detect_auto_reply`, `detect_hard_no`,
  `detect_explicit_intent`.
- `base.py` — `EngagementComposer.compose` (proactive) and `compose_reply`.
  Reply path is a decision tree:
  1. auto-reply detected → `action=end`
  2. hard-no detected → `action=end`
  3. `turn_number ≥ 6` → `action=end`
  4. intent lock → prompt adds "move to action mode"
  5. else → LLM with reply mode prompt
  Output is always validated; verbatim repeats fall back to `action=end`.
- `providers/` — `LLMProvider` abstract base; one subclass per vendor; shared
  `_openai_compat.py` for OpenRouter/Perplexity/Groq/Together; `_retry.py` for
  2-attempt exponential backoff on transient failures; `factory.make_provider`
  picks the right one based on `Settings`.

## Request flows

### /v1/tick
```
for trigger_id in body.available_triggers[:20]:
    bundle = aggregator.build(trigger_id=...)
    if no merchant: skip
    comp = composer.compose(bundle, formatted)          # 8 s budget
    comp.body -> validate_outbound (URLs, length)
    comp.body -> is_repeat? rephrase once
    conversation_store.record_outbound(...)
    append action
```

### /v1/reply
```
conv = conversation_store.record_inbound(...)
bundle = aggregator.build(conv.meta.trigger_id, ...)    # trigger_id from store
comp = composer.compose_reply(bundle, ..., history, msg, turn_number)
if comp.action == "send": record_outbound(); return
if comp.action == "wait": return {wait_seconds}
if comp.action == "end":  return (no body)
```

## Concurrency

FastAPI handlers are `async`. The blocking LLM `requests.post` call is
dispatched via `asyncio.to_thread`, yielding the event loop while the HTTP
round-trip is in flight. `ContextStore` and `ConversationStore` wrap all
writes in an `asyncio.Lock`.

## Logging

`logging_config.configure_logging()` runs once at import time. Level from
`LOG_LEVEL` env (default `INFO`). Formatter: `%(asctime)s %(levelname)s
%(name)s: %(message)s`.

## Failure modes

| Failure                      | Handling                                           |
|------------------------------|----------------------------------------------------|
| Provider timeout             | `asyncio.wait_for` at 8 s drops that tick action   |
| Provider 5xx / connection    | `_retry.with_retries` (2 attempts, 0.5/1.5 s)      |
| Provider 429 with Retry-After| Tier marked cold via circuit breaker (ADR-005)     |
| JSON parse error on response | `CompositionJSON` pydantic retry once, then fallback (ADR-001) |
| Empty body post-validation   | Action is dropped (not sent)                       |
| `compose_reply` exception    | Returns `{action:"end", rationale:...}`            |

---

## Architecture Decision Records

Short-form ADRs documenting non-obvious design choices. Each has Context
(what problem), Decision (what we did), Consequence (what it implies).

### ADR-001 — Pydantic-validated LLM output with budgeted retry

- **Context**: LLMs occasionally emit JSON with extra prose, wrong field
  names, or invalid enum values. Brief §10 penalises malformed responses
  (-2 each). The tick deadline is 10 s total per action.
- **Decision**: Define `CompositionJSON` (Pydantic) with enum validators.
  On `ValidationError` or `JSONDecodeError`, re-prompt once with an explicit
  schema hint — but only if we have ≥ 2.5 s of budget left. Otherwise fall
  back immediately to the canned safe composition.
- **Consequence**: Lower silent-fallback rate; retries never push a tick
  over the 10 s judge timeout. Audit log records which path (`llm`,
  `fallback`, `template`) produced each action.

### ADR-002 — Template bypass for mechanical trigger kinds

- **Context**: 41 of the 100-trigger dataset are purely mechanical (recall
  reminders, perf deltas, milestones). LLMs add latency, variance, and
  rate-limit exposure without improving quality on these.
- **Decision**: `templates.py` dispatches 7 kinds (customer-facing:
  `appointment_tomorrow`, `chronic_refill_due`, `recall_due`;
  merchant-facing: `perf_spike`, `perf_dip`, `festival_upcoming`,
  `milestone_reached`) to f-string composers with full payload grounding.
  `festival_upcoming` with `days_until > 30` falls through to the LLM so
  the template doesn't claim urgency it can't justify.
- **Consequence**: Zero-cost, zero-latency, zero-hallucination for 41 %
  of triggers. Reserves LLM budget for triggers that genuinely need
  composition.

### ADR-003 — Precompute on context-push

- **Context**: The judge pushes trigger + merchant + customer contexts
  *before* calling `/v1/tick` (CTB §4 phase 2 order). Waiting for the
  tick to start composing wastes the available asynchronous headroom.
- **Decision**: Every time `/v1/context` arrives and completes a
  `(trigger, merchant)` bundle, kick off a background compose into a
  `_compose_cache` task map. `/v1/tick` reads the cached result first.
- **Consequence**: Hot path at tick time is a single `asyncio.wait_for`
  on a mostly-done task. Global `_compose_semaphore` caps concurrent
  upstream calls so precompute bursts don't hammer provider rate limits.

### ADR-004 — Global compose semaphore

- **Context**: Free-tier LLM APIs (Groq, Gemini) enforce RPM caps. A burst
  of simultaneous context pushes from the judge could fire 25+ composes
  at once, half of which would eat 429s.
- **Decision**: One process-wide `asyncio.Semaphore(TICK_CONCURRENCY)`
  shared by precompute *and* tick paths. Budget is one tunable env var.
- **Consequence**: Sustained throughput is capped but jitter-free; no
  single path can starve the other.

### ADR-005 — Circuit breaker with Retry-After

- **Context**: When a tier returns 429, the next ~25 parallel composes in
  the same tick would naively hit the same 429, log-spamming and wasting
  ~150 ms each on a network round-trip.
- **Decision**: `FallbackLLMProvider` reads the `Retry-After` header from
  the 429 response and marks that tier cold until the window closes (hard
  cap 300 s). Cold tiers are skipped without a network call. Falls
  through to the next tier in the chain.
- **Consequence**: Burst composes during a rate-limited window cost near
  zero extra latency. Metrics endpoint surfaces cold tier status for
  operator visibility.

### ADR-006 — Fallback log dedupe

- **Context**: A burst of 429s across a tick would historically log 25
  identical "fallback: groq failed" warnings.
- **Decision**: Per `(provider_name, error_class)` key, suppress repeat
  warnings within a 5 s window.
- **Consequence**: Signal preserved, noise gone. One log line per
  upstream incident type per 5 s.

### ADR-007 — Category voice blocks + taboo enforcement

- **Context**: Brief §11 penalises promotional tone on clinical
  categories and explicit taboo usage (e.g. "cure", "guaranteed" for
  dentists).
- **Decision**: `prompts.py::_CATEGORY_VOICE` injects per-vertical
  vocabulary into the system prompt. `validators.py::enforce_taboos`
  strips category-voice taboos from the final body as a defence in depth.
- **Consequence**: Category Fit dimension scores improve; taboo leakage
  becomes a zero-percent tail risk instead of a probabilistic one.

### ADR-008 — Offer-catalog grounding guard

- **Context**: LLMs readily invent plausible-sounding offers ("Diwali
  Glow Bundle @ ₹899") that don't exist in the merchant's catalog. Brief
  §5.8 prohibits this; judges penalise it on Specificity and Merchant
  Fit.
- **Decision**: Regex-match `Service @ ₹price` patterns in the output;
  any that don't appear in `category.offer_catalog` or
  `merchant.offers` are stripped. If stripping guts the body, replace
  with a canned "Want me to set up a matching offer from your catalog?"
  fallback.
- **Consequence**: Zero invented offers hit the wire. Operator sees
  `invented_offer_dropped` flag in the audit log when it fires.

### ADR-009 — Few-shot examples per trigger kind

- **Context**: Kind blocks previously described structure abstractly
  ("LEAD with the metric"). Instruction-following LLMs do markedly better
  with one concrete 9/10 example in-context.
- **Decision**: Each `_KIND_BLOCKS` entry now includes an
  `EXAMPLE (9/10): "..."` reference line after the structure description.
- **Consequence**: Engagement + Category Fit scores rise without
  increasing token cost materially (~30-50 tokens per block).

### ADR-010 — Language localization directive

- **Context**: Brief §5.7: "Hindi-English code-mix is fine — and often
  preferred for Indian merchant audiences. Match the merchant's
  language." The composer previously left this to the LLM to infer from
  the bundle.
- **Decision**: `select_prompt` now accepts a `languages` iterable. When
  it includes `hi`, `hinglish`, or similar, a natural-language directive
  is inserted telling the LLM to write Hinglish (roman-Hindi preferred,
  English for numbers / CTAs).
- **Consequence**: Merchant Fit axis gains on Indian-market merchants
  without affecting English-only merchants.

### ADR-011 — Fuzzy repeat detection

- **Context**: Brief §10 penalises "same body verbatim" but LLMs readily
  produce paraphrases that read identically. Exact-match `is_repeat`
  missed those.
- **Decision**: Normalise (lowercase, strip punctuation, collapse
  whitespace), then use `difflib.SequenceMatcher` ratio ≥ 0.92 as a
  fuzzy-repeat signal. On hit, action is dropped (tick path) or ended
  (reply path) — never a second LLM call.
- **Consequence**: Eliminates a class of subtle repeat penalties with no
  latency hit.

### ADR-012 — Payload-fact citation audit

- **Context**: Specificity is the highest-weight judge axis. Messages
  that don't anchor on a number or named entity from the bundle score
  poorly. Teaching the LLM once isn't enough; hallucinations slip.
- **Decision**: `has_payload_anchor(body, bundle)` extracts all numeric
  tokens and capitalised entity tokens from the bundle and checks
  overlap with the output. Mismatches flag `no_payload_anchor` in the
  audit log (warning, not a hard block).
- **Consequence**: Grounding-failure rate is observable per compose;
  prompt regressions show up immediately in the dashboard / metrics
  endpoint.

### ADR-013 — Operator observability (metrics + dashboard)

- **Context**: The required 5-endpoint spec surface gives reviewers no
  way to inspect the bot's decision path during a run.
- **Decision**: Add two read-only endpoints — `/v1/metrics` (JSON
  snapshot of compose counts, provider mix, fallback cascade, 429
  windows, p50/p95 latency, recent 20 composes) and `/dashboard` (a
  single self-contained HTML page with 5-second auto-refresh).
- **Consequence**: Zero additional runtime cost. Reviewer sees a
  production-shaped observability surface instead of guessing from log
  lines.
