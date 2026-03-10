"""
Ghostless API — Database Models (v5)

New in v5:
  WorkerScore:
    - max_trust          (fraud recovery ceiling, from v4)
    - velocity_score     (rolling velocity health, 0–1)
    - shadow_banned      (FIX: shadow-ban mode)
    - lifecycle_stage    (new | learning | trusted | flagged | banned)

  Validation:
    - anomaly_score      (per-submission anomaly composite score)
    - submission_idempotency_key  (replay protection)
    - difficulty_weight  (task difficulty for trust weighting)

  FraudEvent:
    - reason_codes       (JSON list: velocity | ip | coordination | zscore | entropy)
    - progressive_level  (1=warning, 2=lock, 3=ban)
    - decay_logged_at    (when decay was last applied)

  New tables:
    CoordinatedAttackEvent  — persisted group fraud signals (long-horizon)
    ScoringDecisionLog      — why trust changed per recalc cycle
    FraudDecayLog           — audit trail for fraud score decay
    AdminAuditLog           — every admin action recorded
    AppealRecord            — worker appeal workflow
    BaselineVersion         — versioned snapshots of task baselines
    TenantConfig            — per-tenant scoring / fraud overrides (hot reload)
    TaskArchive             — moved from tasks after ARCHIVAL_CUTOFF_DAYS
"""
from sqlalchemy import (
    Column, String, Float, Integer, Boolean, DateTime,
    ForeignKey, Text, JSON, Numeric, Enum as SAEnum, Index,
    UniqueConstraint, BigInteger
)
from sqlalchemy.dialects.postgresql import UUID, JSONB
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


class TenantTierEnum(str, enum.Enum):
    starter    = "starter"
    growth     = "growth"
    enterprise = "enterprise"


class LedgerEntryType(str, enum.Enum):
    task_payout = "task_payout"
    bonus       = "bonus"
    adjustment  = "adjustment"
    reversal    = "reversal"
    clawback    = "clawback"
    payout_sent = "payout_sent"


class WorkerLifecycleStage(str, enum.Enum):
    new      = "new"       # < 10 tasks
    learning = "learning"  # 10–50 tasks, building baseline
    trusted  = "trusted"   # 50+ tasks, high confidence
    flagged  = "flagged"   # active fraud signals
    banned   = "banned"    # permanently removed


class FraudProgressiveLevel(int, enum.Enum):
    warning = 1
    lock    = 2   # shadow-ban
    ban     = 3


class AppealStatus(str, enum.Enum):
    pending   = "pending"
    reviewing = "reviewing"
    approved  = "approved"
    denied    = "denied"


# ─── Tenants ──────────────────────────────────────────────────────────────────

class Tenant(Base):
    __tablename__ = "tenants"

    id             = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name           = Column(String(200), nullable=False)
    slug           = Column(String(100), unique=True, nullable=False)
    tier           = Column(SAEnum(TenantTierEnum), default=TenantTierEnum.starter)
    api_key_hash   = Column(String(64), unique=True)
    jwt_secret     = Column(String(64))
    webhook_url    = Column(String(500))
    webhook_secret = Column(String(64))
    webhook_events = Column(JSON, default=list)
    brand_name     = Column(String(200))
    brand_color    = Column(String(7))
    is_active      = Column(Boolean, default=True)
    created_at     = Column(DateTime(timezone=True), server_default=func.now())
    metadata_      = Column("metadata", JSON, default=dict)

    workers        = relationship("Worker",       back_populates="tenant")
    api_keys       = relationship("APIKey",       back_populates="tenant")
    refresh_tokens = relationship("RefreshToken", back_populates="tenant")
    config         = relationship("TenantConfig", back_populates="tenant", uselist=False)


class TenantConfig(Base):
    """
    Per-tenant overrides for scoring and fraud thresholds.
    Hot-reloaded by scoring tasks — no restart needed.
    """
    __tablename__ = "tenant_configs"

    tenant_id = Column(UUID(as_uuid=True), ForeignKey("tenants.id"), primary_key=True)
    # Fraud thresholds
    max_tasks_per_minute      = Column(Integer)
    max_tasks_per_hour        = Column(Integer)
    max_tasks_per_day         = Column(Integer)
    max_workers_per_ip        = Column(Integer)
    fraud_progressive_step1   = Column(Integer)
    fraud_progressive_step2   = Column(Integer)
    fraud_progressive_step3   = Column(Integer)
    post_fraud_max_trust      = Column(Float)
    # Scoring parameters
    ewma_alpha_new            = Column(Float)
    ewma_alpha_mature         = Column(Float)
    confidence_full_at_n      = Column(Integer)
    min_peers_for_confidence_50 = Column(Integer)
    velocity_trust_penalty_max  = Column(Float)
    # Earnings
    default_currency          = Column(String(10))
    # Misc
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    tenant = relationship("Tenant", back_populates="config")


class APIKey(Base):
    __tablename__ = "api_keys"

    id             = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id      = Column(UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False)
    name           = Column(String(100))
    key_hash       = Column(String(128), unique=True, nullable=False)
    hash_algorithm = Column(String(20), default="argon2")
    key_prefix     = Column(String(20))
    role           = Column(String(20), default="worker")   # worker | admin
    is_active      = Column(Boolean, default=True)
    revoked_at     = Column(DateTime(timezone=True))
    revoked_reason = Column(String(200))
    last_used_at   = Column(DateTime(timezone=True))
    created_at     = Column(DateTime(timezone=True), server_default=func.now())
    expires_at     = Column(DateTime(timezone=True))
    rate_limit_rpm = Column(Integer)   # per-key override

    tenant = relationship("Tenant", back_populates="api_keys")

    __table_args__ = (
        Index("ix_api_keys_tenant_active", "tenant_id", "is_active"),
    )


class RefreshToken(Base):
    __tablename__ = "refresh_tokens"

    id           = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id    = Column(UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False)
    worker_id    = Column(UUID(as_uuid=True), ForeignKey("workers.id"), nullable=True)
    token_hash   = Column(String(64), unique=True, nullable=False)
    family_id    = Column(UUID(as_uuid=True), default=uuid.uuid4)
    is_revoked   = Column(Boolean, default=False)
    revoked_at   = Column(DateTime(timezone=True))
    created_at   = Column(DateTime(timezone=True), server_default=func.now())
    expires_at   = Column(DateTime(timezone=True), nullable=False)
    last_used_at = Column(DateTime(timezone=True))

    tenant = relationship("Tenant", back_populates="refresh_tokens")

    __table_args__ = (
        Index("ix_refresh_tokens_tenant_worker", "tenant_id", "worker_id"),
        Index("ix_refresh_tokens_family",        "family_id"),
    )


# ─── Workers ──────────────────────────────────────────────────────────────────

class Worker(Base):
    __tablename__ = "workers"

    id          = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id   = Column(UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False)
    external_id = Column(String(200), nullable=False)
    tier        = Column(SAEnum(TierEnum), default=TierEnum.bronze)
    status      = Column(SAEnum(StatusEnum), default=StatusEnum.active)
    created_at  = Column(DateTime(timezone=True), server_default=func.now())
    metadata_   = Column("metadata", JSON, default=dict)

    tenant         = relationship("Tenant",      back_populates="workers")
    tasks          = relationship("Task",        back_populates="worker")
    score          = relationship("WorkerScore", back_populates="worker", uselist=False)
    promotions     = relationship("Promotion",   back_populates="worker")
    messages       = relationship("Message",     back_populates="worker")
    ledger_entries = relationship("LedgerEntry", back_populates="worker")
    fraud_events   = relationship("FraudEvent",  back_populates="worker")
    appeals        = relationship("AppealRecord", back_populates="worker")

    __table_args__ = (
        UniqueConstraint("tenant_id", "external_id", name="uq_workers_tenant_external"),
        Index("ix_workers_tenant_status",  "tenant_id", "status"),
    )


class WorkerScore(Base):
    __tablename__ = "worker_scores"

    worker_id    = Column(UUID(as_uuid=True), ForeignKey("workers.id"), primary_key=True)
    tenant_id    = Column(UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False)
    trust_score  = Column(Float, default=50.0)
    max_trust    = Column(Float, default=100.0, nullable=False)   # fraud recovery ceiling

    # Rolling accuracy windows (SQL-computed, never counters)
    accuracy_7d  = Column(Float)
    accuracy_30d = Column(Float)
    accuracy_all = Column(Float)

    # EWMA quality score
    ewma_quality = Column(Float, default=0.5)
    ewma_alpha   = Column(Float, default=0.3)

    # Z-score anomaly detection
    zscore_latest        = Column(Float)
    zscore_flagged_count = Column(Integer, default=0)
    task_baselines       = Column(JSON, default=dict)

    # Confidence and velocity
    confidence_weight  = Column(Float, default=0.5)
    velocity_score     = Column(Float, default=1.0)  # 1.0 = healthy, < 1.0 = velocity penalty
    lifecycle_stage    = Column(SAEnum(WorkerLifecycleStage), default=WorkerLifecycleStage.new)

    # Shadow-ban: tasks accepted, payout zeroed out
    shadow_banned      = Column(Boolean, default=False, nullable=False)

    avg_quality_score  = Column(Float)
    total_tasks        = Column(Integer, default=0)
    accepted_tasks     = Column(Integer, default=0)
    speed_flag_count   = Column(Integer, default=0)
    fraud_flag_count   = Column(Integer, default=0)
    streak_days        = Column(Integer, default=0)
    last_active_date   = Column(DateTime(timezone=True))
    last_calculated_at = Column(DateTime(timezone=True))

    worker = relationship("Worker", back_populates="score")

    __table_args__ = (
        Index("ix_worker_scores_tenant_trust",  "tenant_id", "trust_score"),
        Index("ix_worker_scores_tenant_worker", "tenant_id", "worker_id"),
    )


class Promotion(Base):
    __tablename__ = "promotions"

    id             = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id      = Column(UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False)
    worker_id      = Column(UUID(as_uuid=True), ForeignKey("workers.id"), nullable=False)
    from_tier      = Column(SAEnum(TierEnum))
    to_tier        = Column(SAEnum(TierEnum))
    reason         = Column(String(500))
    triggered_at   = Column(DateTime(timezone=True), server_default=func.now())
    trust_score_at = Column(Float)

    worker = relationship("Worker", back_populates="promotions")

    __table_args__ = (Index("ix_promotions_tenant_worker", "tenant_id", "worker_id"),)


# ─── Tasks ────────────────────────────────────────────────────────────────────

class Task(Base):
    __tablename__ = "tasks"

    id                   = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id            = Column(UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False)
    worker_id            = Column(UUID(as_uuid=True), ForeignKey("workers.id"))
    task_type            = Column(String(100), nullable=False)
    project_id           = Column(String(200))
    submitted_at         = Column(DateTime(timezone=True), server_default=func.now())
    completion_time      = Column(Float)
    was_accepted         = Column(Boolean)
    payout_amount        = Column(Numeric(10, 4), default=0)
    idempotency_key      = Column(String(128), unique=True)
    difficulty_weight    = Column(Float, default=1.0)   # task difficulty multiplier
    currency             = Column(String(10), default="USD")
    metadata_            = Column("metadata", JSON, default=dict)
    archived_at          = Column(DateTime(timezone=True))   # set when moved to archive

    worker     = relationship("Worker",     back_populates="tasks")
    validation = relationship("Validation", back_populates="task", uselist=False)

    __table_args__ = (
        Index("ix_tasks_worker_submitted",           "worker_id", "submitted_at"),
        Index("ix_tasks_tenant_submitted",           "tenant_id", "submitted_at"),
        Index("ix_tasks_tenant_worker",              "tenant_id", "worker_id"),
        Index("ix_tasks_tenant_type",                "tenant_id", "task_type"),
        # Composite indexes for rolling window queries (FIX #11)
        Index("ix_tasks_tenant_worker_submitted",    "tenant_id", "worker_id", "submitted_at"),
        Index("ix_tasks_tenant_type_submitted",      "tenant_id", "task_type", "submitted_at"),
    )


class TaskArchive(Base):
    """
    Tasks older than ARCHIVAL_CUTOFF_DAYS are moved here.
    Same schema as Task but stored in a separate (possibly partitioned) table.
    """
    __tablename__ = "tasks_archive"

    id              = Column(UUID(as_uuid=True), primary_key=True)
    tenant_id       = Column(UUID(as_uuid=True), nullable=False)
    worker_id       = Column(UUID(as_uuid=True))
    task_type       = Column(String(100), nullable=False)
    project_id      = Column(String(200))
    submitted_at    = Column(DateTime(timezone=True))
    completion_time = Column(Float)
    was_accepted    = Column(Boolean)
    payout_amount   = Column(Numeric(10, 4))
    idempotency_key = Column(String(128))
    currency        = Column(String(10))
    archived_at     = Column(DateTime(timezone=True), server_default=func.now())
    metadata_       = Column("metadata", JSON, default=dict)

    __table_args__ = (
        Index("ix_tasks_archive_tenant_worker", "tenant_id", "worker_id"),
        Index("ix_tasks_archive_submitted",     "tenant_id", "submitted_at"),
    )


class Validation(Base):
    __tablename__ = "validations"

    id                         = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    task_id                    = Column(UUID(as_uuid=True), ForeignKey("tasks.id"))
    worker_id                  = Column(UUID(as_uuid=True), ForeignKey("workers.id"))
    tenant_id                  = Column(UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False)
    quality_score              = Column(Float, nullable=False)
    anomaly_score              = Column(Float, default=0.0)   # composite anomaly (0–1)
    warning_count              = Column(Integer, default=0)
    error_count                = Column(Integer, default=0)
    was_allowed                = Column(Boolean, nullable=False)
    validated_at               = Column(DateTime(timezone=True), server_default=func.now())
    rule_flags                 = Column(JSON, default=list)
    processed_ms               = Column(Integer)
    submission_idempotency_key = Column(String(128), unique=True)   # replay protection
    difficulty_weight          = Column(Float, default=1.0)

    task = relationship("Task", back_populates="validation")

    __table_args__ = (
        Index("ix_validations_worker_date",   "worker_id", "validated_at"),
        Index("ix_validations_tenant_date",   "tenant_id", "validated_at"),
        Index("ix_validations_tenant_worker", "tenant_id", "worker_id"),
        Index("ix_validations_tenant_task",   "tenant_id", "task_id"),
    )


# ─── Ledger (append-only earnings ledger) ─────────────────────────────────────

class LedgerEntry(Base):
    """
    Immutable double-entry ledger. NEVER UPDATE — only INSERT.
    Reversals, clawbacks, and adjustments are new rows with negative amounts.
    Balance = SUM(amount_usd) WHERE worker_id=X AND tenant_id=Y AND currency=Z.
    """
    __tablename__ = "ledger_entries"

    id              = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id       = Column(UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False)
    worker_id       = Column(UUID(as_uuid=True), ForeignKey("workers.id"), nullable=False)
    task_id         = Column(UUID(as_uuid=True), ForeignKey("tasks.id"), nullable=True)
    entry_type      = Column(SAEnum(LedgerEntryType), nullable=False)
    amount_usd      = Column(Numeric(12, 6), nullable=False)
    currency        = Column(String(10), default="USD", nullable=False)
    running_balance = Column(Numeric(12, 6))
    description     = Column(String(500))
    idempotency_key = Column(String(128), unique=True)
    is_confirmed    = Column(Boolean, default=False)
    reference_id    = Column(UUID(as_uuid=True))   # reversal/clawback → original id
    clawback_reason = Column(String(500))          # populated on clawback entries
    created_at      = Column(DateTime(timezone=True), server_default=func.now())
    metadata_       = Column("metadata", JSON, default=dict)

    worker = relationship("Worker", back_populates="ledger_entries")

    __table_args__ = (
        Index("ix_ledger_tenant_worker",   "tenant_id", "worker_id"),
        Index("ix_ledger_tenant_task",     "tenant_id", "task_id"),
        Index("ix_ledger_idempotency",     "idempotency_key"),
        Index("ix_ledger_tenant_currency", "tenant_id", "currency"),
    )


# ─── Fraud Events ─────────────────────────────────────────────────────────────

class FraudEvent(Base):
    __tablename__ = "fraud_events"

    id                 = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id          = Column(UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False)
    worker_id          = Column(UUID(as_uuid=True), ForeignKey("workers.id"), nullable=False)
    task_id            = Column(UUID(as_uuid=True), ForeignKey("tasks.id"), nullable=True)
    event_type         = Column(String(50), nullable=False)
    severity           = Column(String(20), default="warning")
    reason_codes       = Column(JSON, default=list)   # ["velocity", "ip", "zscore", ...]
    details            = Column(JSON, default=dict)
    ip_address         = Column(String(45))
    user_agent         = Column(String(500))
    auto_action        = Column(String(50))            # flag | suspend | shadow_ban | ban
    progressive_level  = Column(Integer, default=1)    # 1=warning, 2=lock, 3=ban
    reviewed           = Column(Boolean, default=False)
    reviewed_by        = Column(String(200))
    decay_logged_at    = Column(DateTime(timezone=True))   # last decay audit entry
    created_at         = Column(DateTime(timezone=True), server_default=func.now())

    worker = relationship("Worker", back_populates="fraud_events")

    __table_args__ = (
        Index("ix_fraud_tenant_worker",    "tenant_id", "worker_id"),
        Index("ix_fraud_tenant_type",      "tenant_id", "event_type"),
        Index("ix_fraud_tenant_created",   "tenant_id", "created_at"),
        Index("ix_fraud_unreviewed",       "worker_id", "tenant_id", "reviewed",
              postgresql_where="reviewed = FALSE"),
    )


class CoordinatedAttackEvent(Base):
    """
    Persisted record of a detected coordinated attack group.
    Populated by both short-window (validate.py) and long-horizon (tasks/fraud_clustering.py).
    """
    __tablename__ = "coordinated_attack_events"

    id              = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id       = Column(UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False)
    payload_hash    = Column(String(64), nullable=False)   # structural hash shared by group
    worker_ids      = Column(JSON, nullable=False)         # list of involved worker UUIDs
    worker_count    = Column(Integer, nullable=False)
    detection_type  = Column(String(30), default="short_window")  # short_window | long_horizon
    lookback_hours  = Column(Float)
    reviewed        = Column(Boolean, default=False)
    created_at      = Column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        Index("ix_coord_attack_tenant",         "tenant_id", "created_at"),
        Index("ix_coord_attack_hash",           "tenant_id", "payload_hash"),
    )


# ─── Scoring Decision Log ─────────────────────────────────────────────────────

class ScoringDecisionLog(Base):
    """
    Append-only log of every trust score change and the components that drove it.
    Enables fraud explainability and scoring audit.
    """
    __tablename__ = "scoring_decision_logs"

    id                    = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id             = Column(UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False)
    worker_id             = Column(UUID(as_uuid=True), ForeignKey("workers.id"), nullable=False)
    trust_score_before    = Column(Float)
    trust_score_after     = Column(Float)
    ewma_component        = Column(Float)
    accuracy_component    = Column(Float)
    streak_component      = Column(Float)
    tenure_component      = Column(Float)
    confidence_factor     = Column(Float)
    fraud_multiplier      = Column(Float)
    velocity_penalty      = Column(Float)
    task_difficulty_used  = Column(Float)
    fraud_event_count     = Column(Integer)
    is_fraud_suspect      = Column(Boolean, default=False)
    drift_detected        = Column(Boolean, default=False)
    drift_details         = Column(JSON, default=dict)
    calculated_at         = Column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        Index("ix_scoring_log_tenant_worker", "tenant_id", "worker_id"),
        Index("ix_scoring_log_calculated_at", "tenant_id", "calculated_at"),
    )


# ─── Fraud Decay Log ──────────────────────────────────────────────────────────

class FraudDecayLog(Base):
    """
    Audit trail for every fraud score decay event.
    Required for accountability and appeals.
    """
    __tablename__ = "fraud_decay_logs"

    id                = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id         = Column(UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False)
    worker_id         = Column(UUID(as_uuid=True), ForeignKey("workers.id"), nullable=False)
    fraud_event_id    = Column(UUID(as_uuid=True), ForeignKey("fraud_events.id"))
    effective_weight  = Column(Float)   # decayed weight (e.g. 0.5 at half-life)
    age_days          = Column(Float)
    logged_at         = Column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        Index("ix_fraud_decay_tenant_worker", "tenant_id", "worker_id"),
    )


# ─── Admin Audit Log ──────────────────────────────────────────────────────────

class AdminAuditLog(Base):
    """Every admin action recorded for accountability."""
    __tablename__ = "admin_audit_logs"

    id          = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id   = Column(UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False)
    admin_key_id= Column(UUID(as_uuid=True))       # which API key performed the action
    action      = Column(String(100), nullable=False)  # review_worker | bulk_suspend | etc.
    target_type = Column(String(50))               # worker | tenant | fraud_event
    target_id   = Column(String(100))
    before_state= Column(JSON, default=dict)
    after_state = Column(JSON, default=dict)
    notes       = Column(Text)
    performed_at= Column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        Index("ix_admin_audit_tenant", "tenant_id", "performed_at"),
    )


# ─── Appeal Record ────────────────────────────────────────────────────────────

class AppealRecord(Base):
    """Worker appeal workflow for fraud flags and suspensions."""
    __tablename__ = "appeal_records"

    id              = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id       = Column(UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False)
    worker_id       = Column(UUID(as_uuid=True), ForeignKey("workers.id"), nullable=False)
    fraud_event_id  = Column(UUID(as_uuid=True), ForeignKey("fraud_events.id"), nullable=True)
    status          = Column(SAEnum(AppealStatus), default=AppealStatus.pending)
    reason          = Column(Text, nullable=False)
    evidence        = Column(JSON, default=dict)    # worker-supplied evidence
    reviewer_notes  = Column(Text)
    reviewed_by     = Column(String(200))
    created_at      = Column(DateTime(timezone=True), server_default=func.now())
    resolved_at     = Column(DateTime(timezone=True))

    worker = relationship("Worker", back_populates="appeals")

    __table_args__ = (
        Index("ix_appeals_tenant_worker", "tenant_id", "worker_id"),
        Index("ix_appeals_status",        "tenant_id", "status"),
    )


# ─── Baseline Version ─────────────────────────────────────────────────────────

class BaselineVersion(Base):
    """
    Versioned snapshot of per-task-type baselines.
    Written when baseline drift is detected or on a schedule.
    Allows rollback if a poisoning attack is identified.
    """
    __tablename__ = "baseline_versions"

    id          = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id   = Column(UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False)
    task_type   = Column(String(100), nullable=False)
    baseline    = Column(JSON, nullable=False)   # {"mean": x, "m2": y, "n": z}
    version     = Column(Integer, nullable=False)
    created_at  = Column(DateTime(timezone=True), server_default=func.now())
    trigger     = Column(String(50), default="scheduled")  # scheduled | drift_detected | manual

    __table_args__ = (
        Index("ix_baseline_versions_tenant_type", "tenant_id", "task_type"),
        UniqueConstraint("tenant_id", "task_type", "version",
                         name="uq_baseline_versions_tenant_type_version"),
    )


# ─── Hub / Community ──────────────────────────────────────────────────────────

class Message(Base):
    __tablename__ = "messages"

    id           = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id    = Column(UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False)
    worker_id    = Column(UUID(as_uuid=True), ForeignKey("workers.id"))
    project_id   = Column(String(200))
    room         = Column(String(200))
    content      = Column(Text, nullable=False)
    msg_type     = Column(String(50), default="chat")
    reply_to_id  = Column(UUID(as_uuid=True), ForeignKey("messages.id"))
    is_moderated = Column(Boolean, default=False)
    created_at   = Column(DateTime(timezone=True), server_default=func.now())

    worker = relationship("Worker", back_populates="messages")

    __table_args__ = (
        Index("ix_messages_room_created",  "room", "created_at"),
        Index("ix_messages_tenant_worker", "tenant_id", "worker_id"),
    )


class BugReport(Base):
    __tablename__ = "bug_reports"

    id             = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id      = Column(UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False)
    worker_id      = Column(UUID(as_uuid=True), ForeignKey("workers.id"))
    project_id     = Column(String(200))
    title          = Column(String(500), nullable=False)
    description    = Column(Text)
    severity       = Column(String(20), default="medium")
    status         = Column(String(20), default="open")
    ticket_id      = Column(String(50), unique=True)
    screenshot_url = Column(String(1000))
    created_at     = Column(DateTime(timezone=True), server_default=func.now())
    resolved_at    = Column(DateTime(timezone=True))

    __table_args__ = (Index("ix_bug_reports_tenant", "tenant_id"),)


class Announcement(Base):
    __tablename__ = "announcements"

    id         = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id  = Column(UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False)
    content    = Column(Text, nullable=False)
    priority   = Column(String(20), default="normal")
    created_by = Column(String(200))
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    expires_at = Column(DateTime(timezone=True))

    __table_args__ = (Index("ix_announcements_tenant", "tenant_id"),)


class EarningsSummary(Base):
    __tablename__ = "earnings_summaries"

    id              = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    worker_id       = Column(UUID(as_uuid=True), ForeignKey("workers.id"))
    tenant_id       = Column(UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False)
    date            = Column(DateTime(timezone=True), nullable=False)
    currency        = Column(String(10), default="USD")
    confirmed_usd   = Column(Numeric(10, 4), default=0)
    pending_usd     = Column(Numeric(10, 4), default=0)
    tasks_completed = Column(Integer, default=0)
    tasks_accepted  = Column(Integer, default=0)
    bonus_earned    = Column(Numeric(10, 4), default=0)
    payout_method   = Column(String(50), default="pending")

    __table_args__ = (
        UniqueConstraint("worker_id", "date", "currency",
                         name="uq_earnings_worker_date_currency"),
        Index("ix_earnings_tenant_worker", "tenant_id", "worker_id"),
    )


class WebhookLog(Base):
    __tablename__ = "webhook_logs"

    id              = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id       = Column(UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False)
    event           = Column(String(100))
    payload         = Column(JSON)
    status_code     = Column(Integer)
    attempt         = Column(Integer, default=1)
    success         = Column(Boolean, default=False)
    sent_at         = Column(DateTime(timezone=True), server_default=func.now())
    duration_ms     = Column(Integer)
    idempotency_key = Column(String(128))
    hmac_signature  = Column(String(128))   # HMAC-SHA256 of payload

    __table_args__ = (Index("ix_webhook_logs_tenant", "tenant_id", "sent_at"),)


class WebhookDeadLetter(Base):
    __tablename__ = "webhook_dead_letters"

    id              = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id       = Column(UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False)
    event           = Column(String(100), nullable=False)
    payload         = Column(JSON, nullable=False)
    total_attempts  = Column(Integer, nullable=False)
    last_error      = Column(Text)
    idempotency_key = Column(String(128))
    created_at      = Column(DateTime(timezone=True), server_default=func.now())
    replayed_at     = Column(DateTime(timezone=True))
    is_replayed     = Column(Boolean, default=False)

    __table_args__ = (Index("ix_dead_letters_tenant", "tenant_id", "created_at"),)
