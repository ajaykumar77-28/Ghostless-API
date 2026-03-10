"""
Ghostless API — Long-Horizon Fraud Clustering Task (v5)

Runs every 30 minutes via Celery beat.
Looks back FRAUD_CLUSTER_LOOKBACK_DAYS for shared signals across workers.

Unlike the real-time 5-minute window in validate.py, this task finds:
  - Slow-drip coordinated attacks spread over hours/days
  - IP rotation patterns (same worker pool, rotating IPs)
  - Answer template reuse over time
  - Graph components that only become visible at longer windows

Results are persisted to coordinated_attack_events and can trigger
auto review queue population for admin review.
"""
import json
from datetime import datetime, timedelta
from typing import List

from app.config import settings
from app.services.graph_clustering import (
    WorkerNode, find_suspicious_clusters, fingerprint_device,
)
from app.tasks.webhooks import celery_app


def _get_sync_conn():
    import psycopg2
    return psycopg2.connect(settings.DATABASE_URL.replace("+asyncpg", ""))


@celery_app.task(
    name="ghostless.run_fraud_clustering",
    bind=True,
    max_retries=3,
)
def run_fraud_clustering(self):
    """
    Long-horizon coordinated attack detection across all tenants.
    """
    conn = _get_sync_conn()
    total_clusters = 0
    try:
        cur = conn.cursor()

        # Get all active tenants
        cur.execute("SELECT id FROM tenants WHERE is_active = TRUE")
        tenant_ids = [str(r[0]) for r in cur.fetchall()]

        for tenant_id in tenant_ids:
            try:
                n = _cluster_for_tenant(cur, conn, tenant_id)
                total_clusters += n
            except Exception as e:
                import logging
                logging.getLogger(__name__).error(
                    f"Clustering failed for tenant {tenant_id}: {e}"
                )
                continue

        return {"tenants": len(tenant_ids), "new_clusters": total_clusters}

    except Exception as exc:
        raise self.retry(exc=exc, countdown=60)
    finally:
        conn.close()


def _cluster_for_tenant(cur, conn, tenant_id: str) -> int:
    """Run graph clustering for one tenant. Returns count of new clusters found."""
    lookback_days = settings.FRAUD_CLUSTER_LOOKBACK_DAYS
    since = datetime.utcnow() - timedelta(days=lookback_days)

    # Fetch recent workers and their signals
    cur.execute("""
        SELECT DISTINCT
            w.id::text            AS worker_id,
            fe.ip_address,
            fe.user_agent,
            t.metadata_->>'payload_hash' AS answer_hash
        FROM workers w
        LEFT JOIN fraud_events fe ON fe.worker_id = w.id AND fe.tenant_id = w.tenant_id
        LEFT JOIN tasks t         ON t.worker_id = w.id  AND t.tenant_id = w.tenant_id
        WHERE w.tenant_id = %s
          AND w.status = 'active'
          AND (fe.created_at > %s OR t.submitted_at > %s)
    """, (tenant_id, since, since))
    rows = cur.fetchall()

    if not rows:
        return 0

    # Build WorkerNode objects
    worker_nodes: dict = {}
    for row in rows:
        worker_id, ip, user_agent, answer_hash = row
        if not worker_id:
            continue
        if worker_id not in worker_nodes:
            worker_nodes[worker_id] = WorkerNode(worker_id=worker_id)
        node = worker_nodes[worker_id]
        if ip:
            node.ip_addresses.add(ip)
        if user_agent:
            node.device_hashes.add(fingerprint_device(user_agent))
        if answer_hash:
            node.answer_hashes.add(answer_hash)

    if len(worker_nodes) < settings.FRAUD_CLUSTER_MIN_WORKERS:
        return 0

    # Run graph clustering
    clusters = find_suspicious_clusters(
        list(worker_nodes.values()),
        min_cluster_size=settings.FRAUD_CLUSTER_MIN_WORKERS,
        min_shared_signals=2,
    )

    new_cluster_count = 0
    for cluster in clusters:
        # Check if this cluster (by payload_hash or worker set) was already recorded
        sorted_ids = sorted(cluster.worker_ids)
        cluster_signature = json.dumps(sorted_ids)

        cur.execute("""
            SELECT id FROM coordinated_attack_events
            WHERE tenant_id = %s
              AND worker_ids::text = %s::text
              AND created_at > NOW() - INTERVAL '24 hours'
        """, (tenant_id, cluster_signature))
        if cur.fetchone():
            continue   # already recorded today

        # Persist new cluster event
        cur.execute("""
            INSERT INTO coordinated_attack_events (
                id, tenant_id, payload_hash, worker_ids,
                worker_count, detection_type, lookback_hours
            ) VALUES (gen_random_uuid(), %s, %s, %s::jsonb, %s, 'long_horizon', %s)
        """, (
            tenant_id,
            cluster.shared_signals[0]["value"] if cluster.shared_signals else "unknown",
            cluster_signature,
            cluster.total_workers,
            lookback_days * 24.0,
        ))

        # Create FraudEvent for each worker in the cluster
        for worker_id in cluster.worker_ids:
            # Check if worker already has a coordination fraud event recently
            cur.execute("""
                SELECT id FROM fraud_events
                WHERE worker_id = %s AND tenant_id = %s
                  AND event_type = 'coordinated_attack'
                  AND created_at > NOW() - INTERVAL '24 hours'
            """, (worker_id, tenant_id))
            if cur.fetchone():
                continue

            cur.execute("""
                INSERT INTO fraud_events (
                    id, tenant_id, worker_id,
                    event_type, severity, reason_codes, details,
                    auto_action, progressive_level, reviewed
                ) VALUES (
                    gen_random_uuid(), %s, %s,
                    'coordinated_attack', 'critical',
                    %s::jsonb, %s::jsonb, 'flag', 2, FALSE
                )
            """, (
                tenant_id, worker_id,
                json.dumps(["coordination"]),
                json.dumps({
                    "cluster_score":    cluster.cluster_score,
                    "cluster_size":     cluster.total_workers,
                    "shared_signals":   cluster.shared_signals,
                    "lookback_hours":   lookback_days * 24,
                    "detected_at":      datetime.utcnow().isoformat(),
                }),
            ))

        # Populate auto review queue (just means creating an unreviewed appeal stub)
        # This surfaces in the admin suspicious workers list automatically
        new_cluster_count += 1

    conn.commit()
    return new_cluster_count
