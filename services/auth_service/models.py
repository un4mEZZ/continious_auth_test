from datetime import datetime
from sqlalchemy import Column, String, DateTime, Integer, Float, Boolean
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import declarative_base

Base = declarative_base()

class BehaviorProfile(Base):
    __tablename__ = "behavior_profiles"

    username = Column(String, primary_key=True)
    features = Column(JSONB)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

class AuthEvent(Base):
    __tablename__ = "auth_events"

    id = Column(Integer, primary_key=True, index=True)
    username = Column(String, nullable=False)
    score = Column(Float)
    decision = Column(Boolean)
    ts = Column(DateTime, default=datetime.utcnow)
