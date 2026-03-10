"""
Ghostless API — Alembic Migration 005: v6 Schema Improvements

Adds:
  1. algorithm_version to scoring_decision_logs (score versioning)
  2. posterior_alpha, posterior_beta, score_volatility to worker_scores (Bayesian state)
  3. deleted_at to workers, tasks (soft deletes)
  4. behavior_events table (event sourcing archive — drains from Redis Stream)
  5. score_snapshots table (regression testing baseline)
  6. feature_flags table (per-tenant feature toggles)
  7. Additional indexes for soft-delete queries
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID, JSONB

revision = "005_v6"
down_revision = "004_v5_full"
branch_labels = None
depends_on = None


def upgrade() -> None:

    # ── 1. Score versioning on scoring_decision_logs ──────────────────────
    op.add_column(
        "scoring_decision_logs",
        sa.Column("algorithm_version", sa.String(20), nullable=True),
    )
    op.execute(
        "UPDATE scoring_decision_logs SET algorithm_version = 'v5.0.0' "
        "WHERE algorithm_version IS NULL"
    )

    # ── 2. Bayesian posterior state + volatility on worker_scores ─────────
    op.add_column("worker_scores", sa.Column("posterior_alpha",  sa.Float, nullable=True))
    op.add_column("worker_scores", sa.Column("posterior_beta",   sa.Float, nullable=True))
    op.add_column("worker_scores", sa.Column("score_volatility", sa.Float, nullable=True))
    op.add_column("worker_scores", sa.Column("trust_ci_lower",   sa.Float, nullable=True))
    op.add_column("worker_scores", sa.Column("trust_ci_upper",   sa.Float, nullable=True))

    # Seed existing workers with weakly informative prior
    op.execute("UPDATE worker_scores SET posterior_alpha = 2.0, posterior_beta = 2.0 "
               "WHERE posterior_alpha IS NULL")

    # ── 3. Soft deletes on workers ────────────────────────────────────────
    op.add_column("workers", sa.Column(
        "deleted_at", sa.DateTime(timezone=True), nullable=True
    ))
    op.create_index("ix_workers_deleted_at", "workers", ["deleted_at"],
                    postgresql_where=sa.text("deleted_at IS NULL"))

    # ── 3b. Soft deletes on tasks ─────────────────────────────────────────
    op.add_column("tasks", sa.Column(
        "deleted_at", sa.DateTime(timezone=True), nullable=True
    ))
    op.create_index("ix_tasks_not_deleted", "tasks", ["tenant_id", "worker_id", "submitted_at"],
                    postgresql_where=sa.text("deleted_at IS NULL"))

    # ── 4. Behavior events table (event sourcing archive) ─────────────────
    op.create_table(
        "behavior_events",
        sa.Column("id",                UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("event_id",          sa.String(64), nullable=False, unique=True),
        sa.Column("event_type",        sa.String(80), nullable=False),
        sa.Column("tenant_id",         UUID(as_uuid=True), nullable=False),
        sa.Column("worker_id",         UUID(as_uuid=True), nullable=True),
        sa.Column("payload",           JSONB, nullable=False, server_default="{}"),
        sa.Column("algorithm_version", sa.String(20)),
        sa.Column("ts",                sa.Float, nullable=False),
        sa.Column("created_at",        sa.DateTime(timezone=True),
                  server_default=sa.text("now()")),
    )
    op.create_index("ix_behavior_events_tenant_type",   "behavior_events",
                    ["tenant_id", "event_type"])
    op.create_index("ix_behavior_events_tenant_worker", "behavior_events",
                    ["tenant_id", "worker_id"])
    op.create_index("ix_behavior_events_ts",            "behavior_events",
                    ["tenant_id", "ts"])

    # ── 5. Score snapshots (for regression testing baselines) ─────────────
    op.create_table(
        "score_snapshots",
        sa.Column("id",                UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id",         UUID(as_uuid=True), nullable=False),
        sa.Column("worker_id",         UUID(as_uuid=True), nullable=False),
        sa.Column("trust_score",       sa.Float, nullable=False),
        sa.Column("posterior_alpha",   sa.Float),
        sa.Column("posterior_beta",    sa.Float),
        sa.Column("algorithm_version", sa.String(20)),
        sa.Column("snapshot_label",    sa.String(200)),   # e.g. "pre-v6-migration"
        sa.Column("created_at",        sa.DateTime(timezone=True),
                  server_default=sa.text("now()")),
    )
    op.create_index("ix_score_snapshots_tenant_worker", "score_snapshots",
                    ["tenant_id", "worker_id"])
    op.create_index("ix_score_snapshots_label", "score_snapshots",
                    ["tenant_id", "snapshot_label"])

    # ── 6. Feature flags (per-tenant) ─────────────────────────────────────
    op.create_table(
        "feature_flags",
        sa.Column("id",          UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id",   UUID(as_uuid=True), nullable=True),   # NULL = global
        sa.Column("flag_name",   sa.String(100), nullable=False),
        sa.Column("enabled",     sa.Boolean, default=False, nullable=False),
        sa.Column("rollout_pct", sa.Integer, default=100),    # 0–100 % of workers
        sa.Column("metadata_",   JSONB, default={}, name="metadata"),
        sa.Column("created_at",  sa.DateTime(timezone=True),
                  server_default=sa.text("now()")),
        sa.Column("updated_at",  sa.DateTime(timezone=True),
                  onupdate=sa.text("now()")),
        sa.UniqueConstraint("tenant_id", "flag_name", name="uq_feature_flags_tenant_flag"),
    )
    op.create_index("ix_feature_flags_tenant", "feature_flags", ["tenant_id"])

    # ── 7. Snapshot existing scores before algorithm change ───────────────
    op.execute("""
        INSERT INTO score_snapshots (
            id, tenant_id, worker_id, trust_score,
            posterior_alpha, posterior_beta,
            algorithm_version, snapshot_label, created_at
        )
        SELECT
            gen_random_uuid(),
            ws.tenant_id,
            ws.worker_id,
            ws.trust_score,
            2.0,   -- prior alpha (seed)
            2.0,   -- prior beta  (seed)
            'v5.0.0',
            'pre-v6-migration',
            now()
        FROM worker_scores ws
    """)

    # ── 8. Cooldown column on FraudEvents ─────────────────────────────────
    op.add_column("fraud_events", sa.Column(
        "cooldown_until", sa.DateTime(timezone=True), nullable=True,
        comment="Worker cannot gain trust above current level until this timestamp"
    ))


def downgrade() -> None:
    op.drop_column("fraud_events", "cooldown_until")
    op.drop_table("feature_flags")
    op.drop_table("score_snapshots")
    op.drop_table("behavior_events")
    op.drop_index("ix_tasks_not_deleted", "tasks")
    op.drop_column("tasks", "deleted_at")
    op.drop_index("ix_workers_deleted_at", "workers")
    op.drop_column("workers", "deleted_at")
    op.drop_column("worker_scores", "trust_ci_upper")
    op.drop_column("worker_scores", "trust_ci_lower")
    op.drop_column("worker_scores", "score_volatility")
    op.drop_column("worker_scores", "posterior_beta")
    op.drop_column("worker_scores", "posterior_alpha")
    op.drop_column("scoring_decision_logs", "algorithm_version")
