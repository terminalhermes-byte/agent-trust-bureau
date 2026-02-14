from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.auth import AuthContext, require_api_key
from app.db import get_db
from app.schemas import PolicyDecisionResponse, ThresholdsOut
from app.services.policy import evaluate_policy
from app.services.scoring import calculate_trust_score
from app.services.webhook import send_webhook
from app.store import list_agent_events, save_score_snapshot

router = APIRouter(prefix="/policy", tags=["policy"])


@router.get("/decision/{agent_id}", response_model=PolicyDecisionResponse)
def get_policy_decision(
    agent_id: str,
    auth: AuthContext = Depends(require_api_key),
    db: Session = Depends(get_db),
) -> PolicyDecisionResponse:
    # 1. Compute trust score (same logic as /v1/trust/score/{agent_id})
    events = list_agent_events(db, auth.tenant_id, agent_id)
    result = calculate_trust_score(events)
    save_score_snapshot(db, auth.tenant_id, agent_id, result)

    # 2. Evaluate against policy thresholds
    decision = evaluate_policy(db, auth.tenant_id, agent_id, result.score)

    # 3. Fire webhook (best-effort, non-blocking for the response)
    send_webhook(db, auth.tenant_id, agent_id, decision)

    return PolicyDecisionResponse(
        agent_id=agent_id,
        decision=decision.decision,
        score=decision.score,
        thresholds=ThresholdsOut(
            allow=decision.thresholds.allow,
            review=decision.thresholds.review,
            block=decision.thresholds.block,
        ),
        explanation=decision.explanation,
    )
