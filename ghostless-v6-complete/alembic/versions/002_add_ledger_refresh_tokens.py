"""Add ledger_entries, refresh_tokens, webhook_dead_letters, fraud_events

Revision ID: 002
Create Date: 2025-01-15
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = '002'
down_revision = '001'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ledger_entries
    op.create_table(
        'ledger_entries',
        sa.Column('id',              postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('tenant_id',       postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('worker_id',       postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('task_id',         postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column('entry_type',      sa.String(30), nullable=False),
        sa.Column('amount_usd',      sa.Numeric(12, 6), nullable=False),
        sa.Column('running_balance', sa.Numeric(12, 6)),
        sa.Column('description',     sa.String(500)),
        sa.Column('idempotency_key', sa.String(128), unique=True),
        sa.Column('is_confirmed',    sa.Boolean, default=False),
        sa.Column('reference_id',    postgresql.UUID(as_uuid=True)),
        sa.Column('created_at',      sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column('metadata',        sa.JSON, default=dict),
        sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id']),
        sa.ForeignKeyConstraint(['worker_id'], ['workers.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_ledger_tenant_worker', 'ledger_entries', ['tenant_id', 'worker_id'])
    op.create_index('ix_ledger_idempotency',   'ledger_entries', ['idempotency_key'])

    # refresh_tokens
    op.create_table(
        'refresh_tokens',
        sa.Column('id',           postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('tenant_id',    postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('worker_id',    postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column('token_hash',   sa.String(64), unique=True, nullable=False),
        sa.Column('family_id',    postgresql.UUID(as_uuid=True)),
        sa.Column('is_revoked',   sa.Boolean, default=False),
        sa.Column('revoked_at',   sa.DateTime(timezone=True)),
        sa.Column('created_at',   sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column('expires_at',   sa.DateTime(timezone=True), nullable=False),
        sa.Column('last_used_at', sa.DateTime(timezone=True)),
        sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id']),
        sa.PrimaryKeyConstraint('id'),
    )

    # fraud_events
    op.create_table(
        'fraud_events',
        sa.Column('id',          postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('tenant_id',   postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('worker_id',   postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('task_id',     postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column('event_type',  sa.String(50), nullable=False),
        sa.Column('severity',    sa.String(20), default='warning'),
        sa.Column('details',     sa.JSON, default=dict),
        sa.Column('ip_address',  sa.String(45)),
        sa.Column('user_agent',  sa.String(500)),
        sa.Column('auto_action', sa.String(50)),
        sa.Column('reviewed',    sa.Boolean, default=False),
        sa.Column('created_at',  sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id']),
        sa.ForeignKeyConstraint(['worker_id'], ['workers.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_fraud_tenant_worker', 'fraud_events', ['tenant_id', 'worker_id'])

    # webhook_dead_letters
    op.create_table(
        'webhook_dead_letters',
        sa.Column('id',              postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('tenant_id',       postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('event',           sa.String(100), nullable=False),
        sa.Column('payload',         sa.JSON, nullable=False),
        sa.Column('total_attempts',  sa.Integer, nullable=False),
        sa.Column('last_error',      sa.Text),
        sa.Column('idempotency_key', sa.String(128)),
        sa.Column('created_at',      sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column('replayed_at',     sa.DateTime(timezone=True)),
        sa.Column('is_replayed',     sa.Boolean, default=False),
        sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id']),
        sa.PrimaryKeyConstraint('id'),
    )

    # Upgrade api_keys table
    op.add_column('api_keys', sa.Column('hash_algorithm', sa.String(20), server_default='argon2'))
    op.add_column('api_keys', sa.Column('revoked_at',     sa.DateTime(timezone=True)))
    op.add_column('api_keys', sa.Column('revoked_reason', sa.String(200)))
    op.add_column('api_keys', sa.Column('rate_limit_rpm', sa.Integer))

    # Upgrade tenants table
    op.add_column('tenants', sa.Column('jwt_secret', sa.String(64)))

    # Upgrade worker_scores
    op.add_column('worker_scores', sa.Column('ewma_quality',         sa.Float, server_default='0.5'))
    op.add_column('worker_scores', sa.Column('ewma_alpha',           sa.Float, server_default='0.3'))
    op.add_column('worker_scores', sa.Column('zscore_latest',        sa.Float))
    op.add_column('worker_scores', sa.Column('zscore_flagged_count', sa.Integer, server_default='0'))
    op.add_column('worker_scores', sa.Column('task_baselines',       sa.JSON,  server_default='{}'))
    op.add_column('worker_scores', sa.Column('confidence_weight',    sa.Float, server_default='0.5'))

    # Upgrade tasks
    op.add_column('tasks', sa.Column('idempotency_key', sa.String(128), unique=True))


def downgrade() -> None:
    op.drop_table('ledger_entries')
    op.drop_table('refresh_tokens')
    op.drop_table('fraud_events')
    op.drop_table('webhook_dead_letters')
    op.drop_column('api_keys', 'hash_algorithm')
    op.drop_column('api_keys', 'revoked_at')
    op.drop_column('api_keys', 'revoked_reason')
    op.drop_column('api_keys', 'rate_limit_rpm')
    op.drop_column('tenants', 'jwt_secret')
