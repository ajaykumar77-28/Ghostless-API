# Ghostless API

**Infrastructure for crowdsourcing platforms.**
Real-time task validation, worker scoring, earnings tracking, and community — as a service.

---

## Quickstart (3 minutes)

```bash
# 1. Clone + configure
git clone https://github.com/ajaykumar77-28/Ghostless-API.git
cd ghostless-api
cp .env.example .env
# Edit .env — change SECRET_KEY at minimum

# 2. Start everything
docker compose up -d

# 3. Create your first tenant
curl -X POST http://localhost:8000/v1/tenants \
  -H "Content-Type: application/json" \
  -d '{"name": "Acme Corp", "slug": "acme-corp", "tier": "growth"}'

# Returns: { "api_key": "sk_live_gl_...", ... }
# Store this key — it's only shown once.

# 4. Run your first validation
curl -X POST http://localhost:8000/v1/validate/task \
  -H "Content-Type: application/json" \
  -H "X-API-Key: sk_live_gl_<your-key>" \
  -H "X-Tenant-ID: acme-corp" \
  -d '{
    "worker_id": "w_001",
    "task_type": "survey",
    "payload": {"responses": {"q1":"A","q2":"A","q3":"A","q4":"A"}},
    "completion_time": 8.5
  }'

# 5. Open the API docs
open http://localhost:8000/docs
```

---

## API Endpoints

### Validation
| Method | Path | Description |
|--------|------|-------------|
| POST | `/v1/validate/task` | Pre-validate a task submission |

### Scoring
| Method | Path | Description |
|--------|------|-------------|
| GET | `/v1/workers/{id}/score` | Get worker trust score |
| GET | `/v1/workers/leaderboard` | Top workers for a tenant |
| POST | `/v1/workers/{id}/score/recalc` | Force score recalculation |
| GET | `/v1/workers/{id}/promotions` | Promotion history |

### Earnings
| Method | Path | Description |
|--------|------|-------------|
| GET | `/v1/earnings/live` | Real-time earnings snapshot |
| GET | `/v1/earnings/summary` | Daily/weekly/monthly summary |
| POST | `/v1/earnings/task/{id}/accept` | Accept task + confirm payout |
| POST | `/v1/earnings/task/{id}/reject` | Reject task |

### Community Hub
| Method | Path | Description |
|--------|------|-------------|
| WS | `/v1/projects/{id}/chat` | Real-time project chat |
| GET | `/v1/workers/{id}/messages` | Get DMs |
| POST | `/v1/workers/{id}/messages` | Send DM |
| POST | `/v1/bugs/report` | Submit bug report |
| GET | `/v1/bugs/{ticket_id}` | Bug status |
| POST | `/v1/announcements` | Broadcast announcement |
| GET | `/v1/announcements` | List active announcements |

### Tenant Management
| Method | Path | Description |
|--------|------|-------------|
| POST | `/v1/tenants` | Create tenant (onboard client) |
| GET | `/v1/tenants/me` | Tenant info |
| POST | `/v1/tenants/me/api-keys` | Generate API key |
| DELETE | `/v1/tenants/me/api-keys/{id}` | Revoke API key |
| PUT | `/v1/tenants/me/webhooks` | Configure webhooks |
| GET | `/v1/tenants/me/usage` | Usage stats |

---

## Authentication

### Server-to-server (recommended)
```
X-API-Key: sk_live_gl_<your-key>
X-Tenant-ID: your-tenant-slug
```

### Worker-facing (JWT)
```
Authorization: Bearer <jwt-token>
X-Tenant-ID: your-tenant-slug
```

Generate a worker JWT:
```python
from app.middleware.auth import create_worker_token
token = create_worker_token(worker_id="w_001", tenant_id="your-tenant-id")
```

---

## Webhook Events

Configure your webhook endpoint via `PUT /v1/tenants/me/webhooks`.

Verify signatures:
```python
import hmac, hashlib

def verify(payload_bytes: bytes, signature_header: str, secret: str) -> bool:
    expected = "sha256=" + hmac.new(
        secret.encode(), payload_bytes, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, signature_header)
```

Available events:
- `task.validated` — fires after every pre-validation call
- `task.accepted` — client platform accepted a task
- `task.rejected` — client platform rejected a task
- `worker.promoted` — tier upgrade or downgrade
- `worker.suspended` — worker suspended
- `payout.sent` — payout dispatched
- `bug.reported` — new bug report (critical only by default)

---

## JavaScript SDK

```javascript
import GhostlessSDK from '@ghostless/sdk';

const gl = new GhostlessSDK({
  apiKey:   'sk_live_gl_...',
  tenantId: 'acme-corp',
});

// Validate before submit
const result = await gl.validateTask({
  workerId:       'w_001',
  taskType:       'survey',
  payload:        { responses: { q1: 'A', q2: 'B', q3: 'C', q4: 'A' } },
  completionTime: 95,
});

if (result.allow_submit) {
  submitTask();
} else {
  showWarnings(result.warnings);
}

// Live earnings
const earnings = await gl.getLiveEarnings('w_001');
console.log(`$${earnings.predicted_payout_usd} predicted`);

// Project chat
const ws = gl.connectToProjectChat('project-123', 'w_001', (msg) => {
  console.log(msg.worker_id, ':', msg.content);
});
```

---

## Python SDK

```python
from ghostless import GhostlessClient

client = GhostlessClient(
    api_key="sk_live_gl_...",
    tenant_id="acme-corp",
)

# Validate a task
result = client.validate_task(
    worker_id="w_001",
    task_type="transcription",
    payload={"text": "The meeting was held on Tuesday.", "audio_length_seconds": 18},
    completion_time=95.0,
)
print(result["quality_score"], result["allow_submit"])

# Get live earnings
earnings = client.get_live_earnings("w_001")
print(f"${earnings['predicted_payout_usd']:.2f} predicted payout")
```

---

## Architecture

```
Client Platform
      │
      ▼
  API Gateway (FastAPI + nginx)
  ├── JWT + API Key Auth
  ├── Rate Limiting (Redis)
  └── Tenant Isolation
      │
      ├── /v1/validate  →  Validation Service
      │                    Rule Engine + ML Hook
      │
      ├── /v1/workers   →  Scoring Service
      │                    Trust Score + Promotions
      │
      ├── /v1/earnings  →  Earnings Service
      │                    Live Payouts + Bonuses
      │
      └── /v1/projects  →  Hub Service (WebSocket)
                           Chat + Bug Reports
      │
      ▼
  PostgreSQL ← Celery ← Redis ← Stream (validations)
                │
                └── Webhook Dispatcher (signed HMAC)
                          │
                          ▼
                  Client Webhook Endpoint
```

---

## Production Deployment

```bash
# Set real secrets
export SECRET_KEY=$(openssl rand -base64 32)
export ENVIRONMENT=production
export DEBUG=false

# Deploy (Hetzner, DO, AWS, GCP — anything with Docker)
docker compose -f docker-compose.yml up -d

# Scale API workers
docker compose up -d --scale api=3

# Monitor
docker compose logs -f api
curl http://localhost:8000/health
curl http://localhost:8000/metrics
```

---

## Pricing (for your clients)

| Tier | Price | Validations/mo |
|------|-------|----------------|
| Starter | $49/mo + $0.004/validation | 50K |
| Growth | $299/mo + $0.002/validation | 500K |
| Enterprise | Custom | Unlimited |

---

## License

PLease Contact Me
