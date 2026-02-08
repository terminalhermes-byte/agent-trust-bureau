from fastapi import APIRouter, Depends, HTTPException, Response, Request, BackgroundTasks
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from sqlalchemy import func
from pydantic import BaseModel, Field
from typing import List, Optional
import math
import os

from src.database import get_db, ReputationEvent, Agent
from src.svg_utils import generate_shield_svg
from src.auth import require_api_key
from src.rate_limit import limiter, AUTHENTICATED_LIMIT, PUBLIC_LIMIT

router = APIRouter()
templates = Jinja2Templates(directory=os.path.join(os.path.dirname(__file__), "templates"))


# --- Pydantic Models ---
class EventIngest(BaseModel):
    rater_id: str
    subject_id: str  # URL of the agent
    capability: str
    outcome: int = Field(..., ge=0, le=1)  # 0 or 1
    cost_usd: float = 0.0
    latency_ms: Optional[int] = None
    signature: Optional[str] = None  # Placeholder for HMAC


class ScoreResponse(BaseModel):
    agent_id: str
    trust_score: float
    confidence: str
    breakdown: dict


# --- Logic (Wilson Score) ---
def calculate_wilson_score(positive: int, total: int, confidence: float = 0.95) -> float:
    """
    Calculate Wilson score interval lower bound.
    Good for ranking with low sample sizes - balances rating count vs average.
    """
    if total == 0:
        return 0.0
    z = 1.96  # 95% confidence
    phat = positive / total
    denominator = 1 + z * z / total
    numerator = phat + z * z / (2 * total) - z * math.sqrt((phat * (1 - phat) + z * z / (4 * total)) / total)
    return numerator / denominator


def recompute_agent_scores(agent_ids: set[str], db: Session):
    """
    Recompute trust scores for given agents.
    Called as background task after event ingestion.
    """
    for agent_id in agent_ids:
        all_events = db.query(ReputationEvent).filter(ReputationEvent.subject_id == agent_id).all()
        
        total = len(all_events)
        positive = sum(1 for ev in all_events if ev.outcome == 1)
        
        # Calculate Wilson Score (0-100 scale)
        raw_score = calculate_wilson_score(positive, total) * 100
        
        # Confidence based on sample size
        if total > 50:
            conf = "high"
        elif total > 10:
            conf = "med"
        else:
            conf = "low"
        
        # Calculate capability breakdown
        capabilities = {}
        capability_events = {}
        for ev in all_events:
            if ev.capability not in capability_events:
                capability_events[ev.capability] = {"positive": 0, "total": 0}
            capability_events[ev.capability]["total"] += 1
            if ev.outcome == 1:
                capability_events[ev.capability]["positive"] += 1
        
        for cap, stats in capability_events.items():
            capabilities[cap] = {
                "score": round(calculate_wilson_score(stats["positive"], stats["total"]) * 100, 1),
                "total": stats["total"],
                "success_rate": round(stats["positive"] / stats["total"] * 100, 1) if stats["total"] > 0 else 0
            }
        
        # Upsert Agent
        agent = db.query(Agent).filter(Agent.id == agent_id).first()
        if not agent:
            agent = Agent(id=agent_id)
            db.add(agent)
        
        agent.trust_score = raw_score
        agent.confidence = conf
        agent.capabilities = capabilities
    
    db.commit()


# --- Endpoints ---

@router.post("/events", status_code=201)
@limiter.limit(AUTHENTICATED_LIMIT)
def ingest_events(
    request: Request,
    events: List[EventIngest],
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    api_key: str = Depends(require_api_key)
):
    """
    Ingest reputation events. Requires API key authentication.
    Score recomputation happens in background.
    """
    new_events = []
    agent_ids = set()
    
    for e in events:
        db_event = ReputationEvent(
            rater_id=e.rater_id,
            subject_id=e.subject_id,
            capability=e.capability,
            outcome=e.outcome,
            cost_usd=e.cost_usd,
            latency_ms=e.latency_ms
        )
        new_events.append(db_event)
        agent_ids.add(e.subject_id)
    
    db.add_all(new_events)
    db.commit()
    
    # Schedule score recomputation in background
    # Note: We need a new db session for background task
    background_tasks.add_task(recompute_scores_background, agent_ids)
    
    return {
        "message": f"Ingested {len(events)} events",
        "agents_affected": list(agent_ids),
        "scores_updating": True
    }


def recompute_scores_background(agent_ids: set[str]):
    """Background task wrapper that creates its own DB session."""
    from src.database import SessionLocal
    db = SessionLocal()
    try:
        recompute_agent_scores(agent_ids, db)
    finally:
        db.close()


@router.get("/scores/{agent_id}", response_model=ScoreResponse)
@limiter.limit(PUBLIC_LIMIT)
def get_score(request: Request, agent_id: str, db: Session = Depends(get_db)):
    """Get trust score for an agent. Public endpoint."""
    agent = db.query(Agent).filter(Agent.id == agent_id).first()
    if not agent:
        return ScoreResponse(agent_id=agent_id, trust_score=0.0, confidence="none", breakdown={})
    
    return ScoreResponse(
        agent_id=agent.id,
        trust_score=round(agent.trust_score, 1),
        confidence=agent.confidence,
        breakdown=agent.capabilities or {}
    )


@router.get("/badge/{agent_id}", response_class=Response)
@limiter.limit(PUBLIC_LIMIT)
def get_badge(request: Request, agent_id: str, db: Session = Depends(get_db)):
    """Get SVG trust badge for an agent. Public endpoint."""
    agent = db.query(Agent).filter(Agent.id == agent_id).first()
    
    if not agent:
        return Response(
            content=generate_shield_svg("trust", "unverified", "#9f9f9f"),
            media_type="image/svg+xml"
        )
    
    score = int(agent.trust_score)
    
    # Color based on score
    if score >= 90:
        color = "#4c1"  # bright green
    elif score >= 80:
        color = "#97ca00"  # green
    elif score >= 60:
        color = "#dfb317"  # yellow
    else:
        color = "#e05d44"  # red
    
    status_text = f"{score}%"
    if agent.confidence == "low":
        status_text += " (low)"
    
    svg = generate_shield_svg("trust", status_text, color)
    return Response(content=svg, media_type="image/svg+xml")


# --- Dashboard & Stats ---

@router.get("/dashboard", response_class=HTMLResponse)
@limiter.limit(PUBLIC_LIMIT)
def get_dashboard(request: Request):
    """Dashboard UI. Public endpoint."""
    return templates.TemplateResponse("dashboard.html", {"request": request})


@router.get("/dashboard/stats")
@limiter.limit(PUBLIC_LIMIT)
def get_dashboard_stats(request: Request, db: Session = Depends(get_db)):
    """Dashboard stats API. Public endpoint."""
    total_events = db.query(ReputationEvent).count()
    total_agents = db.query(Agent).count()
    avg_score = db.query(func.avg(Agent.trust_score)).scalar() or 0
    
    recent_events = db.query(ReputationEvent)\
        .order_by(ReputationEvent.timestamp.desc())\
        .limit(10)\
        .all()
    
    top_agents = db.query(Agent)\
        .filter(Agent.trust_score > 0)\
        .order_by(Agent.trust_score.desc())\
        .limit(5)\
        .all()
    
    return {
        "stats": {
            "total_events": total_events,
            "total_agents": total_agents,
            "avg_score": round(avg_score, 1)
        },
        "recent_events": recent_events,
        "top_agents": top_agents
    }
