"""Force dummy provider for tests so they are hermetic (no network).

The production `vera_bot.config` calls `load_dotenv(override=True)` at import
time, which would clobber any env we set here. So we:
  1. Set our overrides first (belt)
  2. Patch `load_dotenv` to a no-op (braces), so the real `.env` keys can't
     override us
"""
import os

os.environ["LLM_PROVIDER"] = "dummy"
os.environ.pop("LLM_FALLBACKS", None)
os.environ.pop("LLM_MODEL", None)

try:
    import dotenv  # type: ignore
    dotenv.load_dotenv = lambda *a, **kw: False  # type: ignore[attr-defined]
except Exception:  # pragma: no cover
    pass
