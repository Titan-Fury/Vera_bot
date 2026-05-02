# Prompt library (composer_v1)

Source of truth: `vera_bot/composer/prompts.py`.

Every prompt is assembled as:

```
_BASE_RULES
+ _AUDIENCE_{MERCHANT|CUSTOMER}
+ _KIND_BLOCKS[trigger.kind or "default"]
+ (REPLY_LOGIC if is_reply)
+ _OUTPUT_{COMPOSE|REPLY}
```

The rationale emitted by the LLM MUST start with `[composer_v1]` — if it
doesn't, the composer prepends it (for audit).

## Hard rules (every prompt)

1. Body ≤ 320 chars (aim 150-220).
2. No URLs of any kind.
3. Exactly one CTA — binary for action triggers, open-ended for info nudges.
4. Specificity: anchor on a verifiable fact from the context.
5. Voice/vocab aligned with CategoryContext; respect taboos.
6. Language aligned with merchant/customer preference; Hindi-English
   code-mix encouraged when indicated.
7. No hallucinations: only facts in the bundle.
8. Rationale starts with `[composer_v1]`.

## Audience blocks

| Block                 | Send-as              | Lever hints                              |
|-----------------------|----------------------|------------------------------------------|
| `_AUDIENCE_MERCHANT`  | `vera`               | loss aversion, social proof, curiosity   |
| `_AUDIENCE_CUSTOMER`  | `merchant_on_behalf` | relationship, scarcity, personalization  |

## Kind blocks

| `trigger.kind`         | Primary lever              | CTA shape           |
|------------------------|----------------------------|---------------------|
| `research_digest`      | curiosity + reciprocity    | open-ended          |
| `perf_dip`             | loss aversion              | binary YES/NO       |
| `perf_spike`           | momentum / social proof    | binary              |
| `recall_due`           | relationship continuity    | binary (booking)    |
| `competitor_opened`    | loss aversion + status     | binary (defend)     |
| `festival_upcoming`    | timeliness + scarcity      | binary              |
| `dormant_with_vera`    | reciprocity + curiosity    | open-ended          |
| `milestone_reached`    | social proof + pride       | open-ended          |
| `default`              | strongest lever from ctx   | context-dependent   |

Unknown kinds fall through to the `default` block. Every unlisted kind still
gets `_BASE_RULES` + audience, so the hard rules remain enforced.

## Reply-mode logic block

Added only when `is_reply=True`. Codifies:

- Intent lock (yes/ok/chalo/go ahead) → confirm concretely, drop qualifying.
- Hostile / stop / not interested → `action=end`, warm exit.
- Off-topic → one-clause acknowledge, pivot back.
- Uncertainty / "give me a moment" → `action=wait` with `wait_seconds` in
  [30, 600].

Pre-LLM hard gates (in `compose_reply`, before calling the provider):

```
detect_auto_reply(user_msgs)     -> action=end
detect_hard_no(latest_inbound)   -> action=end
turn_number >= 6                 -> action=end
detect_explicit_intent(latest)   -> add [INTENT LOCK] note to prompt
```

## Output schemas

**Compose** (proactive, `/v1/tick`):
```json
{
  "body": "<string, <=320>",
  "cta": "open_ended | binary_yes_no | none",
  "send_as": "vera | merchant_on_behalf",
  "suppression_key": "<stable id>",
  "rationale": "[composer_v1] <why>"
}
```

**Reply** (`/v1/reply`):
```json
{
  "action": "send | wait | end",
  "body": "<only if send>",
  "cta": "open_ended | binary_yes_no | none",
  "wait_seconds": 60,
  "rationale": "[composer_v1] <why>"
}
```

## Versioning

Version bump checklist (e.g. to `composer_v2`):

1. Update `COMPOSER_VERSION` in `vera_bot/config.py`.
2. Modify whichever kind block(s) in `prompts.py`.
3. Add a `docs/CHANGELOG.md` entry with diff rationale + expected rubric lift.
4. Update this file's version header.
5. Re-run the judge harness on all scenarios and attach before/after scores.
