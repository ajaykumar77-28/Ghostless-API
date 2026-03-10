# Ghostless API v6 — Quick Start Guide

> **Trust scoring and fraud detection for crowdsourcing platforms.**
> Bayesian trust scoring · Webhook delivery · Python & Node SDKs · One-command Docker setup

---

## Table of Contents

1. [Requirements](#1-requirements)
2. [Project Structure](#2-project-structure)
3. [Setup — Development](#3-setup--development)
4. [Setup — Production](#4-setup--production)
5. [Running Tests](#5-running-tests)
6. [API Overview & Endpoints](#6-api-overview--endpoints)
7. [Python SDK](#7-python-sdk)
8. [Node.js SDK](#8-nodejs-sdk)
9. [Webhook Integration](#9-webhook-integration)
10. [Score Explanations](#10-score-explanations)
11. [Operations & Scaling](#11-operations--scaling)
12. [Security Guidelines](#12-security-guidelines)
13. [Troubleshooting](#13-troubleshooting)

---

## 1. Requirements

### System Requirements

| Requirement | Minimum | Recommended |
|-------------|---------|-------------|
| **OS** | Linux / macOS / WSL2 | Ubuntu 22.04+ |
| **CPU** | 2 cores | 4+ cores |
| **RAM** | 2 GB | 4 GB+ |
| **Disk** | 5 GB free | 20 GB+ |

### Software Prerequisites

| Tool | Minimum Version | Check |
|------|----------------|-------|
| **Docker** | 24.0+ | `docker --version` |
| **Docker Compose** | 2.20+ | `docker compose version` |
| **Python** *(local dev/testing only)* | 3.10+ | `python3 --version` |
| **Node.js** *(Node SDK only)* | 18.0+ | `node --version` |
| **Git** | any | `git --version` |
| **curl** | any | `curl --version` |
| **openssl** *(prod secret generation)* | any | `openssl version` |

> **Docker is the only hard requirement** to run the full stack. Python/Node are only needed if you want to run tests locally or use the SDKs directly.

### Python Package Requirements (pinned in `requirements.txt`)

```
# API
fastapi==0.111.0
uvicorn[standard]==0.29.0
pydantic==2.7.1
pydantic-settings==2.2.1

# Database
sqlalchemy==2.0.30
asyncpg==0.29.0
alembic==1.13.1
psycopg2-binary==2.9.9

# Cache & Queue
redis==5.0.4
celery==5.4.0

# Auth & Security
python-jose[cryptography]==3.3.0
passlib[argon2]==1.7.4
argon2-cffi==23.1.0

# HTTP & Utils
httpx==0.27.0
websockets==12.0
tenacity==8.3.0
structlog==24.1.0
prometheus-fastapi-instrumentator==6.1.0

# Dev & Testing
pytest==8.2.0
pytest-asyncio==0.23.6
pytest-cov==5.0.0
```

### Infrastructure (provided via Docker)

| Service | Image | Purpose |
|---------|-------|---------|
| PostgreSQL 16 | `postgres:16-alpine` | Primary database |
| Redis 7 | `redis:7-alpine` | Cache + Celery broker |
| Nginx | `nginx:1.25-alpine` | Reverse proxy, TLS, rate limiting |

---

## 2. Project Structure

```
ghostless-v6-complete/
│
├── 📄 QUICKSTART.md              ← You are here
├── 📄 README.md                  ← Full deployment reference
├── 📄 requirements.txt           ← Pinned Python dependencies
├── 📄 alembic.ini                ← Alembic migration config
│
├── 🐳 Dockerfile                 ← Multi-stage production build
├── 🐳 docker-compose.yml         ← Full stack (dev & prod base)
├── 🐳 docker-compose.prod.yml    ← Production overrides (resource limits)
├── 🐳 .dockerignore              ← Never bake secrets into images
│
├── ⚙️  .env.development           ← Dev environment template → copy to .env
├── ⚙️  .env.production            ← Prod environment template → fill in secrets
├── ⚙️  .env.example               ← Minimal env reference
│
├── 📁 app/                       ← FastAPI application
│   ├── main.py                   ← App entry, middleware, router registration
│   ├── config.py                 ← Settings (loaded from .env)
│   ├── database.py               ← Async SQLAlchemy setup
│   ├── engine/
│   │   ├── bayesian.py           ← Bayesian Beta trust scoring (pure functions)
│   │   ├── pipeline.py           ← Unified decision pipeline
│   │   ├── feature_store.py      ← Centralized signal loading + Redis cache
│   │   └── event_store.py        ← Immutable event sourcing (Redis Streams)
│   ├── routers/
│   │   ├── validate.py           ← POST /v1/validate/task  (hot path)
│   │   ├── scoring.py            ← GET  /v1/workers/{id}/score
│   │   ├── explain.py            ← GET  /v1/scores/{id}/explain
│   │   ├── webhooks.py           ← Webhook management + dead-letter replay
│   │   ├── admin.py              ← Admin actions (suspend, ban, clawback)
│   │   ├── earnings.py           ← Ledger and payout endpoints
│   │   ├── hub.py                ← WebSocket task hub
│   │   └── tenants.py            ← Tenant management
│   ├── middleware/
│   │   ├── auth.py               ← JWT + API key authentication
│   │   └── rbac.py               ← Role-based access control
│   ├── models/models.py          ← All SQLAlchemy ORM models
│   ├── services/                 ← Business logic (fraud, scoring, ledger)
│   └── tasks/                    ← Celery async tasks
│
├── 📁 alembic/versions/          ← Database migrations (run in order)
│   ├── 001_initial_schema.py
│   ├── 002_add_ledger_refresh_tokens.py
│   ├── 003_add_max_trust_and_fixes.py
│   ├── 004_v5_full.py
│   └── 005_v6_improvements.py    ← v6: Bayesian columns, event sourcing, soft deletes
│
├── 📁 tests/                     ← Full test suite (40+ tests)
│   ├── test_v6_suite.py          ← Bayesian, pipeline, adversarial, chaos, fuzz
│   ├── test_scoring.py
│   ├── test_fraud.py
│   ├── test_adversarial.py
│   └── ...
│
├── 📁 scripts/
│   ├── setup.sh                  ← One-command setup (dev + prod)
│   ├── deploy.sh                 ← Zero-downtime deploy + auto-rollback
│   ├── health.sh                 ← Health check (human / JSON / watch)
│   ├── scale.sh                  ← Manual + auto worker scaling
│   └── schema.sql                ← DB schema reference
│
├── 📁 infra/
│   ├── nginx/nginx.conf          ← Production Nginx (rate limits, TLS, proxy)
│   └── prometheus/prometheus.yml ← Metrics scraping config
│
├── 📁 sdks/
│   ├── python/ghostless/         ← Python SDK (sync + async, zero deps)
│   └── node/src/index.ts         ← TypeScript SDK (native fetch, zero deps)
│
└── 📁 docs/
    ├── RUNBOOK.md                ← Failure recovery playbook (9 scenarios)
    └── CHANGES_V6.md             ← v6 changelog
```

---

## 3. Setup — Development

### Step 1: Clone and configure environment

```bash
git clone <your-repo-url> ghostless-v6-complete
cd ghostless-v6-complete

# Copy dev environment template
cp .env.development .env
```

The dev `.env` has safe defaults — no secrets needed to get started locally.

### Step 2: One-command startup

```bash
./scripts/setup.sh
```

This script:
1. Checks Docker is running
2. Builds the multi-stage image (cached after first run)
3. Waits for PostgreSQL and Redis health checks
4. Runs all 5 Alembic migrations automatically
5. Starts API, workers, scheduler, Redis, PostgreSQL, and Nginx
6. Verifies the API is healthy before finishing

**Expected output (last lines):**
```
✅ Ghostless API v6 is running!

  Endpoints:
  API       http://localhost:8000/v1
  Docs      http://localhost:8000/docs
  Health    http://localhost:8000/health
  Metrics   http://localhost:8000/metrics
```

### Step 3: Verify it's working

```bash
# Health check
curl http://localhost:8000/health
# → {"status":"ok","version":"6.0.0","database":"connected"}

# Interactive API docs
open http://localhost:8000/docs   # Swagger UI
open http://localhost:8000/redoc  # ReDoc

# Follow logs
docker compose logs -f api
docker compose logs -f worker
```

### Step 4 (Optional): Start monitoring

```bash
./scripts/setup.sh --monitoring
# Adds: Prometheus (:9090), Grafana (:3000), Flower (:5555)
```

---

## 4. Setup — Production

### Step 1: Generate secrets

```bash
# Generate all required secrets at once
echo "SECRET_KEY=$(openssl rand -hex 32)"
echo "POSTGRES_PASSWORD=$(openssl rand -hex 24)"
echo "REDIS_PASSWORD=$(openssl rand -hex 24)"
```

### Step 2: Configure production environment

```bash
cp .env.production .env
```

Open `.env` and replace **every** `CHANGE_ME` value:

```bash
# Minimum required fields:
SECRET_KEY=<output of openssl rand -hex 32>
POSTGRES_PASSWORD=<strong password>
REDIS_PASSWORD=<strong password>
DATABASE_URL=postgresql+asyncpg://ghost:<POSTGRES_PASSWORD>@db:5432/ghostless
DATABASE_SYNC_URL=postgresql://ghost:<POSTGRES_PASSWORD>@db:5432/ghostless
REDIS_URL=redis://:<REDIS_PASSWORD>@redis:6379/0
CELERY_BROKER_URL=redis://:<REDIS_PASSWORD>@redis:6379/0
CELERY_RESULT_BACKEND=redis://:<REDIS_PASSWORD>@redis:6379/1

# Restrict CORS to your domain
CORS_ORIGINS=https://yourdomain.com

# TLS certs in infra/certs/ (fullchain.pem + privkey.pem)
```

### Step 3: TLS certificates

```bash
mkdir -p infra/certs
# Option A: Let's Encrypt
certbot certonly --standalone -d api.yourdomain.com
cp /etc/letsencrypt/live/api.yourdomain.com/fullchain.pem infra/certs/
cp /etc/letsencrypt/live/api.yourdomain.com/privkey.pem infra/certs/

# Option B: Self-signed (testing only)
openssl req -x509 -nodes -days 365 -newkey rsa:2048 \
  -keyout infra/certs/privkey.pem -out infra/certs/fullchain.pem \
  -subj "/CN=localhost"
```

### Step 4: Launch production stack

```bash
./scripts/setup.sh --prod
```

The script validates that no `CHANGE_ME` values remain and that `SECRET_KEY` is ≥ 32 characters before launching.

### Step 5: Deploy future updates

```bash
# Deploy current HEAD
./scripts/deploy.sh

# Deploy specific version
./scripts/deploy.sh 6.1.0

# Roll back to previous image
./scripts/deploy.sh --rollback
```

---

## 5. Running Tests

### Via Docker (recommended — no local Python needed)

```bash
# Run all tests inside the container
docker compose run --rm api pytest tests/ -v

# Run specific test file
docker compose run --rm api pytest tests/test_v6_suite.py -v

# With coverage report
docker compose run --rm api pytest tests/ --cov=app --cov-report=term-missing
```

### Local Python (requires Python 3.10+)

```bash
# Create virtual environment
python3 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt

# Set test environment variables
export DATABASE_URL="postgresql+asyncpg://ghost:ghost@localhost:5432/ghostless"
export REDIS_URL="redis://localhost:6379/0"
export SECRET_KEY="test-secret-key-min-32-chars-long"
export ENVIRONMENT="test"

# Run migrations first
alembic upgrade head

# Run all tests
pytest tests/ -v

# Run specific categories
pytest tests/test_v6_suite.py -v -k "TestBayesian"
pytest tests/test_adversarial.py -v
pytest tests/test_fraud.py -v
```

### Test suite overview

| Test File | What it covers | Tests |
|-----------|---------------|-------|
| `test_v6_suite.py` | Bayesian math, pipeline, adversarial, chaos, fuzz, load | 40+ |
| `test_scoring.py` | Trust score computation, tiers | 15+ |
| `test_fraud.py` | Velocity, entropy, hash clustering | 12+ |
| `test_adversarial.py` | Farming attacks, score bounce, ceiling attacks | 8+ |
| `test_cold_start.py` | New worker behavior | 6+ |
| `test_ledger.py` | Earnings, payouts, clawbacks | 10+ |
| `test_auth_service.py` | JWT, API key, refresh tokens | 8+ |
| `test_reliability.py` | Redis fallback, DB failure modes | 5+ |

---

## 6. API Overview & Endpoints

Base URL: `https://your-domain.com/v1`

### Authentication

All endpoints require one of:

```bash
# API Key (recommended for server-to-server)
curl -H "X-API-Key: sk_live_gl_your_key" \
     -H "X-Tenant-ID: your-tenant-slug" \
     http://localhost:8000/v1/workers/worker_1/score

# JWT Bearer (for user-facing clients)
curl -H "Authorization: Bearer eyJ..." \
     http://localhost:8000/v1/workers/worker_1/score
```

### Core Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/health` | Liveness + database check |
| `GET` | `/metrics` | Prometheus metrics |
| `POST` | `/v1/validate/task` | **Hot path** — validate a submission |
| `GET` | `/v1/workers/{id}/score` | Worker trust score |
| `POST` | `/v1/workers/{id}/score/recalc` | Trigger immediate recalculation |
| `GET` | `/v1/workers/leaderboard` | Top workers for tenant |
| `GET` | `/v1/scores/{id}/explain` | Full Bayesian explanation |
| `GET` | `/v1/scores/{id}/history` | Score change history |
| `GET` | `/v1/earnings/{id}` | Worker earnings summary |
| `PUT` | `/v1/webhooks/subscription` | Configure webhook endpoint |
| `GET` | `/v1/webhooks/events` | List all available webhook events |
| `POST` | `/v1/webhooks/test` | Fire a test event |
| `GET` | `/v1/webhooks/logs` | Delivery history |
| `POST` | `/v1/webhooks/dead-letters/{id}/replay` | Retry failed delivery |
| `POST` | `/v1/admin/workers/{id}/suspend` | Suspend worker |
| `POST` | `/v1/admin/workers/{id}/ban` | Ban worker |
| `GET` | `/v1/admin/fraud/events` | Fraud event log |
| `GET` | `/v1/admin/flags` | Feature flags |

### Validate a task (primary integration point)

```bash
curl -X POST http://localhost:8000/v1/validate/task \
  -H "X-API-Key: sk_live_gl_your_key" \
  -H "X-Tenant-ID: my-tenant" \
  -H "Content-Type: application/json" \
  -d '{
    "worker_id":        "worker_123",
    "task_type":        "survey",
    "payload":          {"responses": {"q1": "A", "q2": "B", "q3": "C"}},
    "completion_time":  45.2,
    "difficulty":       1.0
  }'
```

**Response:**
```json
{
  "validation_id":    "val_abc123",
  "worker_id":        "worker_123",
  "task_type":        "survey",
  "quality_score":    0.87,
  "allow_submit":     true,
  "warnings":         [],
  "suggestions":      [],
  "flags":            [],
  "anomaly_score":    0.08,
  "velocity_warning": false,
  "shadow_banned":    false,
  "processed_ms":     12
}
```

---

## 7. Python SDK

### Installation

```bash
pip install ghostless              # zero dependencies (sync client only)
pip install ghostless[async]       # + httpx for async client
```

Or use directly from the `sdks/python/` folder:
```bash
pip install ./sdks/python/
```

### Synchronous client (zero dependencies)

```python
from ghostless import GhostlessClient

client = GhostlessClient(
    api_key   = "sk_live_gl_your_key",
    tenant_id = "your-tenant-slug",
    base_url  = "http://localhost:8000",  # local dev
)

# Validate a task submission
result = client.validate(
    worker_id       = "worker_123",
    task_type       = "survey",
    payload         = {"responses": {"q1": "A", "q2": "B"}},
    completion_time = 45.2,
)

# Business-friendly properties
print(result.decision)       # "accept" | "review" | "reject"
print(result.risk_level)     # "low" | "medium" | "high" | "critical"
print(result.explanation)    # "Submission looks good. Quality within expected parameters."
print(result.should_submit)  # True — use this as your gate

# Detailed signals (for your own logic)
print(result.quality_score)   # 0.87
print(result.anomaly_score)   # 0.08
print(result.flags)           # []
print(result.suggestions)     # actionable messages to show the worker

# Worker score
score = client.get_score("worker_123")
print(score.summary)          # "Worker worker_123 — Trusted (78.4/100) | ..."
print(score.trust_label)      # "Trusted"
print(score.trust_score)      # 78.4
print(score.tier)             # Tier.SILVER
print(score.acceptance_rate)  # 0.923

# Full Bayesian explanation
explain = client.get_explanation("worker_123")
print(explain.narrative)        # plain-English paragraph for dashboards
print(explain.ci_lower)         # 0.68  ← 90% credible interval
print(explain.ci_upper)         # 0.87
print(explain.confidence_band)  # "moderate confidence"
print(explain.algorithm_version) # "v6.0.0"
```

### Async client (requires httpx)

```python
import asyncio
from ghostless import AsyncGhostlessClient

async def main():
    async with AsyncGhostlessClient(
        api_key   = "sk_live_gl_your_key",
        tenant_id = "your-tenant-slug",
    ) as client:

        result = await client.validate(
            worker_id       = "worker_456",
            task_type       = "image_label",
            payload         = {"labels": ["cat", "dog"], "confidence": 0.95},
            completion_time = 18.3,
            difficulty      = 1.5,                  # harder task
            idempotency_key = "unique-key-xyz",     # prevent double-validation
        )

        if result.should_submit:
            print(f"✅ {result.decision.upper()} — {result.explanation}")
            await accept_task_on_your_platform(task_id)
        else:
            print(f"❌ {result.explanation}")
            for tip in result.suggestions:
                print(f"   → {tip}")

asyncio.run(main())
```

### Webhook signature verification

```python
from ghostless import GhostlessClient
from fastapi import Request, HTTPException

@app.post("/hooks/ghostless")
async def handle_webhook(request: Request):
    body = await request.body()
    sig  = request.headers.get("X-Ghostless-Signature", "")

    if not GhostlessClient.verify_webhook(body, sig, "your-webhook-secret"):
        raise HTTPException(401, "Invalid signature")

    event = await request.json()
    event_type = event["event"]  # "task.validated", "fraud.detected", etc.

    if event_type == "fraud.detected":
        worker_id = event["data"]["worker_id"]
        severity  = event["data"]["severity"]
        print(f"Fraud detected for {worker_id}: {severity}")

    return {"ok": True}
```

---

## 8. Node.js SDK

### Installation

```bash
npm install ghostless
# or: yarn add ghostless
```

Or use directly from the `sdks/node/` folder:
```bash
cd sdks/node && npm install && npm run build
```

### Usage (TypeScript / JavaScript)

```typescript
import { GhostlessClient } from 'ghostless';

const client = new GhostlessClient({
  apiKey:   'sk_live_gl_your_key',
  tenantId: 'your-tenant-slug',
  baseUrl:  'http://localhost:8000',  // local dev
});

// Validate a submission
const result = await client.validate({
  workerId:       'worker_123',
  taskType:       'survey',
  payload:        { responses: { q1: 'A', q2: 'B' } },
  completionTime: 45.2,
});

console.log(result.decision);      // 'accept' | 'review' | 'reject'
console.log(result.riskLevel);     // 'low' | 'medium' | 'high' | 'critical'
console.log(result.explanation);   // 'Submission looks good.'
console.log(result.shouldSubmit);  // true

// Worker score with helpers
const score = await client.getScore('worker_123');
console.log(score.summary);         // 'Worker worker_123 — Trusted (78.4/100) | ...'
console.log(score.trustLabel);      // 'Trusted'
console.log(score.acceptanceRate);  // 0.923

// Bayesian explanation
const explain = await client.getExplanation('worker_123');
console.log(explain.narrative);          // plain-English paragraph
console.log(explain.confidenceBand);     // 'moderate confidence'
console.log(explain.credible_interval);  // { lower: 0.68, upper: 0.87, width: 0.19 }
```

### Express webhook handler

```typescript
import express from 'express';
import { GhostlessClient, WebhookEvent } from 'ghostless';

const app = express();

app.post('/hooks/ghostless', express.raw({ type: '*/*' }), (req, res) => {
  const isValid = GhostlessClient.verifyWebhook(
    req.body,
    req.headers['x-ghostless-signature'] as string,
    process.env.GHOSTLESS_WEBHOOK_SECRET!,
  );

  if (!isValid) return res.status(401).send('Invalid signature');

  const event = JSON.parse(req.body.toString()) as WebhookEvent;

  switch (event.event) {
    case 'fraud.detected':
      console.log('Fraud:', event.data.worker_id, event.data.severity);
      break;
    case 'worker.promoted':
      console.log('Promoted:', event.data.worker_id, '→', event.data.to_tier);
      break;
    case 'payout.sent':
      console.log('Paid out:', event.data.amount, event.data.currency);
      break;
  }

  res.send('ok');
});
```

---

## 9. Webhook Integration

### Configure your endpoint

```bash
curl -X PUT http://localhost:8000/v1/webhooks/subscription \
  -H "X-API-Key: your-key" \
  -H "X-Tenant-ID: your-tenant" \
  -H "Content-Type: application/json" \
  -d '{
    "url":    "https://yourapp.com/hooks/ghostless",
    "events": ["task.validated", "fraud.detected", "worker.promoted"],
    "secret": "your-32-char-signing-secret"
  }'

# Subscribe to ALL events at once:
# "events": ["*"]
```

### Available events

| Event | Trigger | Key payload fields |
|-------|---------|-------------------|
| `task.validated` | Every validate call | `decision`, `risk_level`, `anomaly_score` |
| `task.accepted` | Task graded accepted | `trust_delta`, `new_trust_score`, `payout_amount` |
| `task.rejected` | Task graded rejected | `trust_delta`, `reason` |
| `worker.promoted` | Tier change | `from_tier`, `to_tier`, `trust_score` |
| `worker.suspended` | Worker suspended | `reason`, `fraud_event_id`, `duration_hours` |
| `worker.score_update` | Score changes ≥ 5 pts | `trust_delta`, `algorithm_version` |
| `fraud.detected` | New fraud event | `severity`, `reason_codes`, `auto_action` |
| `payout.sent` | Payout confirmed | `amount`, `currency` |
| `payout.clawback` | Earnings reversed | `amount`, `reason` |
| `appeal.submitted` | Worker appeals | `appeal_id`, `fraud_event_id` |
| `appeal.resolved` | Admin resolves | `outcome`, `reviewer_notes` |

### Retry behavior

Ghostless retries failed webhooks with exponential backoff:

| Attempt | Delay |
|---------|-------|
| 1 | Immediate |
| 2 | ~60 seconds |
| 3 | ~120 seconds |
| 4 | ~240 seconds |
| 5 | ~480 seconds |
| 6+ | Dead-letter queue |

### Replay dead-lettered deliveries

```bash
# View failed deliveries
curl http://localhost:8000/v1/webhooks/dead-letters \
  -H "X-API-Key: your-key" -H "X-Tenant-ID: your-tenant"

# Replay a specific delivery (safe — uses fresh idempotency key)
curl -X POST http://localhost:8000/v1/webhooks/dead-letters/dl_abc123/replay \
  -H "X-API-Key: your-key" -H "X-Tenant-ID: your-tenant"
```

### Send a test event

```bash
curl -X POST http://localhost:8000/v1/webhooks/test \
  -H "X-API-Key: your-key" -H "X-Tenant-ID: your-tenant" \
  -H "Content-Type: application/json" \
  -d '{"event": "task.validated"}'
```

---

## 10. Score Explanations

The v6 Bayesian engine provides full explainability for every trust score.

```bash
curl http://localhost:8000/v1/scores/worker_123/explain \
  -H "X-API-Key: your-key" -H "X-Tenant-ID: your-tenant"
```

**Response:**
```json
{
  "worker_id":         "worker_123",
  "trust_score":       78.4,
  "algorithm_version": "v6.0.0",
  "posterior_mean":    0.784,
  "credible_interval": {
    "lower": 0.682,
    "upper": 0.873,
    "width": 0.191
  },
  "total_tasks":    234,
  "lifecycle_stage": "trusted",
  "score_volatility": 1.8,
  "components": {
    "base_from_posterior":   78.4,
    "cold_start_factor":     1.0,
    "farming_weight":        1.0,
    "peer_adjustment_pts":   1.2,
    "streak_bonus_pts":      5.0,
    "tenure_bonus_pts":      2.5,
    "velocity_penalty_pts":  0.0,
    "fraud_multiplier":      1.0,
    "trust_delta":           2.34
  },
  "narrative": "This worker is highly trusted with a trust score of 78.4/100. Based on 234 scored tasks, our Bayesian model estimates their true acceptance probability at 78.4%. The 90% credible interval is [68%–87%], indicating moderate confidence in this estimate.",
  "last_updated": "2026-02-20T10:30:00Z"
}
```

The `narrative` field is ready to paste directly into a worker dashboard or admin panel.

---

## 11. Operations & Scaling

### Health check

```bash
./scripts/health.sh            # human-readable table
./scripts/health.sh --json     # JSON (for monitoring)
./scripts/health.sh --watch    # refresh every 30s
```

### Scale workers

```bash
./scripts/scale.sh status              # show current replicas and queue depths
./scripts/scale.sh worker 4            # scale to 4 general workers
./scripts/scale.sh webhook-worker 3    # scale webhook workers
./scripts/scale.sh auto                # auto-scale based on queue depth
```

**When to scale what:**

| Symptom | Scale this |
|---------|-----------|
| Scoring lag, queue depth > 1000 | `worker` |
| Webhook delivery lag > 2 min | `webhook-worker` |
| API response time > 300ms | `api` |
| Scheduler: **never scale above 1** | N/A |

### Celery task queues

| Queue | Worker | Tasks |
|-------|--------|-------|
| `default` | `worker` | Score recalculation, reconciliation |
| `scoring` | `worker` | Periodic trust score batch |
| `webhooks` | `webhook-worker` | Webhook delivery, retries |

### Periodic tasks (Celery Beat)

| Task | Schedule | Purpose |
|------|---------|---------|
| `recalculate_all_scores` | Every 15 min | Batch trust score update |
| `run_fraud_clustering` | Every hour | Graph cluster analysis |
| `reconcile_ledger` | Daily at 2am | Payout reconciliation |

---

## 12. Security Guidelines

### Mandatory in production

- [ ] **Replace ALL `CHANGE_ME` values** in `.env.production` before launch
- [ ] **`SECRET_KEY`** must be ≥ 32 chars — generate with `openssl rand -hex 32`
- [ ] **TLS certificates** placed in `infra/certs/` (fullchain.pem + privkey.pem)
- [ ] **DB and Redis ports** are hidden in `docker-compose.prod.yml` (never exposed externally)
- [ ] **Webhook secrets** set for each tenant so signature verification works
- [ ] **CORS origins** restricted to your domain — not `*`

### RBAC Roles

| Role | What they can do |
|------|----------------|
| `worker` | Submit tasks, view own score and earnings, use hub |
| `reviewer` | + View any score, review fraud events and appeals |
| `admin` | + Suspend/ban/unban workers, manage ledger, view events |
| `super_admin` | + Cross-tenant access, create/delete tenants, feature flags |

### API Key format

```
sk_live_gl_<32 random hex chars>    ← production
sk_dev_gl_<32 random hex chars>     ← development
```

### Request signing (optional, recommended for webhooks)

For inbound requests, set `HMAC_SIGNATURE_REQUIRED=true` in `.env.production`. Clients must include:
```
X-Signature-SHA256: sha256=<HMAC-SHA256(timestamp.body_hash, secret)>
X-Timestamp: <unix timestamp>
```
Requests older than 300 seconds are rejected.

### Never do this

```bash
# ❌ Never commit .env to git
# ❌ Never expose DB or Redis ports externally (they're hidden in prod)
# ❌ Never run as root (the container uses 'ghostless' user)
# ❌ Never run duplicate Celery Beat instances (double-fires all tasks)
# ❌ Never use DEBUG=true in production
```

---

## 13. Troubleshooting

### API won't start

```bash
docker compose logs api        # check error message
docker compose ps              # check container status
```

**Common causes:**
- Missing `SECRET_KEY` in `.env` — the app refuses to start without it
- DB connection string wrong — check `DATABASE_URL` in `.env`
- Port 8000 already in use — change `API_PORT` in `.env`

### Database migration errors

```bash
# Check current migration state
docker compose run --rm migrate alembic current

# Roll back one step if needed
docker compose run --rm migrate alembic downgrade -1

# Re-run forward
docker compose run --rm migrate alembic upgrade head
```

### Scores not updating

The scheduler must be running (exactly 1 instance):
```bash
docker compose ps scheduler    # should show "Up"
docker compose restart scheduler
```

### Webhook deliveries failing

```bash
docker compose logs webhook-worker     # check for errors
./scripts/health.sh                    # check queue depth

# Test your endpoint directly
curl -X POST http://localhost:8000/v1/webhooks/test \
  -H "X-API-Key: key" -H "X-Tenant-ID: tenant" \
  -d '{"event": "task.validated"}'

# Replay failed deliveries
curl http://localhost:8000/v1/webhooks/dead-letters \
  -H "X-API-Key: key" -H "X-Tenant-ID: tenant"
```

### Full reset (dev only)

```bash
./scripts/setup.sh --reset     # destroys all containers and volumes, starts fresh
```

---

## Quick Reference Card

```bash
# ── Start ──────────────────────────────────────────────────────────
./scripts/setup.sh                        # start everything (dev)
./scripts/setup.sh --prod                 # start production stack
./scripts/setup.sh --monitoring           # include Prometheus + Grafana

# ── Deploy ─────────────────────────────────────────────────────────
./scripts/deploy.sh                       # deploy latest
./scripts/deploy.sh 6.1.0                 # deploy specific version
./scripts/deploy.sh --rollback            # roll back to previous

# ── Monitor ────────────────────────────────────────────────────────
./scripts/health.sh                       # health snapshot
./scripts/health.sh --watch               # live monitor (30s refresh)
docker compose logs -f api                # follow API logs
docker compose logs -f worker             # follow worker logs

# ── Scale ──────────────────────────────────────────────────────────
./scripts/scale.sh status                 # current replicas + queue depth
./scripts/scale.sh worker 4               # 4 general workers
./scripts/scale.sh auto                   # auto-scale from queue depth

# ── Test ───────────────────────────────────────────────────────────
docker compose run --rm api pytest tests/ -v
docker compose run --rm api pytest tests/test_v6_suite.py -v

# ── Migrate ────────────────────────────────────────────────────────
docker compose run --rm migrate alembic upgrade head
docker compose run --rm migrate alembic current
docker compose run --rm migrate alembic downgrade -1
```

---

*For failure recovery procedures (DB down, Redis down, crash loops, fraud attacks), see `docs/RUNBOOK.md`.*
*For full deployment architecture and SDK reference, see `README.md`.*
