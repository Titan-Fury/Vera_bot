# Vera Engagement Bot

magicpin AI Challenge submission — an HTTP bot (FastAPI) that composes
WhatsApp-length messages for magicpin merchants from a four-context bundle:

```
Category × Merchant × Trigger × (optional) Customer
```

The bot exposes the six endpoints in
[`challenge-testing-brief.md`](challenge-testing-brief.md), uses a pluggable
LLM provider stack with automatic fallback, and ships two key performance
optimizations (precompute-on-ingest + deterministic template bypass) that cut
`/v1/tick` latency from ~54 s to <0.1 s on the 25-trigger dataset.

---

## Table of contents

1. [Architecture](#architecture)
2. [Quickstart](#quickstart)
3. [Configuration](#configuration)
4. [HTTP endpoints](#http-endpoints)
5. [Feature walkthrough](#feature-walkthrough)
6. [Running the judge simulator](#running-the-judge-simulator)
7. [Tests](#tests)
8. [Docker](#docker)
9. [Project layout](#project-layout)
10. [Trade-offs](#trade-offs)

---

## Architecture

```
judge  ──HTTP──▶  FastAPI app  ─┬─▶ ContextStore      (scope,id) → versioned payload
                                ├─▶ ConversationStore  history + suppression keys
                                ├─▶ ContextAggregator  merge 4 scopes → bundle
                                ├─▶ Compose cache      asyncio.Task per trigger_id
                                │                      (precompute-on-ingest)
                                │
                                └─▶ EngagementComposer (composer_v1)
                                     ├─ try_template_compose()   (deterministic bypass)
                                     ├─ select_prompt(kind, audience, is_reply)
                                     ├─ LLMProvider.generate()   (pluggable)
                                     │    └─ fallback chain: primary → tier-2 → tier-3
                                     └─ post-LLM validators:
                                         strip_urls · enforce_length(320) ·
                                         is_repeat · detect_{auto_reply,hard_no,intent}
```

Module map: [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)
Prompt library: [`docs/PROMPTS.md`](docs/PROMPTS.md)
Spec compliance: [`docs/COMPLIANCE.md`](docs/COMPLIANCE.md)
Testing recipes: [`docs/TESTING.md`](docs/TESTING.md)
Interview overview: [`docs/PROJECT_OVERVIEW.md`](docs/PROJECT_OVERVIEW.md)

---

## Quickstart

> **Want to test without spending money?** See [`docs/TESTING.md`](docs/TESTING.md)
> for three zero-cost setups (Dummy-only, Ollama-local, and Groq bot + Ollama judge).

### 1. Install

```bash
python -m venv venv
venv\Scripts\activate            # Linux/macOS: source venv/bin/activate
pip install -r requirements.txt
```

### 2. Configure (set at least one LLM key)

```bash
copy .env.example .env           # Linux/macOS: cp .env.example .env
# then edit .env and paste a key for any one provider (OpenRouter is free-tier)
```

### 3. Run

```bash
python -m vera_bot               # serves on 0.0.0.0:8080
```

Alternatively with uvicorn directly:

```bash
python -m uvicorn vera_bot.api.app:app --host 0.0.0.0 --port 8080 --workers 1
```

### 4. Verify

```bash
curl http://localhost:8080/v1/healthz
curl http://localhost:8080/v1/metadata
```

Expected healthz response:

```json
{
  "status": "ok",
  "uptime_seconds": 3,
  "contexts_loaded": {"merchant": 0, "trigger": 0, "category": 0, "customer": 0},
  "composer_version": "composer_v1",
  "provider": "fallback[openrouter-gemma->openrouter->openrouter-gemma]"
}
```

---

## Configuration

All settings live in `.env`. Template is in `.env.example`.

### Provider matrix

| Provider         | Env var              | Default model                             | Notes                    |
|------------------|----------------------|-------------------------------------------|--------------------------|
| `openrouter-gemma` | `OPENROUTER_API_KEY` | `google/gemma-3n-e4b-it:free`             | merged-system prompt     |
| `openrouter`       | `OPENROUTER_API_KEY` | `openai/gpt-oss-20b:free`                 | standard chat/completions |
| `gemini`           | `GEMINI_API_KEY`     | `gemini-1.5-flash`                        |                          |
| `perplexity`       | `PERPLEXITY_API_KEY` | `sonar`                                   |                          |
| `groq`             | `GROQ_API_KEY`       | `llama-3.3-70b-versatile`                 | separate quota pool      |
| `together`         | `TOGETHER_API_KEY`   | `meta-llama/Llama-3.3-70B-Instruct-Turbo` |                          |
| `ollama`           | `OLLAMA_HOST`        | `llama3`                                  | local                    |
| `dummy`            | _(default)_          | n/a — returns fixed JSON                  | spec-compliant offline   |

### Default fallback chain (shipped in `.env.example`)

```
LLM_PROVIDER=openrouter-gemma
LLM_MODEL=google/gemma-3n-e4b-it:free
LLM_FALLBACKS=openrouter:openai/gpt-oss-20b:free|openrouter-gemma:google/gemma-3-12b-it:free
```

Primary hits Gemma-3n-e4b (~2.5 s avg, 25% faster than `gpt-oss-20b`). On any
HTTP error, timeout, or null content the adapter auto-fails over to tier-2
then tier-3. Gemma models need the "merged-system" prompt shape (they reject
`role:"system"` with HTTP 400), which is why `openrouter-gemma` is a distinct
provider key from `openrouter`.

### Other env knobs

| Variable              | Default  | Purpose                                       |
|-----------------------|----------|-----------------------------------------------|
| `HOST`                | `0.0.0.0`| Bind address                                  |
| `PORT`                | `8080`   | Bind port                                     |
| `LOG_LEVEL`           | `INFO`   | Python logger level                           |
| `TICK_CONCURRENCY`    | `2`      | Max parallel composes inside one `/v1/tick`   |
| `TEAM_NAME`           | —        | Returned by `/v1/metadata`                    |
| `TEAM_MEMBERS`        | —        | Comma-separated, returned by `/v1/metadata`   |
| `CONTACT_EMAIL`       | —        | Returned by `/v1/metadata`                    |
| `APPROACH`            | —        | Returned by `/v1/metadata`                    |

---

## HTTP endpoints

All paths are prefixed with `/v1`. Full request/response shapes in
[`challenge-testing-brief.md §3`](challenge-testing-brief.md).

| Method | Path             | Purpose                                                   |
|--------|------------------|-----------------------------------------------------------|
| GET    | `/v1/healthz`    | Liveness + counts of stored contexts + provider chain     |
| GET    | `/v1/metadata`   | Team name, model, approach, submission version            |
| POST   | `/v1/context`    | Ingest category / merchant / customer / trigger payload   |
| POST   | `/v1/tick`       | Batch-compose actions for `available_triggers` list       |
| POST   | `/v1/reply`      | Generate send/wait/end decision for an inbound message    |
| POST   | `/v1/teardown`   | Clear all in-memory state (idempotent)                    |

### Typical lifecycle

```
POST /v1/context   scope=category  payload={dentists}
POST /v1/context   scope=merchant  payload={merchant_001}
POST /v1/context   scope=customer  payload={customer_042}       (optional)
POST /v1/context   scope=trigger   payload={trg_recall_due_001}
                          │
                          └─▶ fires background compose task if merchant is already stored
POST /v1/tick      available_triggers=[trg_recall_due_001, ...]
                          │
                          └─▶ awaits cached task → <100 ms per action
POST /v1/reply     conversation_id=... message="YES"
POST /v1/teardown  → clears stores and cancels pending cache tasks
```

See [`examples/api-call-examples.md`](examples/api-call-examples.md) for
concrete curl payloads.

---

## Feature walkthrough

### 1. Four-context aggregation

`ContextAggregator.build(trigger_id=...)` joins the stored category, merchant,
optional customer, and trigger payloads into a single `ContextBundle`. The
bundle is what every composer sees; missing scopes are simply `None` and the
prompt selector adapts. See [`vera_bot/adapter.py`](vera_bot/adapter.py).

### 2. Trigger-kind prompt routing

The composer inspects `bundle.trigger["kind"]` and picks from a per-kind
prompt library in [`vera_bot/composer/prompts.py`](vera_bot/composer/prompts.py).
Each prompt encodes the brief's HARD RULES: ≤320 chars, exactly one binary CTA,
no URLs, English unless the customer payload says otherwise, and an anchor on
at least one concrete fact from the payload.

### 3. Deterministic template bypass

Seven trigger kinds — `appointment_tomorrow`, `chronic_refill_due`,
`recall_due`, `perf_spike`, `perf_dip`, `festival_upcoming`,
`milestone_reached` — are mechanical reminders or numeric announcements
where an LLM adds no value. These go through
[`vera_bot/composer/templates.py`](vera_bot/composer/templates.py) and
produce deterministic f-string output in microseconds. Still
length-validated and repeat-suppressed like any other path. ~41% of the
spec dataset bypasses the LLM entirely, which also insulates the bot from
upstream rate limits for those kinds.

### 4. Precompute-on-ingest cache

When `/v1/context` accepts a trigger whose merchant is already stored (or a
merchant whose pending triggers are already known), the handler fires
`asyncio.create_task(compose_and_cache(trigger_id))`. The task runs in the
background during the 5-minute judge inter-tick window. When `/v1/tick`
arrives it simply awaits the ready result.

- Brief-compliant: "deterministic given same inputs" (§277) and "any LLM, any
  prompting strategy" (§276) explicitly allow precompute.
- Real uvicorn has a single persistent event loop, so tasks live across
  requests. (FastAPI `TestClient` spins a loop per request; that specific
  `ValueError: future belongs to different loop` case is caught silently.)
- Cache keyed on `trigger_id`; cleared on `/v1/teardown`.

Measured impact on the 25-trigger dataset: `/v1/tick` went from **~54 s → 0.07 s**.

### 5. Pluggable LLM with fallback chain

[`vera_bot/composer/providers/`](vera_bot/composer/providers/) ships one
provider per API. `factory.make_provider()` reads `LLM_PROVIDER`,
`LLM_MODEL`, and `LLM_FALLBACKS` and wraps them in
[`fallback.FallbackProvider`](vera_bot/composer/providers/fallback.py), which
tries each in order on HTTP error / timeout / null content. The chain is
reported verbatim by `/v1/healthz` so you can see what's live.

### 6. Post-LLM validators

Every draft — templated or LLM-generated — runs through
[`vera_bot/composer/validators.py`](vera_bot/composer/validators.py):

- `enforce_length(text, 320)` — sentence-truncates, doesn't reject
- `strip_urls(text)` — removes http(s) links
- `is_repeat(text, prior_outbound)` — suppresses verbatim repeats
- `detect_auto_reply(msg)` — flags bot-like inbound for `/v1/reply`
- `detect_hard_no(msg)` / `detect_intent(msg)` — drive send/wait/end decisions

Validators coerce rather than reject to stay under the 10 s budget; flags
are preserved in the composition's `flags` list for the audit trail.

### 7. Tick-level parallelism

Inside a single `/v1/tick`, compositions run through an
`asyncio.Semaphore(TICK_CONCURRENCY)` so we parallelize the per-trigger work
while respecting upstream free-tier rate limits.

### 8. Ordering, expiry, and action cap

`/v1/tick` sorts incoming triggers by `urgency` DESC, drops any with
`expires_at <= now`, and caps the response at 20 actions per the spec.

---

## Running the judge simulator

The simulator is a standalone script that ingests the dataset, drives the bot
via HTTP, and uses its own LLM to grade each message on five dimensions.

```bash
# 1. Start the bot
python -m vera_bot

# 2. (in another shell) edit judge_simulator.py top-of-file block:
#    - BOT_URL         = "http://localhost:8080"
#    - LLM_PROVIDER    = "groq"  (or openai, anthropic, gemini, openrouter, ollama)
#    - LLM_API_KEY     = "<your key>"
#    - LLM_MODEL       = "llama-3.3-70b-versatile"  (recommended for groq)
#    - TEST_SCENARIO   = "full_evaluation"  (or warmup / all / hostile / ...)

python judge_simulator.py
```

On Windows, prefix with `set PYTHONIOENCODING=utf-8` so the judge's block
glyphs render in `cp1252` consoles:

```bash
set PYTHONIOENCODING=utf-8
python judge_simulator.py
```

The judge reports per-message scores (Specificity, Category Fit, Merchant Fit,
Trigger Relevance, Engagement) and a final average out of 50.

### Regenerating the dataset deterministically

```bash
python dataset\generate_dataset.py --seed-dir dataset --out dataset
```

---

## Tests

```bash
pytest -q
```

Coverage:

- HTTP contract (`tests/test_api_contract.py`) — healthz, metadata, context
  200/409/400, tick cap + template params, reply action mapping, teardown
- Validators (`tests/test_validators.py`) — length, URL stripping, repetition,
  hard-no, intent, auto-reply
- Prompt routing (`tests/test_composer_routing.py`) — per trigger kind
- Provider factory (`tests/test_provider_factory.py`) — selection + fallback

---

## Docker

```bash
docker build -t vera-bot .
docker run -p 8080:8080 --env-file .env vera-bot
```

The image runs as a non-root user with a healthcheck against `/v1/healthz`.

---

## Project layout

```
magicpin_challenge/
├─ vera_bot/                    application code
│  ├─ api/app.py                FastAPI endpoints
│  ├─ api/schemas.py            pydantic request/response models
│  ├─ composer/base.py          EngagementComposer + Composition dataclass
│  ├─ composer/templates.py     deterministic template bypass
│  ├─ composer/prompts.py       per-trigger-kind prompt library
│  ├─ composer/validators.py    length / URL / repeat / intent / auto-reply
│  ├─ composer/providers/       one module per LLM API + fallback wrapper
│  ├─ store/context_store.py    scope+id → versioned payload
│  ├─ store/conversation_store.py  history + suppression keys
│  ├─ adapter.py                ContextAggregator (4-context merge)
│  ├─ config.py                 env-driven constants + COMPOSER_VERSION
│  └─ __main__.py               python -m vera_bot entrypoint
│
├─ dataset/                     spec-provided fixtures + deterministic generator
│  ├─ categories/  merchants/  customers/  triggers/
│  └─ generate_dataset.py
│
├─ docs/                        architecture, prompts, compliance, testing
├─ examples/                    curl recipes + case studies
├─ scripts/build_submission_jsonl.py   offline batch-compose utility
├─ tests/                       pytest suite
│
├─ judge_simulator.py           LLM-graded judge harness
├─ validate_submission.py       sanity-check a submission.jsonl
├─ Dockerfile                   non-root, healthcheck wired
├─ requirements.txt             pinned deps
├─ pyproject.toml               project metadata
├─ .env.example                 config template
└─ README.md                    this file
```

---

## Trade-offs

- **In-memory state, single worker**: the spec allows in-process state.
  Cross-worker replication would add complexity with no scoring benefit.
  Consequence: `uvicorn --workers 1` (the `python -m vera_bot` entrypoint
  enforces this).

- **Static `composer_v1`**: all current prompts live under one version tag.
  Bumping to `composer_v2` is intentionally a one-line source change.

- **Provider fallback to Dummy** when no keys are configured. The dummy stays
  spec-compliant (valid JSON, ≤320 chars, no URLs) so CI and smoke tests
  don't need network.

- **Validators coerce rather than reject**. A 380-char message is
  sentence-truncated to ≤320; a URL is stripped. The alternative
  (reject-and-retry) adds p99 latency against a 10 s budget.

- **Template bypass vs. LLM everywhere**. Three mechanical kinds go through
  deterministic templates. The brief endorses this (§276, §277). It saves
  tokens, money, latency, and survives upstream rate limits.

### What extra context would have helped

- A structured list of banned / sensitive phrases per category (beyond the
  `taboos` array in the category context).
- A stable per-merchant "mood" or recent-complaint flag to tune tone.
- Sample pass/fail judge transcripts so validator thresholds can be
  calibrated empirically rather than conservatively.
