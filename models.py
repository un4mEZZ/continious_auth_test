from datetime import datetime
from uuid import uuid4
from sqlalchemy import Column, String, DateTime, ForeignKey, Text, Integer, Float, Boolean
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID
from sqlalchemy.orm import declarative_base

Base = declarative_base()

# Схемы данных
class User(Base):
    __tablename__ = "users"
    __table_args__ = {"schema": "auth"}

    uuid = Column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    login = Column(String, unique=True, nullable=False)
    password = Column(String, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)

class Comment(Base):
    __tablename__ = "comments"
    __table_args__ = {"schema": "auth"}

    id = Column(Integer, primary_key=True, index=True)
    target_user_login = Column(String, ForeignKey("auth.users.login"), nullable=False)
    author_login = Column(String, nullable=False)
    text = Column(Text, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)

class BehaviorProfile(Base):
    __tablename__ = "behavior_profiles"
    __table_args__ = {"schema": "auth"}

    user_id = Column(PG_UUID(as_uuid=True), ForeignKey("auth.users.uuid"), primary_key=True)
    features = Column(JSONB)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

class AuthEvent(Base):
    __tablename__ = "auth_events"
    __table_args__ = {"schema": "auth"}

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(PG_UUID(as_uuid=True), ForeignKey("auth.users.uuid"), nullable=False)
    score = Column(Float)
    decision = Column(Boolean)
    ts = Column(DateTime, default=datetime.utcnow)