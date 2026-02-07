from sqlalchemy import create_engine, Column, String, Integer, Float, Boolean, DateTime, JSON
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
from datetime import datetime
import os

DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    if os.path.exists("/data"):
        DATABASE_URL = "sqlite:////data/trust_bureau.db"
    else:
        DATABASE_URL = "sqlite:///./trust_bureau.db"

engine = create_engine(
    DATABASE_URL, connect_args={"check_same_thread": False} if "sqlite" in DATABASE_URL else {}
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()

class Agent(Base):
    __tablename__ = "agents"
    id = Column(String, primary_key=True, index=True) # URL
    trust_score = Column(Float, default=0.0)
    confidence = Column(String, default="low") # low, med, high
    capabilities = Column(JSON, default={}) # breakdown by tag
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

class ReputationEvent(Base):
    __tablename__ = "events"
    id = Column(Integer, primary_key=True, index=True)
    rater_id = Column(String, index=True)
    subject_id = Column(String, index=True)
    capability = Column(String, index=True)
    outcome = Column(Integer) # 1=Success, 0=Fail
    cost_usd = Column(Float, default=0.0)
    latency_ms = Column(Integer, nullable=True)
    timestamp = Column(DateTime, default=datetime.utcnow)

def init_db():
    Base.metadata.create_all(bind=engine)

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
