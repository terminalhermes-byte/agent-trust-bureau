"""Policy evaluation engine.

Resolves thresholds (tenant default → agent override) and maps a trust score
to a policy decision: allow, review, or block.
"""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AgentPolicyOverride, PolicyConfig


# Hard-coded fallbacks if no PolicyConfig row exists for the tenant.
DEFAULT_ALLOW = 80.0
DEFAULT_REVIEW = 60.0
DEFAULT_BLOCK = 40.0


@dataclass
class Thresholds:
    allow: float
    review: float
    block: float


@dataclass
class PolicyDecision:
    decision: str  # "allow" | "review" | "block"
    score: float
    thresholds: Thresholds
    explanation: str


def _resolve_thresholds(db: Session, tenant_id: int, agent_id: str) -> Thresholds:
    """Load tenant-level thresholds, then apply any agent-level overrides."""
    cfg = db.scalars(
        select(PolicyConfig).where(PolicyConfig.tenant_id == tenant_id)
    ).first()

    allow = cfg.allow_threshold if cfg else DEFAULT_ALLOW
    review = cfg.review_threshold if cfg else DEFAULT_REVIEW
    block = cfg.block_threshold if cfg else DEFAULT_BLOCK

    override = db.scalars(
        select(AgentPolicyOverride).where(
            AgentPolicyOverride.tenant_id == tenant_id,
            AgentPolicyOverride.agent_id == agent_id,
        )
    ).first()

    if override:
        if override.allow_threshold is not None:
            allow = override.allow_threshold
        if override.review_threshold is not None:
            review = override.review_threshold
        if override.block_threshold is not None:
            block = override.block_threshold

    return Thresholds(allow=allow, review=review, block=block)


def evaluate_policy(db: Session, tenant_id: int, agent_id: str, score: float) -> PolicyDecision:
    """Evaluate a trust score against thresholds and return a structured decision."""
    thresholds = _resolve_thresholds(db, tenant_id, agent_id)

    if score >= thresholds.allow:
        decision = "allow"
        explanation = f"Score {score} >= allow threshold {thresholds.allow}"
    elif score >= thresholds.review:
        decision = "review"
        explanation = f"Score {score} >= review threshold {thresholds.review} but < allow threshold {thresholds.allow}"
    elif score >= thresholds.block:
        decision = "review"
        explanation = f"Score {score} >= block threshold {thresholds.block} but < review threshold {thresholds.review}"
    else:
        decision = "block"
        explanation = f"Score {score} < block threshold {thresholds.block}"

    return PolicyDecision(
        decision=decision,
        score=score,
        thresholds=thresholds,
        explanation=explanation,
    )
