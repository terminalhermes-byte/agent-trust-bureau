from __future__ import annotations

from sqlalchemy import Select, select
from sqlalchemy.orm import Session

from app.models import EventRecord, ScoreSnapshot
from app.schemas import EventIn
from app.services.scoring import ScoreResult


def insert_event(db: Session, event: EventIn) -> EventRecord:
    record = EventRecord(
        event_id=event.event_id,
        agent_id=event.agent_id,
        event_type=event.event_type,
        source=event.source,
        occurred_at=event.occurred_at,
        metadata_json=event.metadata,
    )
    db.add(record)
    db.commit()
    db.refresh(record)
    return record


def list_agent_events(db: Session, agent_id: str) -> list[EventRecord]:
    stmt: Select[tuple[EventRecord]] = (
        select(EventRecord)
        .where(EventRecord.agent_id == agent_id)
        .order_by(EventRecord.occurred_at.asc(), EventRecord.id.asc())
    )
    return list(db.scalars(stmt).all())


def save_score_snapshot(db: Session, agent_id: str, result: ScoreResult) -> ScoreSnapshot:
    snapshot = ScoreSnapshot(
        agent_id=agent_id,
        score=result.score,
        tier=result.tier,
        factors=result.factors,
    )
    db.add(snapshot)
    db.commit()
    db.refresh(snapshot)
    return snapshot


def event_record_to_schema(record: EventRecord) -> EventIn:
    return EventIn(
        event_id=record.event_id,
        agent_id=record.agent_id,
        event_type=record.event_type,
        source=record.source,
        occurred_at=record.occurred_at,
        metadata=record.metadata_json,
    )
