from fastapi import APIRouter, Depends, HTTPException, Header, Response, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from sqlalchemy import func, desc
from pydantic import BaseModel, HttpUrl, Field
from typing import List, Optional
from datetime import datetime
from src.database import get_db, ReputationEvent, Agent
from src.svg_utils import generate_shield_svg
import math
import os

router = APIRouter()
templates = Jinja2Templates(directory=os.path.join(os.path.dirname(__file__), "templates"))

# --- Pydantic Models ---
class EventIngest(BaseModel):
    rater_id: str
    subject_id: str # URL of the agent
    capability: str
    outcome: int = Field(..., ge=0, le=1) # 0 or 1
    cost_usd: float = 0.0
    latency_ms: Optional[int] = None
    signature: Optional[str] = None # Placeholder for HMAC

class ScoreResponse(BaseModel):
    agent_id: str
    trust_score: float
    confidence: str
    breakdown: dict

# --- Logic (Wilson Score) ---
def calculate_wilson_score(positive, total, confidence=0.95):
    if total == 0: return 0
    z = 1.96 # 95% confidence
    phat = positive / total
    return (phat + z*z/(2*total) - z * math.sqrt((phat*(1-phat)+z*z/(4*total))/total))/(1+z*z/total)

# --- Endpoints ---

@router.post("/events", status_code=201)
def ingest_events(events: List[EventIngest], db: Session = Depends(get_db)):
    # TODO: Validate API Key / Signature here
    
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
    
    # Simple Recalc Trigger (In prod, move to background worker)
    for agent_id in agent_ids:
        # Get all events for this agent
        all_events = db.query(ReputationEvent).filter(ReputationEvent.subject_id == agent_id).all()
        
        total = len(all_events)
        positive = sum(1 for ev in all_events if ev.outcome == 1)
        
        # Calculate Base Score (ignoring capability breakdown for MVP main score)
        raw_score = calculate_wilson_score(positive, total) * 100
        
        # Confidence Logic
        conf = "low"
        if total > 50: conf = "high"
        elif total > 10: conf = "med"
        
        # Upsert Agent
        agent = db.query(Agent).filter(Agent.id == agent_id).first()
        if not agent:
            agent = Agent(id=agent_id)
            db.add(agent)
        
        agent.trust_score = raw_score
        agent.confidence = conf
        # breakdown TODO
    
    db.commit()
    return {"message": f"Ingested {len(events)} events"}

@router.get("/scores/{agent_id}", response_model=ScoreResponse)
def get_score(agent_id: str, db: Session = Depends(get_db)):
    # URL decode handled by FastAPI if passed correctly, but caution on path params with URLs
    # Better: use query param, but sticking to spec: /scores/{agent_id} needs encoding
    
    # Actually, simpler to use query param for URLs to avoid slash issues
    # But let's check DB
    agent = db.query(Agent).filter(Agent.id == agent_id).first()
    if not agent:
        return ScoreResponse(agent_id=agent_id, trust_score=0.0, confidence="none", breakdown={})
    
    return ScoreResponse(
        agent_id=agent.id,
        trust_score=agent.trust_score,
        confidence=agent.confidence,
        breakdown=agent.capabilities or {}
    )

@router.get("/badge/{agent_id}", response_class=Response)
def get_badge(agent_id: str, db: Session = Depends(get_db)):
    # Note: agent_id in path might need double encoding or be passed as query param in real world.
    # For this MVP, we assume the client encodes it properly.
    
    agent = db.query(Agent).filter(Agent.id == agent_id).first()
    
    if not agent:
        return Response(content=generate_shield_svg("trust", "unverified", "#9f9f9f"), media_type="image/svg+xml")
    
    score = int(agent.trust_score)
    
    # Color logic
    color = "#e05d44" # red
    if score >= 90: color = "#4c1" # bright green
    elif score >= 80: color = "#97ca00" # green
    elif score >= 60: color = "#dfb317" # yellow
    
    # Text logic
    status_text = f"{score}%"
    if agent.confidence == "low":
        status_text += " (low conf)"
    
    svg = generate_shield_svg("trust", status_text, color)
    
    return Response(content=svg, media_type="image/svg+xml")

# --- Dashboard & Stats ---

@router.get("/dashboard", response_class=HTMLResponse)
def get_dashboard(request: Request):
    return templates.TemplateResponse("dashboard.html", {"request": request})

@router.get("/dashboard/stats")
def get_dashboard_stats(db: Session = Depends(get_db)):
    # 1. Basic Counts
    total_events = db.query(ReputationEvent).count()
    total_agents = db.query(Agent).count()
    
    # Avg Score (handle 0 agents)
    avg_score = db.query(func.avg(Agent.trust_score)).scalar() or 0
    
    # 2. Recent Events (Last 10)
    recent_events = db.query(ReputationEvent)\
        .order_by(ReputationEvent.timestamp.desc())\
        .limit(10)\
        .all()
        
    # 3. Top Agents (Top 5 by score, where score > 0)
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
