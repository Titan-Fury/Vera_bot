"""Conversation store - per-conversation turn log + outbound metadata.

Tracks:
  * Full message history for replay/validation.
  * Outbound message bodies for anti-repetition (spec §10 penalty: -2 per verbatim repeat).
  * The triggering `trigger_id` + `merchant_id`/`customer_id` captured at /v1/tick
    so that /v1/reply can rebuild the composition bundle without guessing.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Dict, List, Optional


@dataclass
class Turn:
    role: str  # "vera" | "merchant_on_behalf" | "merchant" | "customer"
    body: str
    ts: Optional[str] = None


@dataclass
class ConversationMeta:
    merchant_id: Optional[str] = None
    customer_id: Optional[str] = None
    trigger_id: Optional[str] = None
    send_as: Optional[str] = None  # last outbound send_as


@dataclass
class Conversation:
    conversation_id: str
    meta: ConversationMeta = field(default_factory=ConversationMeta)
    turns: List[Turn] = field(default_factory=list)

    def outbound_bodies(self) -> List[str]:
        return [t.body for t in self.turns if t.role in {"vera", "merchant_on_behalf"}]

    def user_bodies(self) -> List[str]:
        return [t.body for t in self.turns if t.role in {"merchant", "customer"}]


class ConversationStore:
    def __init__(self) -> None:
        self._data: Dict[str, Conversation] = {}
        self._lock = asyncio.Lock()

    async def record_outbound(
        self,
        conversation_id: str,
        merchant_id: Optional[str],
        customer_id: Optional[str],
        trigger_id: Optional[str],
        send_as: str,
        body: str,
        ts: Optional[str] = None,
    ) -> None:
        async with self._lock:
            conv = self._data.setdefault(conversation_id, Conversation(conversation_id))
            conv.meta.merchant_id = merchant_id or conv.meta.merchant_id
            conv.meta.customer_id = customer_id or conv.meta.customer_id
            conv.meta.trigger_id = trigger_id or conv.meta.trigger_id
            conv.meta.send_as = send_as
            conv.turns.append(Turn(role=send_as, body=body, ts=ts))

    async def record_inbound(
        self,
        conversation_id: str,
        from_role: str,
        body: str,
        ts: Optional[str] = None,
    ) -> Conversation:
        async with self._lock:
            conv = self._data.setdefault(conversation_id, Conversation(conversation_id))
            conv.turns.append(Turn(role=from_role, body=body, ts=ts))
            return conv

    def get(self, conversation_id: str) -> Optional[Conversation]:
        return self._data.get(conversation_id)

    def was_already_sent(self, conversation_id: str, body: str) -> bool:
        conv = self._data.get(conversation_id)
        if not conv:
            return False
        return body.strip() in {b.strip() for b in conv.outbound_bodies()}

    async def clear(self) -> None:
        async with self._lock:
            self._data.clear()
