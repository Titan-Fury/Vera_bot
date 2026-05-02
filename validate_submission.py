"""Validate submission_chain.jsonl against brief hard rules."""
import json, sys
sys.stdout.reconfigure(encoding="utf-8")

path = r"C:\magicpin_challenge\out\submission_chain.jsonl"
lines = [json.loads(l) for l in open(path, encoding="utf-8") if l.strip()]

valid_cta = {"open_ended", "binary_yes_no", "none"}
valid_send_as = {"vera", "merchant_on_behalf"}

fails = []
lens = []
fallbacks = 0
for e in lines:
    tid = e["test_id"]
    body = e["body"]
    lens.append(len(body))
    checks = {
        "body_leq_320": len(body) <= 320,
        "has_body": len(body.strip()) > 0,
        "no_http": "http://" not in body.lower() and "https://" not in body.lower() and "www." not in body.lower(),
        "cta_valid": e["cta"] in valid_cta,
        "send_as_valid": e["send_as"] in valid_send_as,
        "rationale_prefix": e["rationale"].startswith("[composer_v1]"),
        "has_supp_key": bool(e["suppression_key"]),
    }
    for k, v in checks.items():
        if not v:
            fails.append((tid, k, repr(body)[:80]))
    if "fallback" in e["rationale"].lower():
        fallbacks += 1

print(f"Total lines: {len(lines)}")
print(f"Body length: min={min(lens)} max={max(lens)} avg={sum(lens)/len(lens):.0f}")
print(f"Fallback messages: {fallbacks}/{len(lines)}")
print(f"Rule violations: {len(fails)}")
for tid, rule, body in fails:
    print(f"  {tid}  {rule}  body={body}")

# Suppression key uniqueness per (merchant/trigger implicit via test_id)
sk = [e["suppression_key"] for e in lines]
print(f"Suppression keys unique: {len(set(sk))}/{len(sk)}")
if len(set(sk)) < len(sk):
    from collections import Counter
    for k, c in Counter(sk).items():
        if c > 1: print(f"  dup: {k} x{c}")

# CTA distribution
from collections import Counter
print(f"CTA mix: {dict(Counter(e['cta'] for e in lines))}")
print(f"send_as mix: {dict(Counter(e['send_as'] for e in lines))}")

print("\n--- Sample 3 bodies ---")
for e in lines[:3]:
    print(f"{e['test_id']}: {e['body']}")
    print(f"  rationale: {e['rationale'][:140]}")
