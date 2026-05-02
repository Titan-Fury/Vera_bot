"""Pydantic request/response models mirroring challenge-testing-brief.md §3."""

from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field

Scope = Literal["category", "merchant", "customer", "trigger"]


# ---- /v1/context -------------------------------------------------------------

class CtxBody(BaseModel):
    scope: Scope
    context_id: str = Field(..., min_length=1)
    version: int = Field(..., ge=0)
    payload: Dict[str, Any]
    delivered_at: str


class CtxAccepted(BaseModel):
    accepted: Literal[True] = True
    ack_id: str
    stored_at: str


class CtxRejected(BaseModel):
    accepted: Literal[False] = False
    reason: str
    current_version: Optional[int] = None
    details: Optional[Dict[str, Any]] = None


# ---- /v1/tick ----------------------------------------------------------------

class TickBody(BaseModel):
    now: str
    available_triggers: List[str] = Field(default_factory=list)


class TickAction(BaseModel):
    conversation_id: str
    merchant_id: str
    customer_id: Optional[str] = None
    send_as: str
    trigger_id: str
    template_name: str
    template_params: List[str] = Field(default_factory=list)
    body: str
    cta: str
    suppression_key: str
    rationale: str


class TickResponse(BaseModel):
    actions: List[TickAction]


# ---- /v1/reply ---------------------------------------------------------------

class ReplyBody(BaseModel):
    conversation_id: str = Field(..., min_length=1)
    merchant_id: Optional[str] = None
    customer_id: Optional[str] = None
    from_role: str
    message: str
    received_at: str
    turn_number: int = Field(..., ge=1)


class ReplyResponseSend(BaseModel):
    action: Literal["send"]
    body: str
    cta: str
    rationale: str


class ReplyResponseWait(BaseModel):
    action: Literal["wait"]
    wait_seconds: int
    rationale: str


class ReplyResponseEnd(BaseModel):
    action: Literal["end"]
    rationale: str


# ---- /v1/metadata ------------------------------------------------------------

class MetadataResponse(BaseModel):
    team_name: str
    team_members: List[str]
    model: str
    approach: str
    contact_email: str
    version: str
    submitted_at: str


# ---- /v1/healthz -------------------------------------------------------------

class HealthResponse(BaseModel):
    status: Literal["ok"] = "ok"
    uptime_seconds: int
    contexts_loaded: Dict[str, int]
    composer_version: str
    provider: str
