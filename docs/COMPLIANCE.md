# Spec compliance matrix

Every pre-flight checklist item from `challenge-testing-brief.md` §12 mapped
to the code that satisfies it.

## HTTP contract (CTB §2)

| Requirement                                              | Location                                           | Verified by                          |
|----------------------------------------------------------|----------------------------------------------------|--------------------------------------|
| `GET /v1/healthz` returns 200 with status+uptime         | `vera_bot/api/app.py:healthz`                      | `tests/test_api_contract.py`         |
| `GET /v1/metadata` returns stable `submitted_at`         | `vera_bot/config.py:SETTINGS.submitted_at`         | `tests/test_api_contract.py`         |
| `POST /v1/context` accepts first write (200)             | `vera_bot/api/app.py:push_context`                 | `tests/test_api_contract.py`         |
| Stale version → **HTTP 409**                             | `vera_bot/api/app.py:push_context` (JSONResponse)  | `tests/test_api_contract.py`         |
| Malformed body → **HTTP 400** with `reason`+`details`    | `vera_bot/api/errors.py:_validation_handler`       | `tests/test_api_contract.py`         |
| Invalid scope → 400                                      | `CtxBody.scope` Literal + 400 handler              | `tests/test_api_contract.py`         |
| 500 KB payload cap                                       | `_payload_cap` middleware                          | covered in design; enforced at edge  |
| `POST /v1/tick` caps at **20 actions**                   | `TICK_ACTION_CAP=20`; loop break                   | `tests/test_api_contract.py`         |
| Each action has real `template_name` + `template_params` | `_template_params_for` + `vera_{kind}_v1`          | `tests/test_api_contract.py`         |
| Tick response per-call budget ≤ 10 s                     | `asyncio.wait_for(compose, timeout=8.0)`           | design                               |
| `POST /v1/reply` supports send/wait/end                  | `Composition.to_reply` + `compose_reply`           | `tests/test_api_contract.py`         |
| `POST /v1/teardown` clears state                         | `vera_bot/api/app.py:teardown`                     | `tests/test_api_contract.py`         |

## Composer guarantees (CTB §10, ED §Composer)

| Requirement                              | Location                                          | Verified by                          |
|------------------------------------------|---------------------------------------------------|--------------------------------------|
| Body ≤ 320 chars                         | `validators.enforce_length`                       | `tests/test_validators.py`           |
| No URLs                                  | `validators.strip_urls`                           | `tests/test_validators.py`           |
| Anti-repetition (verbatim)               | `validators.is_repeat` + `compose_reply`          | `tests/test_validators.py`           |
| Hard-no exit                             | `validators.detect_hard_no`                       | `tests/test_validators.py`           |
| Auto-reply exit                          | `validators.detect_auto_reply`                    | `tests/test_validators.py`           |
| Explicit intent → action mode            | `validators.detect_explicit_intent`               | `tests/test_validators.py`           |
| Per-`trigger.kind` prompt dispatch       | `composer/prompts.py:select_prompt`               | `tests/test_composer_routing.py`     |
| Versioned output (`composer_v1`)         | `COMPOSER_VERSION` stamped in rationale           | `tests/test_composer_routing.py`     |

## Dataset (brief §6)

| Requirement                           | Location                                      |
|---------------------------------------|-----------------------------------------------|
| 5 categories                          | `dataset/categories/*.json`                   |
| 50 merchants                          | `dataset/merchants/*.json`                    |
| 200 customers                         | `dataset/customers/*.json`                    |
| 100 triggers                          | `dataset/triggers/*.json`                     |
| 30 canonical test pairs               | `dataset/test_pairs.json`                     |
| Deterministic regeneration            | `dataset/generate_dataset.py` (fixed seed)    |

## Ops hygiene (brief §7.3)

| Artifact           | File                                           |
|--------------------|------------------------------------------------|
| README             | `README.md`                                    |
| Architecture doc   | `docs/ARCHITECTURE.md`                         |
| Prompt doc         | `docs/PROMPTS.md`                              |
| Changelog          | `docs/CHANGELOG.md`                            |
| Dependency list    | `requirements.txt`                             |
| Packaging          | `pyproject.toml`                               |
| Env template       | `.env.example`                                 |
| Container          | `Dockerfile` (non-root, healthcheck)           |
| Tests              | `tests/test_*.py`                              |

## Concurrency / safety

- Context and conversation stores both guarded by `asyncio.Lock`.
- Only one uvicorn worker (enforced in `vera_bot/__main__.py`).
- Blocking LLM calls dispatched via `asyncio.to_thread` so the event loop
  remains responsive.
- Retry decorator (`composer/providers/_retry.py`) covers transient 5xx and
  connection errors on OpenAI-compat providers.
