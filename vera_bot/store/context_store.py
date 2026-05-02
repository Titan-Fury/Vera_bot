"""Context store - thread/async-safe in-memory store keyed by (scope, context_id).

Spec reference: `challenge-testing-brief.md` §2.1 / §3.

The judge pushes context deltas incrementally; versions are monotonic per
`(scope, context_id)` pair. A higher version atomically replaces the prior one.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Dict, Iterable, Optional, Tuple


VALID_SCOPES = {"category", "merchant", "customer", "trigger"}


@dataclass(frozen=True)
class StoredContext:
    version: int
    payload: Dict[str, Any]


class ContextStore:
    def __init__(self) -> None:
        self._data: Dict[Tuple[str, str], StoredContext] = {}
        self._lock = asyncio.Lock()

    async def put(self, scope: str, context_id: str, version: int, payload: Dict[str, Any]) -> Tuple[bool, Optional[int]]:
        """Attempt to store a context.

        Returns (accepted, current_version). `accepted=False` means stale;
        `current_version` is the version already on file.
        """
        async with self._lock:
            key = (scope, context_id)
            cur = self._data.get(key)
            if cur is not None and cur.version >= version:
                return False, cur.version
            self._data[key] = StoredContext(version=version, payload=payload)
            return True, version

    def get(self, scope: str, context_id: str) -> Optional[Dict[str, Any]]:
        cur = self._data.get((scope, context_id))
        return cur.payload if cur else None

    def get_version(self, scope: str, context_id: str) -> Optional[int]:
        cur = self._data.get((scope, context_id))
        return cur.version if cur else None

    def counts(self) -> Dict[str, int]:
        out = {s: 0 for s in VALID_SCOPES}
        for (scope, _), _ in self._data.items():
            out[scope] = out.get(scope, 0) + 1
        return out

    def iter_scope(self, scope: str) -> Iterable[Tuple[str, Dict[str, Any]]]:
        for (sc, cid), stored in self._data.items():
            if sc == scope:
                yield cid, stored.payload

    async def clear(self) -> None:
        async with self._lock:
            self._data.clear()
