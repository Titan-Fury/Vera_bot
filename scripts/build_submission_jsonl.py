"""Build submission.jsonl from dataset/test_pairs.json.

Emits 30 lines — one per canonical (trigger, merchant, customer?) pair — in
the exact shape mandated by challenge-brief.md §7.2:

    {"test_id": "T01", "body": "...", "cta": "open_ended",
     "send_as": "vera", "suppression_key": "...", "rationale": "..."}

Usage (from repo root):
    python -m scripts.build_submission_jsonl
    python -m scripts.build_submission_jsonl --out out/submission.jsonl

The script runs the real EngagementComposer in-process against whichever LLM
provider is configured via `.env` (Gemini/OpenRouter/Groq/Together/Perplexity/
Ollama/Dummy). No HTTP server is required.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Any, Dict, Optional

# Make the vera_bot package importable when run as a script from repo root.
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from vera_bot.adapter import CompositionBundle, format_context_for_prompt
from vera_bot.composer import EngagementComposer, make_provider
from vera_bot.config import SETTINGS

logger = logging.getLogger("build_submission_jsonl")

DATASET_DIR = REPO_ROOT / "dataset"
DEFAULT_OUT = REPO_ROOT / "submission.jsonl"


def _load_json(path: Path) -> Optional[Dict[str, Any]]:
    if not path.exists():
        logger.warning("missing: %s", path)
        return None
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def _build_bundle(pair: Dict[str, Any]) -> CompositionBundle:
    """Load the 4 contexts for one test pair directly from dataset JSON files."""
    bundle = CompositionBundle()

    trigger_id = pair["trigger_id"]
    merchant_id = pair["merchant_id"]
    customer_id = pair.get("customer_id")

    bundle.trigger = _load_json(DATASET_DIR / "triggers" / f"{trigger_id}.json")
    bundle.merchant = _load_json(DATASET_DIR / "merchants" / f"{merchant_id}.json")

    if bundle.merchant:
        slug = bundle.merchant.get("category_slug")
        if slug:
            bundle.category = _load_json(DATASET_DIR / "categories" / f"{slug}.json")

    if customer_id:
        bundle.customer = _load_json(DATASET_DIR / "customers" / f"{customer_id}.json")

    return bundle


def _compose_for_pair(composer: EngagementComposer, pair: Dict[str, Any]) -> Dict[str, Any]:
    bundle = _build_bundle(pair)
    formatted = format_context_for_prompt(bundle)
    comp = composer.compose(bundle.as_dict(), formatted, prior_outbound=None)

    return {
        "test_id": pair["test_id"],
        "body": comp.body,
        "cta": comp.cta,
        "send_as": comp.send_as,
        "suppression_key": comp.suppression_key,
        "rationale": comp.rationale,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--pairs", type=Path, default=DATASET_DIR / "test_pairs.json",
        help="Path to test_pairs.json (default: dataset/test_pairs.json)",
    )
    parser.add_argument(
        "--out", type=Path, default=DEFAULT_OUT,
        help="Output JSONL path (default: submission.jsonl at repo root)",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true",
        help="Verbose logging",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s - %(message)s",
    )

    pairs_doc = _load_json(args.pairs)
    if not pairs_doc or "pairs" not in pairs_doc:
        logger.error("no 'pairs' key in %s", args.pairs)
        return 2

    provider = make_provider(SETTINGS)
    composer = EngagementComposer(provider)
    logger.info(
        "composer ready: provider=%s model=%s",
        getattr(provider, "name", type(provider).__name__),
        getattr(provider, "model", SETTINGS.llm_model or "default"),
    )

    args.out.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    errors = 0
    with args.out.open("w", encoding="utf-8") as out_f:
        for pair in pairs_doc["pairs"]:
            test_id = pair.get("test_id", "?")
            try:
                line = _compose_for_pair(composer, pair)
            except Exception as exc:  # noqa: BLE001
                logger.exception("compose failed for %s: %s", test_id, exc)
                errors += 1
                # Emit an explicit error line so the submission still has 30 lines.
                line = {
                    "test_id": test_id,
                    "body": "",
                    "cta": "open_ended",
                    "send_as": "vera",
                    "suppression_key": f"error:{test_id}",
                    "rationale": f"compose error: {type(exc).__name__}",
                }

            out_f.write(json.dumps(line, ensure_ascii=False) + "\n")
            written += 1
            logger.info(
                "%s -> %d chars body, cta=%s, send_as=%s",
                test_id, len(line["body"]), line["cta"], line["send_as"],
            )

    logger.info("wrote %d lines to %s (errors=%d)", written, args.out, errors)
    return 0 if errors == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
