"""Entry point: `python -m vera_bot`."""

from __future__ import annotations

import os

import uvicorn

from .logging_config import configure_logging


def main() -> None:
    configure_logging()
    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "8080"))
    # Single worker is required because state is in-memory (see README).
    uvicorn.run("vera_bot.api.app:app", host=host, port=port, workers=1, log_level="info")


if __name__ == "__main__":
    main()
