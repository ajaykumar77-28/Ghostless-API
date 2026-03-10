-- ─────────────────────────────────────────────────────────────────────────────
-- Ghostless API — PostgreSQL Schema
-- Run automatically by Docker Compose on first startup
-- ─────────────────────────────────────────────────────────────────────────────

CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS "pg_trgm";

-- ── Tenants ───────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS tenants (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name            VARCHAR(200) NOT NULL,
    slug            VARCHAR(100) UNIQUE NOT NULL,
    tier            VARCHAR(20) DEFAULT 'starter' CHECK (tier IN ('starter','growth','enterprise')),
    webhook_url     VARCHAR(500),
    webhook_secret  VARCHAR(64),
    webhook_events  JSONB DEFAULT '[]',
    brand_name      VARCHAR(200),
    brand_color     VARCHAR(7),
    is_active       BOOLEAN DEFAULT TRUE,
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    metadata        JSONB DEFAULT '{}'
);

-- ── API Keys ──────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS api_keys (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id    UUID NOT NULL REFERENCES tenants(id),
    name         VARCHAR(100),
    key_hash     VARCHAR(64) UNIQUE NOT NULL,
    key_prefix   VARCHAR(30),
    is_active    BOOLEAN DEFAULT TRUE,
    last_used_at TIMESTAMPTZ,
    created_at   TIMESTAMPTZ DEFAULT NOW(),
    expires_at   TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS ix_api_keys_hash ON api_keys(key_hash) WHERE is_active = TRUE;

-- ── Workers ───────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS workers (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id   UUID NOT NULL REFERENCES tenants(id),
    external_id VARCHAR(200) NOT NULL,
    tier        VARCHAR(20) DEFAULT 'bronze' CHECK (tier IN ('bronze','silver','gold','elite')),
    status      VARCHAR(20) DEFAULT 'active' CHECK (status IN ('active','suspended','banned')),
    created_at  TIMESTAMPTZ DEFAULT NOW(),
    metadata    JSONB DEFAULT '{}',
    UNIQUE(tenant_id, external_id)
);
CREATE INDEX IF NOT EXISTS ix_workers_tenant ON workers(tenant_id);
CREATE INDEX IF NOT EXISTS ix_workers_status ON workers(status) WHERE status = 'active';

-- ── Worker Scores ─────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS worker_scores (
    worker_id           UUID PRIMARY KEY REFERENCES workers(id),
    tenant_id           UUID NOT NULL,
    trust_score         FLOAT DEFAULT 50.0,
    accuracy_7d         FLOAT,
    accuracy_30d        FLOAT,
    accuracy_all        FLOAT,
    avg_quality_score   FLOAT,
    total_tasks         INTEGER DEFAULT 0,
    accepted_tasks      INTEGER DEFAULT 0,
    speed_flag_count    INTEGER DEFAULT 0,
    fraud_flag_count    INTEGER DEFAULT 0,
    streak_days         INTEGER DEFAULT 0,
    last_active_date    TIMESTAMPTZ,
    last_calculated_at  TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS ix_worker_scores_trust ON worker_scores(tenant_id, trust_score DESC);

-- ── Promotions ────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS promotions (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    worker_id      UUID NOT NULL REFERENCES workers(id),
    from_tier      VARCHAR(20),
    to_tier        VARCHAR(20),
    reason         VARCHAR(500),
    triggered_at   TIMESTAMPTZ DEFAULT NOW(),
    trust_score_at FLOAT
);
CREATE INDEX IF NOT EXISTS ix_promotions_worker ON promotions(worker_id, triggered_at DESC);

-- ── Tasks ─────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS tasks (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       UUID NOT NULL,
    worker_id       UUID REFERENCES workers(id),
    task_type       VARCHAR(100) NOT NULL,
    project_id      VARCHAR(200),
    submitted_at    TIMESTAMPTZ DEFAULT NOW(),
    completion_time FLOAT,
    was_accepted    BOOLEAN,        -- NULL = pending review
    payout_amount   NUMERIC(10,4) DEFAULT 0,
    metadata        JSONB DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS ix_tasks_worker_date  ON tasks(worker_id, submitted_at DESC);
CREATE INDEX IF NOT EXISTS ix_tasks_tenant_date  ON tasks(tenant_id, submitted_at DESC);
CREATE INDEX IF NOT EXISTS ix_tasks_pending      ON tasks(tenant_id) WHERE was_accepted IS NULL;

-- ── Validations ───────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS validations (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    task_id       UUID REFERENCES tasks(id),
    worker_id     UUID REFERENCES workers(id),
    tenant_id     UUID NOT NULL,
    quality_score FLOAT NOT NULL,
    warning_count INTEGER DEFAULT 0,
    error_count   INTEGER DEFAULT 0,
    was_allowed   BOOLEAN NOT NULL,
    validated_at  TIMESTAMPTZ DEFAULT NOW(),
    rule_flags    JSONB DEFAULT '[]',
    processed_ms  INTEGER
);
CREATE INDEX IF NOT EXISTS ix_validations_worker ON validations(worker_id, validated_at DESC);
CREATE INDEX IF NOT EXISTS ix_validations_tenant ON validations(tenant_id, validated_at DESC);

-- ── Messages ──────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS messages (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id    UUID NOT NULL,
    worker_id    UUID REFERENCES workers(id),
    project_id   VARCHAR(200),
    room         VARCHAR(200),
    content      TEXT NOT NULL,
    msg_type     VARCHAR(50) DEFAULT 'chat',
    reply_to_id  UUID REFERENCES messages(id),
    is_moderated BOOLEAN DEFAULT FALSE,
    created_at   TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS ix_messages_room ON messages(room, created_at DESC);

-- ── Bug Reports ───────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS bug_reports (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id      UUID NOT NULL,
    worker_id      UUID REFERENCES workers(id),
    project_id     VARCHAR(200),
    title          VARCHAR(500) NOT NULL,
    description    TEXT,
    severity       VARCHAR(20) DEFAULT 'medium',
    status         VARCHAR(20) DEFAULT 'open',
    ticket_id      VARCHAR(50) UNIQUE,
    screenshot_url VARCHAR(1000),
    created_at     TIMESTAMPTZ DEFAULT NOW(),
    resolved_at    TIMESTAMPTZ
);

-- ── Announcements ─────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS announcements (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id   UUID NOT NULL,
    content     TEXT NOT NULL,
    priority    VARCHAR(20) DEFAULT 'normal',
    created_by  VARCHAR(200),
    created_at  TIMESTAMPTZ DEFAULT NOW(),
    expires_at  TIMESTAMPTZ
);

-- ── Earnings Summaries ────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS earnings_summaries (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    worker_id       UUID REFERENCES workers(id),
    tenant_id       UUID NOT NULL,
    date            TIMESTAMPTZ NOT NULL,
    confirmed_usd   NUMERIC(10,4) DEFAULT 0,
    pending_usd     NUMERIC(10,4) DEFAULT 0,
    tasks_completed INTEGER DEFAULT 0,
    tasks_accepted  INTEGER DEFAULT 0,
    bonus_earned    NUMERIC(10,4) DEFAULT 0,
    payout_method   VARCHAR(50) DEFAULT 'pending',
    UNIQUE(worker_id, date)
);

-- ── Webhook Logs ──────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS webhook_logs (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id   UUID NOT NULL,
    event       VARCHAR(100),
    payload     JSONB,
    status_code INTEGER,
    attempt     INTEGER DEFAULT 1,
    success     BOOLEAN DEFAULT FALSE,
    sent_at     TIMESTAMPTZ DEFAULT NOW(),
    duration_ms INTEGER
);
CREATE INDEX IF NOT EXISTS ix_webhook_logs_tenant ON webhook_logs(tenant_id, sent_at DESC);
