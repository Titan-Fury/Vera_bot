# Testing Vera Without Spending Money

Three ways to exercise the bot end-to-end with zero spend. Pick one based on
what you want to verify.

| Setup | Cost | Speed/call | What it verifies |
|---|---|---|---|
| **A. Dummy-only** | $0 | ~instant | Contract, routing, validators, Deterministic template bypass |
| **B. Ollama local** | $0 (disk: ~4 GB) | ~5-8 s | Real end-to-end generation + LLM judge |
| **C. Mixed (recommended)** | $0 | ~1-3 s bot, ~5-8 s judge | Real bot quality + hermetic local judge |

---

## A. Dummy-only (zero network, fastest)

Instant, 100% offline. Every call returns a fixed, spec-compliant message via
`DummyLLMProvider`. The bot still exercises every pipeline stage: context
storage, ordering, validators, suppression, tick budget. Good for CI and
contract regression.

```bat
set LLM_PROVIDER=dummy
set LLM_FALLBACKS=
venv\Scripts\python.exe -m uvicorn vera_bot.api.app:app --host 127.0.0.1 --port 8080
```

The judge simulator scores Specificity structurally in this mode; the other
three judge metrics default to `5/10`. Use this to validate the **pipeline**,
not message quality.

> Note: the judge simulator itself still needs an LLM to grade. Set its
> constants (top of `judge_simulator.py`) to `LLM_PROVIDER = "ollama"` or
> to the Dummy bot running on the same port — the judge will emit a
> structural-only report either way.

---

## B. Ollama local (unlimited real LLM)

Fully offline once the model is downloaded. No rate limits, no keys, no
quotas. Same machine runs the bot, the judge, and the model server.

### 1. Install Ollama

- Windows / macOS: <https://ollama.com/download>
- Linux: `curl -fsSL https://ollama.com/install.sh | sh`

### 2. Pull a model

`qwen2.5:7b-instruct` is a strong free pick (~4.4 GB, good JSON adherence,
~5-8 s per call on a modern laptop CPU; <2 s on a 6+ GB GPU):

```bat
ollama pull qwen2.5:7b-instruct
```

Alternatives:
- `llama3.2:3b` — half the memory, ~2 s/call, noticeably weaker.
- `phi3:mini`  — 2.3 GB, fast, occasional JSON drift.

### 3. Point the bot at Ollama

```bat
set LLM_PROVIDER=ollama
set LLM_MODEL=qwen2.5:7b-instruct
set LLM_FALLBACKS=
set OLLAMA_HOST=http://localhost:11434
venv\Scripts\python.exe -m uvicorn vera_bot.api.app:app --host 127.0.0.1 --port 8080
```

### 4. Run the judge simulator, also on Ollama

The judge simulator is configured via constants at the top of
`judge_simulator.py` (lines 24-39). Edit these four values:

```python
BOT_URL = "http://localhost:8080"
LLM_PROVIDER = "ollama"
LLM_API_KEY = ""                         # ignored for ollama
LLM_MODEL = "qwen2.5:7b-instruct"
```

Then run:

```bat
set PYTHONIOENCODING=utf-8
venv\Scripts\python.exe judge_simulator.py
```

Tick latency with `ollama` as primary runs ~5-8 s per trigger. The template
bypass eliminates LLM calls entirely for 41% of the dataset
(`appointment_tomorrow`, `chronic_refill_due`, `recall_due`, `perf_spike`,
`perf_dip`, `festival_upcoming`, `milestone_reached`) so a 100-trigger run
finishes in under 10 minutes.

---

## C. Mixed: Groq bot + Ollama judge (recommended)

Fastest realistic setup. Bot uses Groq's free tier (30 RPM on
`llama-3.3-70b-versatile`, ~1-2 s/call, separate quota pool — unrelated to
OpenRouter). Judge runs locally on Ollama — never exposed to any remote
rate limit.

### 1. Get a fresh Groq key

Sign in at <https://console.groq.com> (Google/GitHub auth, 30 seconds). The
free tier needs no payment details. Create an API key and copy it.

### 2. Configure .env

```env
LLM_PROVIDER=groq
LLM_MODEL=llama-3.3-70b-versatile
GROQ_API_KEY=gsk_...your_new_key...
LLM_FALLBACKS=openrouter-gemma:google/gemma-3n-e4b-it:free|openrouter:openai/gpt-oss-20b:free|ollama:qwen2.5:7b-instruct
OLLAMA_HOST=http://localhost:11434
```

The fallback chain means: if Groq rate-limits (>30 RPM burst) → try Gemma-3n
on OpenRouter → try gpt-oss-20b on OpenRouter → fall back to the local
Ollama model. All tiers are free.

### 3. Run

```bat
:: Terminal 1 — bot
venv\Scripts\python.exe -m uvicorn vera_bot.api.app:app --host 127.0.0.1 --port 8080
```

Then edit the top of `judge_simulator.py`:

```python
BOT_URL = "http://localhost:8080"
LLM_PROVIDER = "ollama"                  # judge stays local — no quota risk
LLM_API_KEY = ""
LLM_MODEL = "qwen2.5:7b-instruct"
```

```bat
:: Terminal 2 — judge
set PYTHONIOENCODING=utf-8
venv\Scripts\python.exe judge_simulator.py
```

Expected: 100 triggers processed in 2-4 minutes, all composing within the
10 s budget.

---

## If OpenRouter quota is exhausted

OpenRouter's free tier shares one daily token pool across every `:free`
model on the account. When exhausted you get HTTP 429. Two options:

- Wait until 00:00 UTC (quota resets daily).
- Skip OpenRouter tiers by trimming `LLM_FALLBACKS` to Groq + Ollama only:
  `LLM_FALLBACKS=ollama:qwen2.5:7b-instruct`.

## Checking what actually ran

Every composition carries an audit trail in its `rationale` field:

- `template_bypass:<kind>` — fully deterministic, no LLM was called.
- `[COMPOSER_<version>] …` — LLM-generated, check the preceding log line
  `composer: using <provider_name>` to see which tier served the call.

The dashboard at `GET /dashboard` shows a live provider roster; sanity-check
it after boot:

```bat
curl http://127.0.0.1:8080/dashboard
```

## Quick smoke test

For a fast signal without running the full 100-trigger set, edit
`TEST_SCENARIO = "warmup"` (~10 triggers) or `"phase2_short"` near the top
of `judge_simulator.py` before running.
