"""
003_add_max_trust_and_fixes

Revision ID: 003
Revises: 002
Create Date: 2025-01-01

Migrations for v3 fixes:
  - worker_scores.max_trust       (FIX #7: trust ceiling after fraud)
  - tasks index on submitted_at   (supports FIX #1 rolling window queries)
  - fraud_events index on reviewed (supports FIX #4 and #13 queries)
"""
from alembic import op
import sqlalchemy as sa


revision = "003"
down_revision = "002"
branch_labels = None
depends_on = None


def upgrade():
    # FIX #7: max_trust ceiling for fraud recovery cap
    op.add_column(
        "worker_scores",
        sa.Column("max_trust", sa.Float(), nullable=False, server_default="100.0"),
    )

    # FIX #1: Partial index to speed up rolling window queries on tasks
    op.create_index(
        "ix_tasks_worker_submitted_accepted",
        "tasks",
        ["worker_id", "submitted_at", "was_accepted"],
    )

    # FIX #4 + #13: Index to speed up unreviewed fraud event queries
    op.create_index(
        "ix_fraud_unreviewed",
        "fraud_events",
        ["worker_id", "tenant_id", "reviewed"],
        postgresql_where=sa.text("reviewed = FALSE"),
    )


def downgrade():
    op.drop_index("ix_fraud_unreviewed",              table_name="fraud_events")
    op.drop_index("ix_tasks_worker_submitted_accepted", table_name="tasks")
    op.drop_column("worker_scores", "max_trust")
