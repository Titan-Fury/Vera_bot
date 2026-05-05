"""Operator CLI: compose + critique + send a real WhatsApp message.

This is the script you run for tomorrow's live test. It does the full path:
    1. Loads category/merchant/customer/trigger from dataset/ (or your own JSON)
    2. Calls EngagementComposer.compose() with critic v2 enabled
    3. Runs SafetyGuard (opt-out + rate limit + daily cap)
    4. Delivers via the configured WhatsApp provider (cloud / console)
    5. Writes a JSONL audit record to data/send_log.jsonl

USAGE
=====

  # Dry run (prints the message - no real send) - safe to test anytime
  set WHATSAPP_DRY_RUN=1
  python -m vera_bot.tools.send_test --to +919876543210 --trigger trg_023

  # Live send to your own WhatsApp number (after Meta Cloud setup)
  set WHATSAPP_PROVIDER=cloud
  set WHATSAPP_CLOUD_TOKEN=EAAG...
  set WHATSAPP_CLOUD_PHONE_NUMBER_ID=1234567890
  set CRITIC_V2_ENABLED=1
  set CRITIC_NUM_DRAFTS=3
  set SELF_CRITIQUE_URGENCY_THRESHOLD=2
  python -m vera_bot.tools.send_test --to +919876543210 --trigger trg_023

  # Use your own merchant + trigger JSON (paste-ready format below)
  python -m vera_bot.tools.send_test --to +91xxxx --merchant-file my_saree.json --trigger-file my_trigger.json

  # Force a template send (required outside 24h session window)
  python -m vera_bot.tools.send_test --to +91xxxx --template hello_world --language en_US

  # Show health + safety stats and exit
  python -m vera_bot.tools.send_test --health

PRODUCTION CHECKLIST FOR TOMORROW
==================================
[ ] WhatsApp Cloud app created at developers.facebook.com
[ ] Test phone number added under WhatsApp -> API Setup, OTP-verified
[ ] WHATSAPP_CLOUD_TOKEN and WHATSAPP_CLOUD_PHONE_NUMBER_ID exported
[ ] First 'hello_world' template send succeeded (proves credentials)
[ ] CRITIC_V2_ENABLED=1 in env
[ ] LLM provider key set (OPENROUTER or GROQ)
[ ] Recipient is in the Meta-allowed list
[ ] Recipient has messaged YOU within the last 24h, OR you are sending
    a pre-approved template (free-text only works in the 24h session window)
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

# UTF-8 stdout for Windows consoles
try:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
except Exception:
    pass

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from vera_bot import config as cfg  # noqa: E402
from vera_bot.adapter import format_context_for_prompt  # noqa: E402
from vera_bot.composer import EngagementComposer, make_provider  # noqa: E402
from vera_bot.delivery import (  # noqa: E402
    get_provider,
    get_safety_guard,
    get_send_log,
)
from vera_bot.delivery.send_log import SendLog  # noqa: E402
from vera_bot.delivery.whatsapp_cloud import WhatsAppCloudProvider  # noqa: E402

logger = logging.getLogger(__name__)


DATASET = ROOT / "dataset"


# ============================================================================
# Helpers
# ============================================================================

def _load(p: Path) -> Dict[str, Any]:
    return json.loads(p.read_text(encoding="utf-8"))


def _resolve_trigger(token: str) -> Optional[Path]:
    direct = DATASET / "triggers" / f"{token}.json"
    if direct.exists():
        return direct
    matches = list((DATASET / "triggers").glob(f"{token}*.json"))
    return matches[0] if matches else None


def _build_bundle_from_dataset(trigger_path: Path) -> Optional[Dict[str, Any]]:
    trigger = _load(trigger_path)
    merchant_id = trigger.get("merchant_id")
    if not merchant_id:
        return None
    mfile = DATASET / "merchants" / f"{merchant_id}.json"
    if not mfile.exists():
        return None
    merchant = _load(mfile)
    category_slug = merchant.get("category_slug") or merchant.get("category")
    cfile = DATASET / "categories" / f"{category_slug}.json" if category_slug else None
    category = _load(cfile) if cfile and cfile.exists() else {}
    customer = None
    cust_id = trigger.get("customer_id")
    if cust_id:
        cf = DATASET / "customers" / f"{cust_id}.json"
        if cf.exists():
            customer = _load(cf)
    return {
        "category": category,
        "merchant": merchant,
        "trigger": trigger,
        "customer": customer,
    }


def _build_bundle_from_files(
    merchant_file: Optional[str],
    trigger_file: Optional[str],
    category_file: Optional[str],
    customer_file: Optional[str],
) -> Optional[Dict[str, Any]]:
    if not merchant_file or not trigger_file:
        return None
    merchant = _load(Path(merchant_file))
    trigger = _load(Path(trigger_file))
    category: Dict[str, Any] = {}
    if category_file:
        category = _load(Path(category_file))
    elif merchant.get("category_slug"):
        # Try to auto-load from dataset/categories/
        cf = DATASET / "categories" / f"{merchant['category_slug']}.json"
        if cf.exists():
            category = _load(cf)
    customer = _load(Path(customer_file)) if customer_file else None
    return {
        "category": category,
        "merchant": merchant,
        "trigger": trigger,
        "customer": customer,
    }


def _print_section(title: str) -> None:
    print()
    print("=" * 70)
    print(title)
    print("=" * 70)


def _decode_rubric_from_flags(flags: List[str]) -> Dict[str, Any]:
    rubric: Dict[str, Any] = {}
    for f in flags:
        if f.startswith("critique_weighted="):
            try:
                payload = f.split("=", 1)[1]
                w_str, m_str = payload.split("_min=")
                rubric["weighted"] = float(w_str)
                rubric["min_axis"] = int(m_str)
            except (ValueError, IndexError):
                pass
        elif f.startswith("critique_min="):
            try:
                rubric["min_axis"] = int(f.split("=")[1])
            except (ValueError, IndexError):
                pass
        elif f.startswith("multi_draft_picked="):
            rubric["multi_draft_picked"] = f.split("=", 1)[1]
        elif f.startswith("compliance_block_score="):
            try:
                rubric["compliance"] = int(f.split("=")[1])
                rubric["compliance_blocked"] = True
            except (ValueError, IndexError):
                pass
        elif f == "revised":
            rubric["rewritten"] = True
    return rubric


# ============================================================================
# Health command
# ============================================================================

def cmd_health() -> int:
    _print_section("Delivery Provider Health")
    provider = get_provider()
    print(f"  configured provider : {provider.name}")
    health = provider.health()
    for k, v in health.items():
        print(f"  {k:20s}: {v}")

    _print_section("Safety Guard Stats")
    guard = get_safety_guard()
    stats = guard.stats()
    print(json.dumps(stats, indent=2, default=str))

    _print_section("Critic v2 Config")
    print(f"  CRITIC_V2_ENABLED       : {cfg.CRITIC_V2_ENABLED}")
    print(f"  CRITIC_NUM_DRAFTS       : {cfg.CRITIC_NUM_DRAFTS}")
    print(f"  CRITIC_COMPLIANCE_HARD  : {cfg.CRITIC_COMPLIANCE_HARD_BLOCK}")
    print(f"  CRITIC_WEIGHTED_THRESH  : {cfg.CRITIC_WEIGHTED_THRESHOLD}")
    print(f"  SELF_CRITIQUE_URGENCY   : >= {cfg.SELF_CRITIQUE_URGENCY_THRESHOLD}")
    return 0


# ============================================================================
# Send command
# ============================================================================

def cmd_send(args: argparse.Namespace) -> int:
    # ---- Resolve bundle ---------------------------------------------------
    bundle: Optional[Dict[str, Any]] = None
    trigger_path: Optional[Path] = None

    if args.trigger:
        trigger_path = _resolve_trigger(args.trigger)
        if not trigger_path:
            print(f"ERROR: trigger '{args.trigger}' not found in {DATASET / 'triggers'}")
            return 2
        bundle = _build_bundle_from_dataset(trigger_path)
    elif args.merchant_file or args.trigger_file:
        bundle = _build_bundle_from_files(
            merchant_file=args.merchant_file,
            trigger_file=args.trigger_file,
            category_file=args.category_file,
            customer_file=args.customer_file,
        )
        if args.trigger_file:
            trigger_path = Path(args.trigger_file)

    if not args.template and bundle is None:
        print("ERROR: provide --trigger OR --merchant-file + --trigger-file OR --template")
        return 2

    # ---- Provider + safety ------------------------------------------------
    provider = get_provider()
    guard = get_safety_guard()
    send_log = get_send_log()

    print(f"\n[1/5] Delivery provider : {provider.name}")
    print(f"[2/5] Recipient         : {args.to}")

    # Hard-fail before doing any work if recipient already opted out.
    pre_check = guard.check(args.to)
    if not pre_check.allowed:
        print(f"\nBLOCKED by safety: {pre_check.reason}")
        send_log.write({
            "delivery_status": "blocked",
            "blocked_by": pre_check.blocked_by,
            "recipient": args.to,
            "reason": pre_check.reason,
            "retry_after_s": pre_check.retry_after_s,
        })
        return 3

    # ---- Template path (skips composer entirely) --------------------------
    if args.template:
        print(f"[3/5] Mode              : TEMPLATE '{args.template}' ({args.language})")
        if not hasattr(provider, "send_template"):
            print(f"ERROR: provider {provider.name} does not support templates")
            return 4
        result = provider.send_template(  # type: ignore[attr-defined]
            recipient=args.to,
            template_name=args.template,
            language_code=args.language or "en_US",
        )
        if result.success:
            guard.record_send(args.to)
            print(f"\nSENT  message_id={result.message_id} latency={result.latency_ms}ms")
        else:
            print(f"\nFAILED  {result.error_code}: {result.error_message}")
        send_log.write(SendLog.from_result(
            result,
            trigger_id=None,
            trigger_kind="template",
            merchant_id=None,
            delivery_status="sent" if result.success else "failed",
        ))
        return 0 if result.success else 5

    # ---- Compose path -----------------------------------------------------
    assert bundle is not None
    trigger = bundle["trigger"]
    merchant = bundle.get("merchant") or {}

    print(f"[3/5] Trigger           : {trigger.get('id')}  kind={trigger.get('kind')}  urgency={trigger.get('urgency')}")
    print(f"      Merchant          : {merchant.get('identity', {}).get('name', merchant.get('merchant_id'))}")

    formatted = format_context_for_prompt(bundle)
    llm = make_provider(cfg.SETTINGS)
    composer = EngagementComposer(llm)

    print(f"[4/5] Composing (LLM={getattr(llm, 'name', '?')}, critic_v2={cfg.CRITIC_V2_ENABLED}, drafts={cfg.CRITIC_NUM_DRAFTS}) ...")
    try:
        comp = composer.compose(bundle, formatted, prior_outbound=None)
    except Exception as e:
        print(f"\nERROR composing: {type(e).__name__}: {e}")
        return 6

    rubric = _decode_rubric_from_flags(comp.flags)
    body = (comp.body or "").strip()

    print(f"      Body              : {body[:280] or '(empty - check flags)'}")
    if rubric:
        print(f"      Rubric            : {rubric}")
    if comp.flags:
        print(f"      Flags             : {', '.join(comp.flags)}")

    # ---- Pre-flight: empty body (compliance block, repeat, etc.) ----------
    if not body:
        print("\nABORT: composer produced empty body (likely compliance hard-block or fallback). NOT sending.")
        send_log.write({
            "delivery_status": "blocked",
            "blocked_by": "empty_body",
            "trigger_id": trigger.get("id"),
            "trigger_kind": trigger.get("kind"),
            "merchant_id": merchant.get("merchant_id"),
            "recipient": args.to,
            "rationale": comp.rationale,
            "rubric": rubric,
            "flags": comp.flags,
        })
        return 7

    if args.no_send:
        print("\n--no-send specified - stopping after compose.")
        return 0

    # ---- Deliver ---------------------------------------------------------
    print(f"[5/5] Delivering ...")
    context_meta = {
        "trigger_id": trigger.get("id"),
        "trigger_kind": trigger.get("kind"),
        "merchant_id": merchant.get("merchant_id"),
        **{k: v for k, v in rubric.items() if k in ("weighted", "min_axis")},
    }
    result = provider.send_text(
        recipient=args.to,
        body=body,
        context=context_meta,
    )

    if result.success:
        guard.record_send(args.to)
        print(f"\nSENT  message_id={result.message_id}  latency={result.latency_ms}ms  via={result.provider}")
    else:
        print(f"\nFAILED  {result.error_code}: {result.error_message}")
        if result.error_code in ("131030",):
            print("        -> Recipient is not in the Meta-allowed list. Add it under WhatsApp -> API Setup.")
        if result.error_code in ("131047",):
            print("        -> Outside 24h session window. Use --template (after Meta approval).")

    send_log.write(SendLog.from_result(
        result,
        trigger_id=trigger.get("id"),
        trigger_kind=trigger.get("kind"),
        merchant_id=merchant.get("merchant_id"),
        rubric=rubric,
        flags=comp.flags,
        delivery_status="sent" if result.success else "failed",
    ))

    return 0 if result.success else 5


# ============================================================================
# Main
# ============================================================================

def main() -> int:
    ap = argparse.ArgumentParser(
        description="Compose + critique + send a WhatsApp message via the Vera/Omni Bot pipeline",
    )
    ap.add_argument("--health", action="store_true", help="Show provider + safety + critic config and exit")
    ap.add_argument("--to", help="Recipient phone in E.164 (e.g. +919876543210)")

    src = ap.add_mutually_exclusive_group(required=False)
    src.add_argument("--trigger", help="Dataset trigger id token (e.g. trg_023)")
    src.add_argument("--template", help="Pre-approved Meta template name (e.g. hello_world)")

    ap.add_argument("--language", help="Template language code (default en_US)", default=None)
    ap.add_argument("--merchant-file", help="Path to merchant JSON (overrides dataset)")
    ap.add_argument("--trigger-file", help="Path to trigger JSON (overrides dataset)")
    ap.add_argument("--category-file", help="Path to category JSON (overrides dataset)")
    ap.add_argument("--customer-file", help="Path to customer JSON (overrides dataset)")
    ap.add_argument("--no-send", action="store_true", help="Compose only - do not call delivery provider")
    ap.add_argument("--verbose", action="store_true")

    args = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    if args.health:
        return cmd_health()

    if not args.to:
        ap.error("--to is required for sends; use --health to inspect config")
        return 2

    return cmd_send(args)


if __name__ == "__main__":
    raise SystemExit(main())
