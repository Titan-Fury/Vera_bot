"""Critic v2 demo driver - inspect rubric scores end-to-end on the dataset.

Usage (from C:\\magicpin_challenge):
    set PYTHONIOENCODING=utf-8
    set CRITIC_V2_ENABLED=1
    set CRITIC_NUM_DRAFTS=3
    set SELF_CRITIQUE_URGENCY_THRESHOLD=2
    set OPENROUTER_API_KEY=sk-or-v1-...
    python scripts\\test_critic_v2.py --trigger trg_023
    python scripts\\test_critic_v2.py --kind chronic_refill_due
    python scripts\\test_critic_v2.py --max 10

What it does:
    Calls EngagementComposer.compose() directly so we can inspect the full
    Composition object (body + flags + rationale). Bypasses the HTTP layer
    because TickAction does not externalize flags by design.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

# Force stdout to UTF-8 so symbols and Hindi characters render on Windows.
try:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
except Exception:
    pass

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from vera_bot.composer import EngagementComposer, make_provider  # noqa: E402
from vera_bot.adapter import format_context_for_prompt  # noqa: E402
from vera_bot import config as cfg  # noqa: E402

DATASET = ROOT / "dataset"


def _load(p: Path) -> Dict[str, Any]:
    return json.loads(p.read_text(encoding="utf-8"))


def _list_triggers(kind_filter: Optional[str] = None) -> List[Path]:
    files = sorted((DATASET / "triggers").glob("trg_*.json"))
    if not kind_filter or kind_filter == "any":
        return files
    return [f for f in files if _load(f).get("kind") == kind_filter]


def _resolve_trigger(token: str) -> Optional[Path]:
    direct = DATASET / "triggers" / f"{token}.json"
    if direct.exists():
        return direct
    matches = list((DATASET / "triggers").glob(f"{token}*.json"))
    return matches[0] if matches else None


def _build_bundle(trigger: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Manually build a context bundle from disk for the given trigger."""
    merchant_id = trigger.get("merchant_id")
    customer_id = trigger.get("customer_id")
    if not merchant_id:
        return None

    mfile = DATASET / "merchants" / f"{merchant_id}.json"
    if not mfile.exists():
        return None
    merchant = _load(mfile)

    category_slug = merchant.get("category_slug") or merchant.get("category")
    cfile = DATASET / "categories" / f"{category_slug}.json"
    category = _load(cfile) if cfile.exists() else {}

    customer = None
    if customer_id:
        cf = DATASET / "customers" / f"{customer_id}.json"
        if cf.exists():
            customer = _load(cf)

    return {
        "category": category,
        "merchant": merchant,
        "trigger": trigger,
        "customer": customer,
    }


def _bar(score: int) -> str:
    s = max(0, min(10, int(score)))
    return "[" + "#" * s + "." * (10 - s) + "]"


def _flag_summary(flags: List[str]) -> str:
    if not flags:
        return "(none)"
    interesting = [f for f in flags if any(
        f.startswith(p) for p in (
            "critique_", "multi_draft", "compliance_", "revis", "no_payload",
            "fallback", "repeat_", "template_",
        )
    )]
    return ", ".join(interesting) if interesting else "(no v2 flags)"


def _print_critique_summary(flags: List[str]) -> None:
    """Decode v2 flags into a readable rubric breakdown."""
    weighted = None
    min_score = None
    multi_draft_picked = None
    compliance_block = None
    has_revision = False

    for f in flags:
        if f.startswith("critique_weighted="):
            # critique_weighted=7.2_min=6
            try:
                payload = f.split("=", 1)[1]
                w_str, m_str = payload.split("_min=")
                weighted = float(w_str)
                min_score = int(m_str)
            except (ValueError, IndexError):
                pass
        elif f.startswith("critique_min="):
            try:
                min_score = int(f.split("=")[1])
            except (ValueError, IndexError):
                pass
        elif f.startswith("multi_draft_picked="):
            multi_draft_picked = f.split("=", 1)[1]
        elif f.startswith("compliance_block_score="):
            try:
                compliance_block = int(f.split("=")[1])
            except (ValueError, IndexError):
                pass
        elif f == "revised":
            has_revision = True

    parts = []
    if weighted is not None:
        parts.append(f"weighted={weighted:.1f}")
    if min_score is not None:
        parts.append(f"min_axis={min_score}")
    if multi_draft_picked:
        parts.append(f"draft_chosen={multi_draft_picked}")
    if compliance_block is not None:
        parts.append(f"COMPLIANCE_BLOCKED(={compliance_block})")
    if has_revision:
        parts.append("REWRITTEN")
    if parts:
        print(f"    rubric   : {' | '.join(parts)}")


def run(trigger_paths: List[Path], max_n: int = 999) -> None:
    print("\n" + "=" * 70)
    print("Critic v2 Demo Run")
    print("=" * 70)
    print(f"  CRITIC_V2_ENABLED       : {cfg.CRITIC_V2_ENABLED}")
    print(f"  CRITIC_NUM_DRAFTS       : {cfg.CRITIC_NUM_DRAFTS}")
    print(f"  CRITIC_COMPLIANCE_HARD  : {cfg.CRITIC_COMPLIANCE_HARD_BLOCK}")
    print(f"  CRITIC_WEIGHTED_THRESH  : {cfg.CRITIC_WEIGHTED_THRESHOLD}")
    print(f"  SELF_CRITIQUE_URGENCY   : >= {cfg.SELF_CRITIQUE_URGENCY_THRESHOLD}")
    print(f"  Triggers                : {min(len(trigger_paths), max_n)}")
    print()

    provider = make_provider(cfg.SETTINGS)
    composer = EngagementComposer(provider)
    print(f"  Provider                : {getattr(provider, 'name', '?')}")
    print()

    for i, tf in enumerate(trigger_paths[:max_n], 1):
        trigger = _load(tf)
        bundle = _build_bundle(trigger)
        if bundle is None:
            print(f"[{i}] {tf.name}: SKIPPED (missing merchant/category file)\n")
            continue

        formatted = format_context_for_prompt(bundle)

        print(f"[{i}] {tf.name}")
        print(f"    kind     : {trigger.get('kind')}  urgency={trigger.get('urgency')}")
        print(f"    merchant : {bundle['merchant'].get('identity', {}).get('name', trigger.get('merchant_id'))}")

        try:
            comp = composer.compose(bundle, formatted, prior_outbound=None)
        except Exception as e:
            print(f"    ERROR: {type(e).__name__}: {e}\n")
            continue

        body = (comp.body or "").strip()
        if body:
            print(f"    BODY     : {body[:300]}")
        else:
            print(f"    BODY     : (suppressed) {comp.rationale[:200]}")

        _print_critique_summary(comp.flags)
        print(f"    flags    : {_flag_summary(comp.flags)}")
        print()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--trigger", help="trg_001 or full id; runs only this one")
    ap.add_argument("--kind", default=None, help="filter by trigger kind (or 'any')")
    ap.add_argument("--max", type=int, default=999, help="max triggers to run")
    args = ap.parse_args()

    if args.trigger:
        f = _resolve_trigger(args.trigger)
        if not f:
            print(f"trigger '{args.trigger}' not found")
            return
        run([f], max_n=1)
        return

    files = _list_triggers(args.kind)
    if not files:
        print(f"no triggers match kind='{args.kind}'")
        return
    run(files, max_n=args.max)


if __name__ == "__main__":
    main()
