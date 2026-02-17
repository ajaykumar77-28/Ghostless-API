"""
Ghostless API — Database Models
"""
from sqlalchemy import (
    Column, String, Float, Integer, Boolean, DateTime,
    ForeignKey, Text, JSON, Numeric, Enum as SAEnum, Index
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from app.database import Base
import uuid
import enum


# ─── Enums ────────────────────────────────────────────────────────────────────

class TierEnum(str, enum.Enum):
    bronze = "bronze"
    silver = "silver"
    gold   = "gold"
    elite  = "elite"


class StatusEnum(str, enum.Enum):
    active    = "active"
    suspended = "suspended"
    banned    = "banned"


class SeverityEnum(str, enum.Enum):
    info    = "info"
    warning = "warning"
    error   = "error"


class TenantTierEnum(str, enum.Enum):
    starter    = "starter"
    growth     = "growth"
    enterprise = "enterprise"


# ─── Tenants ──────────────────────────────────────────────────────────────────

class Tenant(Base):
    __tablename__ = "tenants"

    id              = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name            = Column(String(200), nullable=False)
    slug            = Column(String(100), unique=True, nullable=False)   # "acme-corp"
    tier            = Column(SAEnum(TenantTierEnum), default=TenantTierEnum.starter)
    api_key_hash    = Column(String(64), unique=True)                    # SHA-256 of raw key
    webhook_url     = Column(String(500))
    webhook_secret  = Column(String(64))
    webhook_events  = Column(JSON, default=list)
    brand_name      = Column(String(200))                                 # white-label
    brand_color     = Column(String(7))                                   # hex color
    is_active       = Column(Boolean, default=True)
    created_at      = Column(DateTime(timezone=True), server_default=func.now())
    metadata_       = Column("metadata", JSON, default=dict)

    workers   = relationship("Worker",    back_populates="tenant")
    api_keys  = relationship("APIKey",    back_populates="tenant")


class APIKey(Base):
    """Supports multiple keys per tenant (rotation, per-service keys)."""
    __tablename__ = "api_keys"

    id          = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id   = Column(UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False)
    name        = Column(String(100))              # "production-key", "staging-key"
    key_hash    = Column(String(64), unique=True)  # SHA-256
    key_prefix  = Column(String(20))               # first 12 chars for UI display
    is_active   = Column(Boolean, default=True)
    last_used_at = Column(DateTime(timezone=True))
    created_at  = Column(DateTime(timezone=True), server_default=func.now())
    expires_at  = Column(DateTime(timezone=True))

    tenant = relationship("Tenant", back_populates="api_keys")


# ─── Workers ──────────────────────────────────────────────────────────────────

class Worker(Base):
    __tablename__ = "workers"

    id          = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id   = Column(UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False)
    external_id = Column(String(200), nullable=False)   # client's own ID
    tier        = Column(SAEnum(TierEnum), default=TierEnum.bronze)
    status      = Column(SAEnum(StatusEnum), default=StatusEnum.active)
    created_at  = Column(DateTime(timezone=True), server_default=func.now())
    metadata_   = Column("metadata", JSON, default=dict)

    # Relationships
    tenant      = relationship("Tenant",      back_populates="workers")
    tasks       = relationship("Task",        back_populates="worker")
    score       = relationship("WorkerScore", back_populates="worker", uselist=False)
    promotions  = relationship("Promotion",   back_populates="worker")
    messages    = relationship("Message",     back_populates="worker")

    __table_args__ = (
        Index("ix_workers_tenant_external", "tenant_id", "external_id", unique=True),
    )


class WorkerScore(Base):
    __tablename__ = "worker_scores"

    worker_id           = Column(UUID(as_uuid=True), ForeignKey("workers.id"), primary_key=True)
    tenant_id           = Column(UUID(as_uuid=True), nullable=False)
    trust_score         = Column(Float, default=50.0)
    accuracy_7d         = Column(Float)
    accuracy_30d        = Column(Float)
    accuracy_all        = Column(Float)
    avg_quality_score   = Column(Float)
    total_tasks         = Column(Integer, default=0)
    accepted_tasks      = Column(Integer, default=0)
    speed_flag_count    = Column(Integer, default=0)
    fraud_flag_count    = Column(Integer, default=0)
    streak_days         = Column(Integer, default=0)
    last_active_date    = Column(DateTime(timezone=True))
    last_calculated_at  = Column(DateTime(timezone=True))

    worker = relationship("Worker", back_populates="score")

    __table_args__ = (
        Index("ix_worker_scores_tenant_trust", "tenant_id", "trust_score"),
    )


class Promotion(Base):
    __tablename__ = "promotions"

    id             = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    worker_id      = Column(UUID(as_uuid=True), ForeignKey("workers.id"), nullable=False)
    from_tier      = Column(SAEnum(TierEnum))
    to_tier        = Column(SAEnum(TierEnum))
    reason         = Column(String(500))
    triggered_at   = Column(DateTime(timezone=True), server_default=func.now())
    trust_score_at = Column(Float)

    worker = relationship("Worker", back_populates="promotions")


# ─── Tasks ────────────────────────────────────────────────────────────────────

class Task(Base):
    __tablename__ = "tasks"

    id              = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id       = Column(UUID(as_uuid=True), nullable=False)
    worker_id       = Column(UUID(as_uuid=True), ForeignKey("workers.id"))
    task_type       = Column(String(100), nullable=False)
    project_id      = Column(String(200))
    submitted_at    = Column(DateTime(timezone=True), server_default=func.now())
    completion_time = Column(Float)             # seconds
    was_accepted    = Column(Boolean)           # NULL = pending
    payout_amount   = Column(Numeric(10, 4), default=0)
    metadata_       = Column("metadata", JSON, default=dict)

    worker      = relationship("Worker",     back_populates="tasks")
    validation  = relationship("Validation", back_populates="task", uselist=False)

    __table_args__ = (
        Index("ix_tasks_worker_submitted", "worker_id", "submitted_at"),
        Index("ix_tasks_tenant_submitted", "tenant_id", "submitted_at"),
    )


class Validation(Base):
    __tablename__ = "validations"

    id            = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    task_id       = Column(UUID(as_uuid=True), ForeignKey("tasks.id"))
    worker_id     = Column(UUID(as_uuid=True), ForeignKey("workers.id"))
    tenant_id     = Column(UUID(as_uuid=True), nullable=False)
    quality_score = Column(Float, nullable=False)
    warning_count = Column(Integer, default=0)
    error_count   = Column(Integer, default=0)
    was_allowed   = Column(Boolean, nullable=False)
    validated_at  = Column(DateTime(timezone=True), server_default=func.now())
    rule_flags    = Column(JSON, default=list)   # ["SPEED_FLAG", "STRAIGHT_LINE"]
    processed_ms  = Column(Integer)

    task   = relationship("Task",   back_populates="validation")

    __table_args__ = (
        Index("ix_validations_worker_date", "worker_id", "validated_at"),
        Index("ix_validations_tenant_date", "tenant_id", "validated_at"),
    )


# ─── Hub / Community ──────────────────────────────────────────────────────────

class Message(Base):
    __tablename__ = "messages"

    id          = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id   = Column(UUID(as_uuid=True), nullable=False)
    worker_id   = Column(UUID(as_uuid=True), ForeignKey("workers.id"))
    project_id  = Column(String(200))
    room        = Column(String(200))         # "project:abc" or "dm:w1:w2"
    content     = Column(Text, nullable=False)
    msg_type    = Column(String(50), default="chat")  # chat | help | system | kudos
    reply_to_id = Column(UUID(as_uuid=True), ForeignKey("messages.id"))
    is_moderated = Column(Boolean, default=False)
    created_at  = Column(DateTime(timezone=True), server_default=func.now())

    worker = relationship("Worker", back_populates="messages")

    __table_args__ = (
        Index("ix_messages_room_created", "room", "created_at"),
    )


class BugReport(Base):
    __tablename__ = "bug_reports"

    id          = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id   = Column(UUID(as_uuid=True), nullable=False)
    worker_id   = Column(UUID(as_uuid=True), ForeignKey("workers.id"))
    project_id  = Column(String(200))
    title       = Column(String(500), nullable=False)
    description = Column(Text)
    severity    = Column(String(20), default="medium")  # low|medium|high|critical
    status      = Column(String(20), default="open")    # open|in_progress|resolved
    ticket_id   = Column(String(50), unique=True)
    screenshot_url = Column(String(1000))
    created_at  = Column(DateTime(timezone=True), server_default=func.now())
    resolved_at = Column(DateTime(timezone=True))


# ─── Announcements ────────────────────────────────────────────────────────────

class Announcement(Base):
    __tablename__ = "announcements"

    id          = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id   = Column(UUID(as_uuid=True), nullable=False)
    content     = Column(Text, nullable=False)
    priority    = Column(String(20), default="normal")   # normal | high | urgent
    created_by  = Column(String(200))
    created_at  = Column(DateTime(timezone=True), server_default=func.now())
    expires_at  = Column(DateTime(timezone=True))


# ─── Earnings / Payouts ───────────────────────────────────────────────────────

class EarningsSummary(Base):
    """Daily earnings snapshot per worker (materialized by Celery nightly)."""
    __tablename__ = "earnings_summaries"

    id              = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    worker_id       = Column(UUID(as_uuid=True), ForeignKey("workers.id"))
    tenant_id       = Column(UUID(as_uuid=True), nullable=False)
    date            = Column(DateTime(timezone=True), nullable=False)    # day
    confirmed_usd   = Column(Numeric(10, 4), default=0)
    pending_usd     = Column(Numeric(10, 4), default=0)
    tasks_completed = Column(Integer, default=0)
    tasks_accepted  = Column(Integer, default=0)
    bonus_earned    = Column(Numeric(10, 4), default=0)
    payout_method   = Column(String(50), default="pending")

    __table_args__ = (
        Index("ix_earnings_worker_date", "worker_id", "date", unique=True),
    )


# ─── Webhook Log ─────────────────────────────────────────────────────────────

class WebhookLog(Base):
    __tablename__ = "webhook_logs"

    id          = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id   = Column(UUID(as_uuid=True), nullable=False)
    event       = Column(String(100))
    payload     = Column(JSON)
    status_code = Column(Integer)
    attempt     = Column(Integer, default=1)
    success     = Column(Boolean, default=False)
    sent_at     = Column(DateTime(timezone=True), server_default=func.now())
    duration_ms = Column(Integer)
