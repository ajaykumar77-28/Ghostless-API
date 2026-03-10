# Ghostless API v2

A production-grade crowdsourcing quality engine. Multi-tenant, task validation, trust scoring, and earnings tracking.

---

## What's in v2 (this release)

### ✅ Automated Tests
```bash
# Install deps
pip install -r requirements.txt

# Run all tests (SQLite, no Docker needed)
pytest

# With coverage report
pytest --cov=app --cov-report=html
```

Tests cover:
- **Unit**: `test_scoring.py` — EWMA math, z-score, Welford variance, confidence weighting (40+ assertions)
- **Unit**: `test_rule_engine.py` — all 5 task types, speed flags, trust adjustment
- **Unit**: `test_fraud.py` — velocity, entropy, IP sharing, speed anomaly, answer clustering
- **Unit**: `test_auth_service.py` — argon2 hashing, JWT claims, audience validation, per-tenant secrets
- **Integration**: `test_integration_auth.py` — real FastAPI app, SQLite test DB, tenant isolation
- **Integration**: `test_ledger.py` — atomic credits, reversals, idempotency, cross-tenant isolation

### ✅ Real Scoring Math (`app/services/scoring.py`)
Replaced the naive weighted-average SQL formula with:
- **EWMA trust score** — α starts at 0.30 for new workers, decays to 0.10 for veterans
- **Rolling accuracy** — 7d / 30d / all-time windows
- **Z-score anomaly detection** — compares each submission against per-task-type peer baseline using Welford online variance
- **Per-task baseline tracking** — O(1) Welford update, no full table scan
- **Confidence weighting** — new workers (< 50 accepted tasks) score at 40–100% weight; prevents gaming leaderboard

Trust score formula:
```
trust = clamp(
    ewma_quality × 60 × confidence_weight
  + accuracy_30d × 25
  + streak_bonus × 10   (capped at 30 days)
  + tenure_bonus × 5    (capped at 1 year)
  - fraud_penalty       (3 pts/event, max 20)
, 0, 100)
```

### ✅ Proper Tenant Isolation
- **Every table** has `tenant_id` column with composite indexes `(tenant_id, worker_id)` and `(tenant_id, task_id)`
- **Service layer** enforces tenant on all queries (ledger reversal, balance query, etc.)
- **Middleware** validates tenant ID on every request before touching DB
- Cross-tenant queries return 404/403, never data from another tenant

### ✅ Hashed API Keys (argon2)
- New keys hashed with **argon2id** via `passlib[argon2]`
- Legacy SHA-256 keys still verifiable (gradual migration path)
- `hash_algorithm` column tracks which algorithm to use
- Per-key `rate_limit_rpm` override
- `revoked_at` + `revoked_reason` for explicit revocation
- Key rotation: issue new key → revoke old key → zero downtime

### ✅ JWT Security Upgrades
- **`iss`** (issuer) = `"ghostless-api"`
- **`aud`** (audience) = `"ghostless:{tenant_id}"` — cross-tenant token reuse rejected
- **`jti`** (JWT ID) on every token — individual revocation via Redis blacklist
- **Per-tenant signing secret** (`tenants.jwt_secret`) — tenant data breach doesn't expose other tenants' tokens
- **Refresh tokens** — stored as SHA-256 hash, rotated on each use (family-based reuse detection)
- **Worker generation revocation** — `revoke_all_worker_tokens()` mass-invalidates all tokens for a worker
- Short access token TTL (60 min default), long refresh TTL (30 days default)

### ✅ Webhook System (complete)
- **Exponential backoff with jitter**: 60s → 120s → 240s → 480s → 960s
- **Idempotency keys**: SHA-256 of `{tenant}:{event}:{payload}` — Redis dedup window prevents double delivery
- **Delivery status tracking**: every attempt logged to `webhook_logs`
- **Dead-letter queue**: `webhook_dead_letters` table after max retries — query + replay manually
- Signature: `X-Ghostless-Signature: sha256=<hmac>` on every delivery

### ✅ Earnings Ledger (honest)
- **Append-only ledger** (`ledger_entries`) — double-entry, never update rows
- **Atomic balance updates** with idempotency keys (no double-credit)
- **Reversal entries** — rejection creates a negative entry, not an update
- **`is_confirmed` flag** — pending vs confirmed balance
- **Note**: Dollar amounts are score predictions until a real payment provider (Stripe, PayPal) is integrated. `payout_sent` entry type is ready for that integration.

### ✅ Alembic Migrations
```bash
# Apply migrations
alembic upgrade head

# Generate new migration after model change
alembic revision --autogenerate -m "add_new_table"

# Rollback one step
alembic downgrade -1
```

### ✅ Fraud Heuristics (`app/services/fraud.py`)
- **Velocity limiter**: 60 tasks/hour, 300 tasks/day (configurable per tenant)
- **Payload entropy**: Shannon entropy check — low entropy = bot fill
- **Answer clustering**: structural hash deduplication of payloads
- **IP fingerprinting**: flag > 3 distinct workers from same IP within tenant
- **Speed anomaly**: z-score vs peer baseline (independent of rule_engine threshold)

### ✅ Domain Separation
```
app/
├── services/
│   ├── auth.py       — key hashing, JWT, token revocation
│   ├── fraud.py      — all fraud heuristics (pure functions)
│   ├── ledger.py     — atomic earnings mutations
│   ├── rule_engine.py — task validation rules
│   └── scoring.py    — trust score math (pure functions, fully testable)
├── middleware/
│   └── auth.py       — FastAPI auth dependency (wires services together)
├── routers/          — HTTP endpoints
├── tasks/            — Celery async tasks
└── models/           — SQLAlchemy models
```

---

## What's NOT in v2 (honest)

- **Real payments**: Not integrated. Ledger is ready for Stripe/PayPal, but no actual transfers happen.
- **ML bonuses**: The `ml_quality_hook` in `rule_engine.py` is still a stub. Replace it with your model server call.
- **WebSocket hardening**: Auth-on-connect, room isolation, and Redis pub/sub are not yet implemented.
- **Observability**: Prometheus metrics are wired in, but trace IDs and structured error dashboards are not.
- **Minhash LSH**: Answer clustering only does exact hash dedup. Approximate near-duplicate detection (minhash/LSH) is marked TODO.

---

## Setup

```bash
# Copy environment
cp .env.example .env

# Start services
docker-compose up -d

# Run migrations
alembic upgrade head

# Start API
uvicorn app.main:app --reload

# Start Celery worker
celery -A app.tasks.webhooks.celery_app worker --loglevel=info

# Start Celery beat (scoring scheduler)
celery -A app.tasks.scoring.celery_app beat --loglevel=info
```

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `DATABASE_URL` | `postgresql+asyncpg://...` | Async PostgreSQL URL |
| `REDIS_URL` | `redis://localhost:6379/0` | Redis for cache + rate limiting |
| `SECRET_KEY` | random | Global JWT signing key |
| `ACCESS_TOKEN_EXPIRE_MINUTES` | `60` | Access token TTL |
| `REFRESH_TOKEN_EXPIRE_DAYS` | `30` | Refresh token TTL |
| `MAX_TASKS_PER_HOUR` | `60` | Fraud velocity limit |
| `WEBHOOK_MAX_RETRIES` | `5` | Max webhook delivery attempts |

## CI Hook

```yaml
# .github/workflows/test.yml
name: Test
on: [push, pull_request]
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: "3.12" }
      - run: pip install -r requirements.txt
      - run: pytest --cov=app --cov-fail-under=70
```
