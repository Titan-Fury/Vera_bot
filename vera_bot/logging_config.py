"""Single place to configure logging for the whole process."""

from __future__ import annotations

import logging
import sys

from .config import SETTINGS


def configure_logging() -> None:
    root = logging.getLogger()
    # Avoid duplicate handlers when uvicorn reloads.
    if getattr(configure_logging, "_done", False):
        return
    level = getattr(logging, SETTINGS.log_level, logging.INFO)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    )
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)

    # Quiet noisy libs in INFO mode.
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)

    configure_logging._done = True  # type: ignore[attr-defined]
