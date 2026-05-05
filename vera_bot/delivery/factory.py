"""Provider factory + singleton SafetyGuard wired from env."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Optional

from .base import DeliveryProvider
from .console import ConsoleProvider
from .safety import OptOutStore, RateLimiter, SafetyGuard
from .whatsapp_cloud import WhatsAppCloudProvider

logger = logging.getLogger(__name__)


def _data_dir() -> Path:
    p = Path(os.environ.get("DATA_DIR") or "./data").resolve()
    p.mkdir(parents=True, exist_ok=True)
    return p


def get_provider(provider_name: Optional[str] = None) -> DeliveryProvider:
    """Construct delivery provider from env vars.

    Env knobs:
        WHATSAPP_PROVIDER          : "cloud" | "console" (default "console" for safety)
        WHATSAPP_DRY_RUN           : "1" forces console regardless of provider
        WHATSAPP_CLOUD_TOKEN       : Meta access token
        WHATSAPP_CLOUD_PHONE_NUMBER_ID : Meta phone number ID (NOT the phone number)
        WHATSAPP_CLOUD_API_VERSION : default "v20.0"
        WHATSAPP_CLOUD_TIMEOUT_S   : default 8.0
    """
    if (os.environ.get("WHATSAPP_DRY_RUN") or "") == "1":
        logger.info("WHATSAPP_DRY_RUN=1 -> using console provider")
        return ConsoleProvider()

    name = (provider_name or os.environ.get("WHATSAPP_PROVIDER") or "console").lower().strip()
    if name in ("console", "dryrun", "dry_run", "stdout", ""):
        return ConsoleProvider()

    if name in ("cloud", "whatsapp_cloud", "meta", "meta_cloud"):
        token = os.environ.get("WHATSAPP_CLOUD_TOKEN") or ""
        pnid = os.environ.get("WHATSAPP_CLOUD_PHONE_NUMBER_ID") or ""
        if not token or not pnid:
            logger.warning(
                "WHATSAPP_PROVIDER=cloud but WHATSAPP_CLOUD_TOKEN or "
                "WHATSAPP_CLOUD_PHONE_NUMBER_ID is missing -> falling back to console"
            )
            return ConsoleProvider()
        version = os.environ.get("WHATSAPP_CLOUD_API_VERSION") or "v20.0"
        try:
            timeout = float(os.environ.get("WHATSAPP_CLOUD_TIMEOUT_S") or "8.0")
        except ValueError:
            timeout = 8.0
        return WhatsAppCloudProvider(
            token=token, phone_number_id=pnid, api_version=version, timeout_s=timeout
        )

    logger.warning("Unknown WHATSAPP_PROVIDER=%r; using console", name)
    return ConsoleProvider()


# ---- SafetyGuard singleton (lazy) -----------------------------------------

_safety_guard: Optional[SafetyGuard] = None


def get_safety_guard() -> SafetyGuard:
    global _safety_guard
    if _safety_guard is not None:
        return _safety_guard

    opt_out_path = Path(
        os.environ.get("DELIVERY_OPT_OUT_FILE") or _data_dir() / "opt_out.jsonl"
    )
    try:
        min_gap = int(os.environ.get("DELIVERY_RATE_LIMIT_PER_RECIPIENT_MIN") or "60") * 60
    except ValueError:
        min_gap = 3600
    try:
        daily_cap = int(os.environ.get("DELIVERY_DAILY_CAP") or "50")
    except ValueError:
        daily_cap = 50

    _safety_guard = SafetyGuard(
        opt_out=OptOutStore(opt_out_path),
        rate_limit=RateLimiter(
            min_gap_seconds_per_recipient=min_gap,
            daily_cap=daily_cap,
        ),
    )
    return _safety_guard
