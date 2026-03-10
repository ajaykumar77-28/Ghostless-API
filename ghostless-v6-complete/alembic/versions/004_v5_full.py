"""
004_v5_full

Revision ID: 004
Revises: 003
Create Date: 2025-01-01

v5 migrations — all new tables and columns.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "004"
down_revision = "003"
branch_labels = None
depends_on = None


def upgrade():
    # ── worker_scores: new columns ─────────────────────────────────────
    op.add_column("worker_scores", sa.Column("velocity_score",  sa.Float(), nullable=True, server_default="1.0"))
    op.add_column("worker_scores", sa.Column("shadow_banned",   sa.Boolean(), nullable=False, server_default="false"))
    op.add_column("worker_scores", sa.Column("lifecycle_stage", sa.String(20), nullable=True, server_default="new"))

    # ── tasks: new columns ────────────────────────────────────────────
    op.add_column("tasks", sa.Column("difficulty_weight", sa.Float(), nullable=True, server_default="1.0"))
    op.add_column("tasks", sa.Column("currency",          sa.String(10), nullable=True, server_default="USD"))
    op.add_column("tasks", sa.Column("archived_at",       sa.DateTime(timezone=True), nullable=True))

    # ── validations: new columns ──────────────────────────────────────
    op.add_column("validations", sa.Column("anomaly_score",                sa.Float(), nullable=True, server_default="0.0"))
    op.add_column("validations", sa.Column("submission_idempotency_key",   sa.String(128), nullable=True))
    op.add_column("validations", sa.Column("difficulty_weight",            sa.Float(), nullable=True, server_default="1.0"))
    op.create_unique_constraint("uq_validations_submission_idem", "validations", ["submission_idempotency_key"])

    # ── fraud_events: new columns ──────────────────────────────────────
    op.add_column("fraud_events", sa.Column("reason_codes",      postgresql.JSON(), nullable=True))
    op.add_column("fraud_events", sa.Column("progressive_level", sa.Integer(), nullable=True, server_default="1"))
    op.add_column("fraud_events", sa.Column("reviewed_by",       sa.String(200), nullable=True))
    op.add_column("fraud_events", sa.Column("decay_logged_at",   sa.DateTime(timezone=True), nullable=True))

    # ── ledger_entries: currency + clawback ───────────────────────────
    op.add_column("ledger_entries", sa.Column("currency",        sa.String(10), nullable=False, server_default="USD"))
    op.add_column("ledger_entries", sa.Column("clawback_reason", sa.String(500), nullable=True))
    op.create_index("ix_ledger_tenant_currency", "ledger_entries", ["tenant_id", "currency"])

    # ── api_keys: role column ─────────────────────────────────────────
    op.add_column("api_keys", sa.Column("role", sa.String(20), nullable=True, server_default="worker"))

    # ── New tables ────────────────────────────────────────────────────

    op.create_table(
        "tenant_configs",
        sa.Column("tenant_id",                sa.UUID(as_uuid=True), sa.ForeignKey("tenants.id"), primary_key=True),
        sa.Column("max_tasks_per_hour",        sa.Integer()),
        sa.Column("velocity_trust_penalty_max",sa.Float()),
        sa.Column("min_peers_for_confidence_50", sa.Integer()),
        sa.Column("confidence_full_at_n",      sa.Integer()),
        sa.Column("post_fraud_max_trust",      sa.Float()),
        sa.Column("fraud_progressive_step1",   sa.Integer()),
        sa.Column("fraud_progressive_step2",   sa.Integer()),
        sa.Column("fraud_progressive_step3",   sa.Integer()),
        sa.Column("default_currency",          sa.String(10)),
        sa.Column("updated_at",                sa.DateTime(timezone=True), onupdate=sa.func.now()),
    )

    op.create_table(
        "tasks_archive",
        sa.Column("id",              sa.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id",       sa.UUID(as_uuid=True), nullable=False),
        sa.Column("worker_id",       sa.UUID(as_uuid=True)),
        sa.Column("task_type",       sa.String(100), nullable=False),
        sa.Column("project_id",      sa.String(200)),
        sa.Column("submitted_at",    sa.DateTime(timezone=True)),
        sa.Column("completion_time", sa.Float()),
        sa.Column("was_accepted",    sa.Boolean()),
        sa.Column("payout_amount",   sa.Numeric(10, 4)),
        sa.Column("idempotency_key", sa.String(128)),
        sa.Column("currency",        sa.String(10)),
        sa.Column("archived_at",     sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("metadata_",       sa.String(), nullable=True),
    )
    op.create_index("ix_tasks_archive_tenant_worker", "tasks_archive", ["tenant_id", "worker_id"])
    op.create_index("ix_tasks_archive_submitted",     "tasks_archive", ["tenant_id", "submitted_at"])

    op.create_table(
        "coordinated_attack_events",
        sa.Column("id",             sa.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id",      sa.UUID(as_uuid=True), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("payload_hash",   sa.String(64), nullable=False),
        sa.Column("worker_ids",     postgresql.JSON(), nullable=False),
        sa.Column("worker_count",   sa.Integer(), nullable=False),
        sa.Column("detection_type", sa.String(30), server_default="short_window"),
        sa.Column("lookback_hours", sa.Float()),
        sa.Column("reviewed",       sa.Boolean(), server_default="false"),
        sa.Column("created_at",     sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_coord_attack_tenant", "coordinated_attack_events", ["tenant_id", "created_at"])
    op.create_index("ix_coord_attack_hash",   "coordinated_attack_events", ["tenant_id", "payload_hash"])

    op.create_table(
        "scoring_decision_logs",
        sa.Column("id",                   sa.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id",            sa.UUID(as_uuid=True), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("worker_id",            sa.UUID(as_uuid=True), sa.ForeignKey("workers.id"), nullable=False),
        sa.Column("trust_score_before",   sa.Float()),
        sa.Column("trust_score_after",    sa.Float()),
        sa.Column("ewma_component",       sa.Float()),
        sa.Column("accuracy_component",   sa.Float()),
        sa.Column("streak_component",     sa.Float()),
        sa.Column("tenure_component",     sa.Float()),
        sa.Column("confidence_factor",    sa.Float()),
        sa.Column("fraud_multiplier",     sa.Float()),
        sa.Column("velocity_penalty",     sa.Float()),
        sa.Column("task_difficulty_used", sa.Float()),
        sa.Column("fraud_event_count",    sa.Integer()),
        sa.Column("is_fraud_suspect",     sa.Boolean(), server_default="false"),
        sa.Column("drift_detected",       sa.Boolean(), server_default="false"),
        sa.Column("drift_details",        postgresql.JSON(), server_default="{}"),
        sa.Column("calculated_at",        sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_scoring_log_tenant_worker", "scoring_decision_logs", ["tenant_id", "worker_id"])
    op.create_index("ix_scoring_log_calculated_at", "scoring_decision_logs", ["tenant_id", "calculated_at"])

    op.create_table(
        "fraud_decay_logs",
        sa.Column("id",               sa.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id",        sa.UUID(as_uuid=True), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("worker_id",        sa.UUID(as_uuid=True), sa.ForeignKey("workers.id"), nullable=False),
        sa.Column("fraud_event_id",   sa.UUID(as_uuid=True), sa.ForeignKey("fraud_events.id")),
        sa.Column("effective_weight", sa.Float()),
        sa.Column("age_days",         sa.Float()),
        sa.Column("logged_at",        sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_fraud_decay_tenant_worker", "fraud_decay_logs", ["tenant_id", "worker_id"])

    op.create_table(
        "admin_audit_logs",
        sa.Column("id",           sa.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id",    sa.UUID(as_uuid=True), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("admin_key_id", sa.UUID(as_uuid=True)),
        sa.Column("action",       sa.String(100), nullable=False),
        sa.Column("target_type",  sa.String(50)),
        sa.Column("target_id",    sa.String(100)),
        sa.Column("before_state", postgresql.JSON(), server_default="{}"),
        sa.Column("after_state",  postgresql.JSON(), server_default="{}"),
        sa.Column("notes",        sa.Text()),
        sa.Column("performed_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_admin_audit_tenant", "admin_audit_logs", ["tenant_id", "performed_at"])

    op.create_table(
        "appeal_records",
        sa.Column("id",              sa.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id",       sa.UUID(as_uuid=True), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("worker_id",       sa.UUID(as_uuid=True), sa.ForeignKey("workers.id"), nullable=False),
        sa.Column("fraud_event_id",  sa.UUID(as_uuid=True), sa.ForeignKey("fraud_events.id"), nullable=True),
        sa.Column("status",          sa.String(20), server_default="pending"),
        sa.Column("reason",          sa.Text(), nullable=False),
        sa.Column("evidence",        postgresql.JSON(), server_default="{}"),
        sa.Column("reviewer_notes",  sa.Text()),
        sa.Column("reviewed_by",     sa.String(200)),
        sa.Column("created_at",      sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("resolved_at",     sa.DateTime(timezone=True)),
    )
    op.create_index("ix_appeals_tenant_worker", "appeal_records", ["tenant_id", "worker_id"])
    op.create_index("ix_appeals_status",        "appeal_records", ["tenant_id", "status"])

    op.create_table(
        "baseline_versions",
        sa.Column("id",         sa.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id",  sa.UUID(as_uuid=True), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("task_type",  sa.String(100), nullable=False),
        sa.Column("baseline",   postgresql.JSON(), nullable=False),
        sa.Column("version",    sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("trigger",    sa.String(50), server_default="scheduled"),
        sa.UniqueConstraint("tenant_id", "task_type", "version",
                            name="uq_baseline_versions_tenant_type_version"),
    )
    op.create_index("ix_baseline_versions_tenant_type", "baseline_versions", ["tenant_id", "task_type"])


def downgrade():
    op.drop_table("baseline_versions")
    op.drop_table("appeal_records")
    op.drop_table("admin_audit_logs")
    op.drop_table("fraud_decay_logs")
    op.drop_table("scoring_decision_logs")
    op.drop_table("coordinated_attack_events")
    op.drop_table("tasks_archive")
    op.drop_table("tenant_configs")
    op.drop_column("api_keys",        "role")
    op.drop_column("ledger_entries",  "clawback_reason")
    op.drop_column("ledger_entries",  "currency")
    op.drop_column("fraud_events",    "decay_logged_at")
    op.drop_column("fraud_events",    "reviewed_by")
    op.drop_column("fraud_events",    "progressive_level")
    op.drop_column("fraud_events",    "reason_codes")
    op.drop_column("validations",     "difficulty_weight")
    op.drop_column("validations",     "submission_idempotency_key")
    op.drop_column("validations",     "anomaly_score")
    op.drop_column("tasks",           "archived_at")
    op.drop_column("tasks",           "currency")
    op.drop_column("tasks",           "difficulty_weight")
    op.drop_column("worker_scores",   "lifecycle_stage")
    op.drop_column("worker_scores",   "shadow_banned")
    op.drop_column("worker_scores",   "velocity_score")
