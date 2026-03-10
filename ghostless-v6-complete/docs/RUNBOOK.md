# Ghostless API v6 — Failure Recovery Runbook

> **Last updated:** v6.0.0  
> **On-call rotation:** See PagerDuty schedule  
> **Severity definitions:** P1 = full outage | P2 = degraded | P3 = minor impact

---

## Quick Reference

| Signal | Likely Cause | First Action |
|--------|-------------|--------------|
| API returns 500 | App crash / unhandled exception | `docker compose logs --tail=100 api` |
| API returns 503 | DB or Redis unreachable | Check infra: `./scripts/health.sh` |
| Webhooks not delivering | Celery worker down or queue full | `docker compose logs worker` + check queue depth |
| Scores not updating | Celery beat not running | `docker compose ps scheduler` |
| All endpoints 502 | Nginx → API upstream dead | Check API containers: `docker compose ps api` |
| Queue depth > 10k | Workers overwhelmed | Scale workers: `./scripts/scale.sh worker 4` |

---

## Scenario 1: API Container Crash Loop

**Symptoms:** 502 from Nginx, container restarting, error logs in `docker compose logs api`

**Diagnosis:**
```bash
docker compose logs --tail=200 api          # find the crash reason
docker compose ps api                        # check restart count
docker compose inspect api | grep -i health # health check status
```

**Resolution steps:**
1. If OOM (Out of Memory):
   ```bash
   docker stats ghostless-api                   # confirm memory usage
   # In docker-compose.prod.yml, increase memory limit from 1G to 2G
   docker compose up -d api                     # restart with new limit
   ```

2. If app exception (ImportError, syntax error after deploy):
   ```bash
   ./scripts/deploy.sh --rollback               # roll back to previous image
   ```

3. If DB connection error:
   ```bash
   docker compose exec api python -c "from app.database import check_db_connection; import asyncio; print(asyncio.run(check_db_connection()))"
   # If False → go to Scenario 3
   ```

**Recovery validation:**
```bash
curl http://localhost:8000/health | python3 -m json.tool
# Expect: {"status": "ok", "database": "connected"}
```

---

## Scenario 2: Database Unavailable

**Symptoms:** API returns 503, `/health` shows `"database": "unavailable"`

**Diagnosis:**
```bash
docker compose ps db                         # is container running?
docker compose logs --tail=50 db             # any errors?
docker compose exec db pg_isready -U ghost   # is Postgres accepting connections?
```

**Resolution steps:**

**Case A — Container stopped:**
```bash
docker compose up -d db
# Wait for health check to pass (up to 60s)
docker compose logs -f db
```

**Case B — Disk full (common on small VMs):**
```bash
df -h                                        # check disk usage
docker system prune -f                       # remove unused layers, stopped containers
# If still full, rotate old logs:
find /var/log -name "*.log" -mtime +7 -delete
docker compose restart db
```

**Case C — Postgres crashed on bad write:**
```bash
docker compose exec db psql -U ghost -d ghostless -c "SELECT pg_postmaster_start_time();"
# If error, the DB needs recovery:
docker compose stop db
docker compose run --rm db postgres --single -D /var/lib/postgresql/data ghostless
# Then restart normally:
docker compose up -d db
```

**Case D — Need to restore from backup:**
```bash
# Restore from latest backup (adjust bucket path)
aws s3 cp s3://ghostless-backups/latest.dump /tmp/ghostless.dump
docker compose exec -T db pg_restore -U ghost -d ghostless /tmp/ghostless.dump
```

**API recovery after DB is up:**
```bash
docker compose restart api worker webhook-worker
```

---

## Scenario 3: Redis Unavailable

**Symptoms:** Validation endpoint slow/failing, Celery tasks not processing

**Diagnosis:**
```bash
docker compose ps redis
docker compose exec redis redis-cli ping    # expect: PONG
docker compose logs --tail=50 redis
```

**Resolution steps:**
```bash
# Most common: container stopped
docker compose up -d redis

# If Redis OOM evicted too aggressively, increase maxmemory in .env:
# REDIS_MAX_MEMORY=1gb
docker compose up -d redis

# Verify
docker compose exec redis redis-cli info memory | grep used_memory_human
```

**Important:** The API has Redis fallback mode (`REDIS_FALLBACK_ENABLED=true`).
While Redis is down, scoring uses DB-backed queries (slower but functional).
Worker history cache is bypassed — no data loss, just slower responses.

---

## Scenario 4: Celery Workers Down (Webhooks Not Delivering, Scores Not Updating)

**Symptoms:** Scores stuck, webhooks timing out, queue depth growing

**Diagnosis:**
```bash
docker compose ps worker webhook-worker scheduler
docker compose logs --tail=100 worker
./scripts/health.sh                          # check queue depths
```

**Resolution steps:**
```bash
# Restart all Celery services
docker compose restart worker webhook-worker scheduler

# If tasks are stuck in the queue, check queue depth:
docker compose exec redis redis-cli llen celery

# If queue is enormous (> 50k), drain it with care:
# Option A: process normally (just need more workers)
docker compose up -d --scale worker=4       # add workers temporarily

# Option B: if queue contains poison tasks causing crash loops:
docker compose exec redis redis-cli del celery  # ⚠️ DESTRUCTIVE — discards all queued tasks
```

**Verify recovery:**
```bash
docker compose exec redis redis-cli llen celery      # should be draining
docker compose logs -f worker | grep "Task.*succeeded"
```

---

## Scenario 5: High Queue Depth / Scaling Workers

**Symptoms:** Queue depth > 5000, webhook delivery lag > 5 minutes

**Scale workers (Docker Compose):**
```bash
# Scale general workers to 4 replicas
docker compose up -d --scale worker=4

# Scale webhook workers separately
docker compose up -d --scale webhook-worker=3

# Scale back down when queue clears
docker compose up -d --scale worker=2 --scale webhook-worker=1
```

**Scale workers (Docker Swarm):**
```bash
docker service scale ghostless_worker=6
docker service scale ghostless_webhook-worker=3
```

**Persistent scaling thresholds:**

| Queue Depth | Action |
|------------|--------|
| > 1,000    | Monitor — may be transient |
| > 5,000    | Scale workers to 4 |
| > 20,000   | Scale workers to 8, alert on-call |
| > 50,000   | Incident — likely something producing tasks at abnormal rate |

---

## Scenario 6: Score Calculation Stopped

**Symptoms:** `last_calculated_at` on WorkerScore is hours old, no score updates

**Diagnosis:**
```bash
docker compose ps scheduler             # is celery beat running? should be exactly 1
docker compose logs --tail=50 scheduler
# Check if beat schedule file exists:
docker compose exec scheduler ls -la /tmp/celerybeat-schedule
```

**Resolution:**
```bash
# Restart scheduler (safe — beat is idempotent, won't double-fire tasks)
docker compose restart scheduler

# Force manual recalculation for specific worker:
curl -X POST http://localhost:8000/v1/workers/{worker_id}/score/recalc \
     -H "X-API-Key: your-key" -H "X-Tenant-ID: your-tenant"
```

**Important:** Only ever run ONE scheduler container. Two beats will double-fire every periodic task.

---

## Scenario 7: Nginx 502 Bad Gateway

**Symptoms:** All API requests return 502

**Diagnosis:**
```bash
docker compose logs nginx
docker compose ps api
curl http://localhost:8000/health   # test API directly (bypassing Nginx)
```

**Resolution:**
```bash
# Test Nginx config first
docker compose exec nginx nginx -t

# If config is valid but API is down:
docker compose restart api
# Wait for health check, then:
docker compose restart nginx

# If TLS certs expired:
# Renew with certbot and copy to infra/certs/
# Then:
docker compose exec nginx nginx -s reload
```

---

## Scenario 8: Data Corruption / Bad Migration

**Symptoms:** Unexpected DB errors, `alembic current` shows wrong version

**Safe rollback:**
```bash
# Check current migration
docker compose run --rm migrate alembic current

# Roll back one migration
docker compose run --rm migrate alembic downgrade -1

# Roll back to specific revision
docker compose run --rm migrate alembic downgrade 004_v5_full
```

**Emergency: restore from backup before bad migration:**
```bash
# 1. Stop the API (stop writes)
docker compose stop api worker webhook-worker

# 2. Restore DB
docker compose exec -T db psql -U ghost -c "DROP DATABASE ghostless;"
docker compose exec -T db psql -U ghost -c "CREATE DATABASE ghostless;"
docker compose exec -T db pg_restore -U ghost -d ghostless < /backups/pre-migration.dump

# 3. Restart with old image
./scripts/deploy.sh --rollback
```

---

## Scenario 9: Fraud / Coordinated Attack

**Symptoms:** Anomaly alerts, sudden spike in fraud events, trust scores manipulated

**Immediate containment:**
```bash
# Shadow-ban a suspicious worker (stops earnings without alerting attacker)
curl -X POST http://localhost:8000/v1/admin/workers/{worker_id}/shadow-ban \
     -H "X-API-Key: admin-key" -H "X-Tenant-ID: tenant"

# Bulk suspend workers from a suspicious IP
curl -X POST http://localhost:8000/v1/admin/workers/bulk-suspend \
     -H "X-API-Key: admin-key" \
     -d '{"ip_address": "1.2.3.4", "reason": "coordinated_attack"}'

# Check coordinated attack events
curl http://localhost:8000/v1/admin/fraud/coordinated \
     -H "X-API-Key: admin-key"
```

**Investigation:**
```bash
# View recent fraud events
docker compose exec db psql -U ghost -d ghostless -c "
  SELECT w.external_id, fe.event_type, fe.severity, fe.created_at
  FROM fraud_events fe JOIN workers w ON w.id = fe.worker_id
  WHERE fe.created_at > NOW() - INTERVAL '1 hour'
  ORDER BY fe.created_at DESC LIMIT 50;
"

# Check for baseline poisoning
curl http://localhost:8000/v1/admin/baselines/drift \
     -H "X-API-Key: admin-key"
```

---

## Backup Procedures

**Automated backup (add to cron):**
```bash
# /etc/cron.d/ghostless-backup
0 2 * * * root /opt/ghostless/scripts/backup.sh >> /var/log/ghostless-backup.log 2>&1
```

```bash
# scripts/backup.sh
#!/bin/bash
DATE=$(date +%Y%m%d_%H%M%S)
BACKUP_DIR=/backups
mkdir -p "$BACKUP_DIR"

# DB backup
docker compose exec -T db pg_dump -U ghost ghostless | \
  gzip > "$BACKUP_DIR/ghostless_${DATE}.sql.gz"

# Keep last 30 days
find "$BACKUP_DIR" -name "*.sql.gz" -mtime +30 -delete

# Upload to S3 (optional)
# aws s3 cp "$BACKUP_DIR/ghostless_${DATE}.sql.gz" s3://your-backup-bucket/
echo "Backup complete: ghostless_${DATE}.sql.gz"
```

---

## Monitoring Thresholds (alert if exceeded)

| Metric | Warning | Critical |
|--------|---------|----------|
| API response time (p99) | > 500ms | > 2s |
| Error rate (5xx) | > 1% | > 5% |
| Queue depth (default) | > 1,000 | > 10,000 |
| Queue depth (webhooks) | > 500 | > 5,000 |
| DB connection pool | > 80% | > 95% |
| Redis memory | > 70% | > 90% |
| Disk usage | > 80% | > 90% |
| Fraud events/hour | > 50 | > 200 |
| Worker score drift | > 2σ | > 3.5σ |
