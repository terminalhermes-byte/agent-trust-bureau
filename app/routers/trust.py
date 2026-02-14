from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.auth import AuthContext, require_api_key
from app.config import settings
from app.db import get_db
from app.rate_limit import check_score_rate_limit
from app.schemas import ScoreHistoryResponse, ScoreSnapshotOut, TrustScoreResponse
from app.services.scoring import calculate_trust_score
from app.store import list_agent_events, list_score_history, save_score_snapshot


router = APIRouter(prefix="/trust", tags=["trust"])


@router.get("/score/{agent_id}", response_model=TrustScoreResponse)
def get_trust_score(
    agent_id: str,
    auth: AuthContext = Depends(require_api_key),
    db: Session = Depends(get_db),
) -> TrustScoreResponse:
    if not check_score_rate_limit(agent_id):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Rate limit exceeded for agent '{agent_id}'. Try again later.",
        )
    events = list_agent_events(db, auth.tenant_id, agent_id)
    result = calculate_trust_score(events)
    save_score_snapshot(db, auth.tenant_id, agent_id, result)

    return TrustScoreResponse(
        agent_id=agent_id,
        trust_score=result.score,
        trust_tier=result.tier,
        model_version=settings.model_version,
        factors=result.factors,
    )


@router.get("/score/{agent_id}/history", response_model=ScoreHistoryResponse)
def get_score_history(
    agent_id: str,
    limit: int = Query(default=50, ge=1, le=500),
    before: datetime | None = Query(default=None),
    auth: AuthContext = Depends(require_api_key),
    db: Session = Depends(get_db),
) -> ScoreHistoryResponse:
    snapshots = list_score_history(db, auth.tenant_id, agent_id, limit=limit, before=before)
    items = [
        ScoreSnapshotOut(
            agent_id=s.agent_id,
            score=s.score,
            tier=s.tier,
            factors=s.factors,
            computed_at=s.computed_at,
        )
        for s in snapshots
    ]
    return ScoreHistoryResponse(agent_id=agent_id, count=len(items), snapshots=items)
