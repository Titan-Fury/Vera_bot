"""Delivery layer - send composed messages over real channels.

Public surface:
    - DeliveryProvider (abstract)
    - DeliveryResult (success / error normalization)
    - get_provider() factory reading WHATSAPP_PROVIDER env var

Why a layer of indirection:
    The composer produces a Composition. Production needs to actually deliver
    that body to a phone number, with safety guards (opt-out, rate-limit, daily
    cap) applied uniformly regardless of which underlying API we use. Adding a
    new BSP (Gupshup, 360dialog, Twilio) means writing one adapter, not
    refactoring the call sites.
"""

from .base import DeliveryProvider, DeliveryResult, DeliveryError
from .factory import get_provider, get_safety_guard
from .safety import SafetyGuard, OptOutStore, RateLimiter, SafetyVerdict
from .send_log import SendLog, get_send_log

__all__ = [
    "DeliveryProvider",
    "DeliveryResult",
    "DeliveryError",
    "get_provider",
    "get_safety_guard",
    "SafetyGuard",
    "OptOutStore",
    "RateLimiter",
    "SafetyVerdict",
    "SendLog",
    "get_send_log",
]
