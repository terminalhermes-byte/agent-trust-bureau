from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class EventIn(BaseModel):
    event_id: str = Field(..., min_length=1)
    agent_id: str = Field(..., min_length=1)
    event_type: str = Field(..., min_length=1)
    source: str = Field(default="unknown", min_length=1)
    occurred_at: datetime
    metadata: dict[str, Any] = Field(default_factory=dict)


class EventAccepted(BaseModel):
    accepted: bool
    event_id: str
    agent_id: str


class AgentEventsResponse(BaseModel):
    agent_id: str
    event_count: int
    events: list[EventIn]


class TrustScoreResponse(BaseModel):
    agent_id: str
    trust_score: float
    trust_tier: str
    model_version: str
    factors: dict[str, float]
