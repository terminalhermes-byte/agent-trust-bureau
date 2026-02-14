from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.config import settings
from app.db import get_db
from app.schemas import TrustScoreResponse
from app.services.scoring import calculate_trust_score
from app.store import list_agent_events, save_score_snapshot


router = APIRouter(prefix="/trust", tags=["trust"])


@router.get("/score/{agent_id}", response_model=TrustScoreResponse)
def get_trust_score(agent_id: str, db: Session = Depends(get_db)) -> TrustScoreResponse:
    events = list_agent_events(db, agent_id)
    result = calculate_trust_score(events)
    save_score_snapshot(db, agent_id, result)

    return TrustScoreResponse(
        agent_id=agent_id,
        trust_score=result.score,
        trust_tier=result.tier,
        model_version=settings.model_version,
        factors=result.factors,
    )
