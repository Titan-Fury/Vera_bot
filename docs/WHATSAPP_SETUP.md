# WhatsApp Live Test Setup Guide

This document walks you through getting Omni Bot to send a real WhatsApp
message tomorrow. Total time: ~20 minutes from zero.

---

## Why Meta WhatsApp Cloud API (not Gupshup, not Twilio, not BSP)

For the first live test, Meta's own Cloud API is the best path:

- **Free up to 1,000 conversations/month** (more than enough for a personal demo)
- **Self-serve signup, no business verification required for test phone numbers**
- **No BSP deposits, no contracts, no per-message resale markup**
- **Real Meta endpoint** — same backend the BSPs themselves use

When you eventually onboard real merchants, switch to Gupshup or 360dialog
(they handle template approval workflows better and offer Indian INR billing).
The `WhatsAppCloudProvider` and BSP adapters share the same `DeliveryProvider`
interface, so the swap is one config line.

---

## Step 1 — Create the Meta Developer App (5 min)

1. Go to https://developers.facebook.com/apps and click **Create App**.
2. Select **Other** → **Next** → **Business** → **Next**.
3. Name it (e.g. "Omni Bot Test"), provide a business email, click **Create**.
4. From the dashboard, find the **WhatsApp** tile and click **Set up**.

That's the app. You're now inside the WhatsApp panel.

---

## Step 2 — Capture Credentials (3 min)

In **WhatsApp → API Setup** you'll see four critical values:

| Field | Where to Copy It | Env Var |
|---|---|---|
| Temporary access token | Under "Temporary access token" — copy the long string | `WHATSAPP_CLOUD_TOKEN` |
| Phone number ID | Below the temp number — *not* the phone number itself, the numeric ID | `WHATSAPP_CLOUD_PHONE_NUMBER_ID` |
| WhatsApp Business Account ID | Below the phone number ID | (not needed for sending text) |
| Test phone number | The number Meta gave you to send FROM | (informational) |

> The temporary token expires in 24 hours. For permanent use, create a System
> User in Business Manager and generate a permanent token (one-time, ~5 min).

---

## Step 3 — Add a Recipient (2 min)

By default Meta only lets you send TO numbers you have added explicitly.

1. In **WhatsApp → API Setup**, scroll to "Send and receive messages".
2. Under "To", click **Manage phone number list**.
3. Add your own WhatsApp number (and any test recipients).
4. Meta sends a 6-digit code via WhatsApp; enter it to verify.

**Without this step you will get error code 131030 — recipient not in allowed list.**

---

## Step 4 — First Send (proves credentials work)

Test with Meta's pre-built `hello_world` template (no approval needed):

```cmd
cd C:\magicpin_challenge
set PYTHONIOENCODING=utf-8
set WHATSAPP_PROVIDER=cloud
set WHATSAPP_CLOUD_TOKEN=EAAG...your...token...here
set WHATSAPP_CLOUD_PHONE_NUMBER_ID=1234567890
python -m vera_bot.tools.send_test --to +919876543210 --template hello_world --language en_US
```

If you see `SENT message_id=wamid...` — credentials work. Check WhatsApp on
your phone; "Hello World" should arrive within 5 seconds.

If you see error `131030` — go back to Step 3 and add your number.

---

## Step 5 — Send a Composed Message (the real demo)

The 24-hour session window opens the moment your verified number sends a
message TO the test phone number. So:

1. **From your phone**, send any message to the test number Meta gave you in
   Step 2.
2. Now you can send any free-text reply for 24 hours.

```cmd
set CRITIC_V2_ENABLED=1
set CRITIC_NUM_DRAFTS=3
set SELF_CRITIQUE_URGENCY_THRESHOLD=2
set OPENROUTER_API_KEY=sk-or-v1-...
python -m vera_bot.tools.send_test --to +919876543210 --trigger trg_023
```

Expected output:
```
[1/5] Delivery provider : whatsapp_cloud
[2/5] Recipient         : +919876543210
[3/5] Trigger           : trg_023_competitor_opened_dentist  kind=competitor_opened  urgency=2
      Merchant          : Dr. Meera's Dental Clinic
[4/5] Composing (LLM=fallback[...], critic_v2=True, drafts=3) ...
      Body              : Smile Studio opened 1.3 km away at ₹199 cleaning. Your...
      Rubric            : {'weighted': 8.6, 'min_axis': 8, 'multi_draft_picked': '1of3'}
      Flags             : multi_draft_picked=1of3, critique_weighted=8.6_min=8
[5/5] Delivering ...

SENT  message_id=wamid.HBgM...  latency=412ms  via=whatsapp_cloud
```

Check your WhatsApp — you'll see the composed message arrive.

---

## Step 6 — Send to Your Saree Customers Tomorrow

When you have your real saree merchant data, build two JSON files matching the
dataset shape (look at `dataset/merchants/m_001_drmeera_dentist_delhi.json` and
`dataset/triggers/trg_023_competitor_opened_dentist.json` as templates).

```cmd
python -m vera_bot.tools.send_test ^
    --to +919876543210 ^
    --merchant-file my_saree_merchant.json ^
    --trigger-file my_abandoned_cart_trigger.json
```

If your category is brand new (not dentists/salons/etc.), also pass
`--category-file my_category.json`.

---

## Health & Inspection

Before sending, always sanity-check the stack:

```cmd
python -m vera_bot.tools.send_test --health
```

This prints provider liveness, safety stats (rate limit usage, opt-out count),
and current Critic v2 config — without sending anything.

The HTTP API exposes the same:

```cmd
curl http://localhost:8080/v1/health/delivery
```

---

## Safety Guards (already enforced — you cannot turn these off)

Before any send, the SafetyGuard checks:

1. **Opt-out list** — any recipient who said "stop" / "unsubscribe" / Hindi
   equivalents is permanently blocked. Persisted to `data/opt_out.jsonl`.
2. **Per-recipient rate limit** — at most 1 message per recipient per
   `DELIVERY_RATE_LIMIT_PER_RECIPIENT_MIN` minutes (default 60). Stops
   accidental double-sends.
3. **Daily cap** — max `DELIVERY_DAILY_CAP` total sends across all recipients
   in any rolling 24h (default 50). Stops runaway loops.

Every attempt — successful or blocked — is logged to `data/send_log.jsonl`
with the full rubric breakdown.

---

## Manually Opting Out a Recipient

If a customer texts back "stop":

```cmd
curl -X POST http://localhost:8080/v1/optout ^
     -H "Content-Type: application/json" ^
     -d "{\"recipient\": \"+919876543210\", \"reason\": \"customer_request\"}"
```

Or directly:

```python
from vera_bot.delivery import get_safety_guard
get_safety_guard().opt_out_recipient("+919876543210", "customer_request")
```

---

## Common Pitfalls

| Symptom | Cause | Fix |
|---|---|---|
| `131030` | Recipient not in allowed list | Add via Step 3 |
| `131047` | Outside 24h session window | Use `--template` with approved template |
| `131056` | Pair rate limit | Wait or increase `DELIVERY_RATE_LIMIT_PER_RECIPIENT_MIN` |
| Token expired | 24h temp token | Generate System User token in Business Manager |
| Empty body, "compliance hard-block" | Critic v2 caught a risky claim | Check rationale; adjust prompt or accept the block |
| `critique_skipped` | LLM rate-limited or timed out | Fallback chain handled it; original draft shipped |
| Body contains `[STYLE EXEMPLAR]` | Few-shot prompt leaked | Should be impossible; file an issue |

---

## What's NOT Production-Ready Yet (deferred)

These come AFTER the live test validates the basics. Listed here so you know
what's in flight:

- **Multi-tenancy** — single tenant only. Multiple merchants need Postgres
  (see Section 2 Gap 2 in `omnibot_monetization_plan.pdf`).
- **Self-serve dashboard** — operator runs CLI today. Dashboard is Gap 4.
- **Trigger scheduler** — fires manually via CLI. Auto-fire is Gap 3.
- **Billing** — manual invoices today. Razorpay subscriptions is Gap 5.
- **ROAS attribution** — send_log captures rubric scores but not conversion
  webhooks. That is Gap 6.
- **Template auto-submit to Meta** — manual today. Auto is Gap 7.
- **Webhook receiver for inbound replies** — bot only sends right now;
  receiving requires a public HTTPS endpoint registered with Meta. That's
  the next milestone after the first send works.

---

## File Map

```
vera_bot/
  config.py                       # CRITIC_V2_*, WHATSAPP_*, DELIVERY_*
  composer/
    critic.py                     # 7-axis rubric, weighted scoring
    base.py                       # multi-draft pick-best, compliance hard-block
  delivery/                       # NEW
    __init__.py
    base.py                       # DeliveryProvider protocol, DeliveryResult
    console.py                    # dry-run printer
    whatsapp_cloud.py             # Meta Cloud API adapter
    factory.py                    # provider selection from env
    safety.py                     # OptOutStore, RateLimiter, SafetyGuard
    send_log.py                   # JSONL audit log
  tools/                          # NEW
    send_test.py                  # operator CLI for live sends
  api/
    app.py                        # +/v1/health/delivery, +/v1/optout

scripts/
  test_critic_v2.py               # rubric inspection across the dataset

data/                             # NEW (gitignored)
  opt_out.jsonl                   # persistent opt-out list
  send_log.jsonl                  # every delivery attempt

tests/
  test_delivery.py                # NEW: 16 tests for safety + console + factory

docs/
  WHATSAPP_SETUP.md               # this file

.env.demo                         # ready-to-merge config
```
