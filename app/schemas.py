from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, Field, model_validator


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


class ScoreSnapshotOut(BaseModel):
    agent_id: str
    score: float
    tier: str
    factors: dict[str, float]
    computed_at: datetime


class ScoreHistoryResponse(BaseModel):
    agent_id: str
    count: int
    snapshots: list[ScoreSnapshotOut]


class ThresholdsOut(BaseModel):
    allow: float
    review: float
    block: float


class PolicyDecisionResponse(BaseModel):
    agent_id: str
    decision: str
    score: float
    thresholds: ThresholdsOut
    explanation: str


# ---------------------------------------------------------------------------
# Admin: Policy Config
# ---------------------------------------------------------------------------

class _ThresholdValidatorMixin(BaseModel):
    """Shared validation: allow >= review >= block, all in [0, 100]."""

    @model_validator(mode="after")
    def _check_threshold_ordering(self) -> _ThresholdValidatorMixin:
        # Collect the effective values (skip None for partial-override schemas)
        allow = getattr(self, "allow_threshold", None)
        review = getattr(self, "review_threshold", None)
        block = getattr(self, "block_threshold", None)

        for name, val in [("allow_threshold", allow), ("review_threshold", review), ("block_threshold", block)]:
            if val is not None and not (0 <= val <= 100):
                raise ValueError(f"{name} must be between 0 and 100")

        if allow is not None and review is not None and allow < review:
            raise ValueError("allow_threshold must be >= review_threshold")
        if review is not None and block is not None and review < block:
            raise ValueError("review_threshold must be >= block_threshold")
        if allow is not None and block is not None and allow < block:
            raise ValueError("allow_threshold must be >= block_threshold")

        return self


class PolicyConfigUpdate(_ThresholdValidatorMixin):
    allow_threshold: float = Field(..., ge=0, le=100)
    review_threshold: float = Field(..., ge=0, le=100)
    block_threshold: float = Field(..., ge=0, le=100)


class PolicyConfigOut(BaseModel):
    allow_threshold: float
    review_threshold: float
    block_threshold: float
    created_at: datetime
    updated_at: datetime


# ---------------------------------------------------------------------------
# Admin: Agent Policy Overrides
# ---------------------------------------------------------------------------

class AgentOverrideCreate(_ThresholdValidatorMixin):
    agent_id: str = Field(..., min_length=1)
    allow_threshold: Optional[float] = Field(None, ge=0, le=100)
    review_threshold: Optional[float] = Field(None, ge=0, le=100)
    block_threshold: Optional[float] = Field(None, ge=0, le=100)


class AgentOverrideOut(BaseModel):
    agent_id: str
    allow_threshold: Optional[float]
    review_threshold: Optional[float]
    block_threshold: Optional[float]
    created_at: datetime
    updated_at: datetime


class AgentOverrideListResponse(BaseModel):
    count: int
    overrides: list[AgentOverrideOut]


# ---------------------------------------------------------------------------
# Admin: Webhooks
# ---------------------------------------------------------------------------

class WebhookCreate(BaseModel):
    url: str = Field(..., min_length=1)


class WebhookPatch(BaseModel):
    enabled: Optional[bool] = None
    rotate_secret: Optional[bool] = None


class WebhookOut(BaseModel):
    id: int
    url: str
    secret_last4: str
    # Only returned on create/rotate; otherwise None.
    secret: Optional[str] = None
    enabled: bool
    revoked_at: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime


class WebhookListResponse(BaseModel):
    count: int
    webhooks: list[WebhookOut]
