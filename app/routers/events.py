from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db import get_db
from app.schemas import AgentEventsResponse, EventAccepted, EventIn
from app.store import event_record_to_schema, insert_event, list_agent_events


router = APIRouter(prefix="/intake", tags=["intake"])


@router.post("/events", response_model=EventAccepted)
def ingest_event(event: EventIn, db: Session = Depends(get_db)) -> EventAccepted:
    try:
        insert_event(db, event)
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"event_id '{event.event_id}' already exists",
        ) from exc

    return EventAccepted(accepted=True, event_id=event.event_id, agent_id=event.agent_id)


@router.get("/events/{agent_id}", response_model=AgentEventsResponse)
def get_agent_events(agent_id: str, db: Session = Depends(get_db)) -> AgentEventsResponse:
    events = [event_record_to_schema(record) for record in list_agent_events(db, agent_id)]
    return AgentEventsResponse(agent_id=agent_id, event_count=len(events), events=events)
