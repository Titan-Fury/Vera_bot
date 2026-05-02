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
| JSON parse error on response | Fallback Composition with `[composer_v1] fallback` |
| Empty body post-validation   | Action is dropped (not sent)                       |
| `compose_reply` exception    | Returns `{action:"end", rationale:...}`            |
