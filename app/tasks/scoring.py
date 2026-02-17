"""
Ghostless API — Scoring Celery Task

Recalculates trust scores for all active workers every 15 minutes.
Checks promotion thresholds and fires worker.promoted webhooks.
Runs as a Celery Beat periodic task.
"""
import time
from datetime import datetime, timedelta
from typing import Optional

import psycopg2
from celery import Celery
from celery.schedules import crontab

from app.config import settings
from app.tasks.webhooks import celery_app


# ─── Periodic Schedule ────────────────────────────────────────────────────────

celery_app.conf.beat_schedule = {
    "recalculate-all-scores": {
        "task":     "ghostless.recalculate_all_scores",
        "schedule": crontab(minute=f"*/{settings.SCORE_RECALC_INTERVAL_MINUTES}"),
    },
}


def _get_sync_conn():
    sync_url = settings.DATABASE_URL.replace("+asyncpg", "")
    return psycopg2.connect(sync_url)


# ─── Trust Score Formula ──────────────────────────────────────────────────────
#
#   trust_score = (accuracy_30d × 30%)
#               + (avg_quality  × 25%)
#               - speed_penalty  (up to 20pts)
#               + (tenure_score × 10%)
#               + (streak_score × 15%)
#
#   Capped 0–100.

SCORE_SQL = """
WITH base AS (
    SELECT
        w.id                AS worker_id,
        w.tenant_id,
        w.created_at,
        w.tier,

        -- 30d acceptance accuracy
        COALESCE(
            SUM(t.was_accepted::int) FILTER (
                WHERE t.submitted_at > NOW() - INTERVAL '30 days'
                  AND t.was_accepted IS NOT NULL
            )::FLOAT /
            NULLIF(COUNT(t.id) FILTER (
                WHERE t.submitted_at > NOW() - INTERVAL '30 days'
                  AND t.was_accepted IS NOT NULL
            ), 0),
            0.5
        ) * 100 AS accuracy_30d,

        -- 7d acceptance accuracy
        COALESCE(
            SUM(t.was_accepted::int) FILTER (
                WHERE t.submitted_at > NOW() - INTERVAL '7 days'
                  AND t.was_accepted IS NOT NULL
            )::FLOAT /
            NULLIF(COUNT(t.id) FILTER (
                WHERE t.submitted_at > NOW() - INTERVAL '7 days'
                  AND t.was_accepted IS NOT NULL
            ), 0),
            0.5
        ) * 100 AS accuracy_7d,

        -- All-time accuracy
        COALESCE(
            SUM(t.was_accepted::int) FILTER (WHERE t.was_accepted IS NOT NULL)::FLOAT /
            NULLIF(COUNT(t.id) FILTER (WHERE t.was_accepted IS NOT NULL), 0),
            0.5
        ) * 100 AS accuracy_all,

        -- Avg validation quality score (last 30d)
        COALESCE(
            AVG(v.quality_score) FILTER (
                WHERE v.validated_at > NOW() - INTERVAL '30 days'
            ) * 100,
            50.0
        ) AS avg_quality,

        -- Speed flag count (last 30d) — each flag is -2pts, max -20
        LEAST(
            COUNT(v.id) FILTER (
                WHERE v.rule_flags::jsonb @> '["SPEED_FLAG"]'
                  AND v.validated_at > NOW() - INTERVAL '30 days'
            ) * 2,
            20
        ) AS speed_penalty,

        -- Tenure score (days since join, capped at 365)
        LEAST(
            EXTRACT(EPOCH FROM NOW() - w.created_at) / 86400.0, 365
        ) / 365.0 * 100 AS tenure_score,

        -- Task totals
        COUNT(t.id) FILTER (WHERE t.submitted_at IS NOT NULL) AS total_tasks,
        COUNT(t.id) FILTER (WHERE t.was_accepted = TRUE)      AS accepted_tasks,

        -- Speed flag count raw
        COUNT(v.id) FILTER (
            WHERE v.rule_flags::jsonb @> '["SPEED_FLAG"]'
        ) AS speed_flag_count,

        -- Current streak (set externally or default to 0)
        COALESCE(ws.streak_days, 0) AS streak_days

    FROM workers w
    LEFT JOIN tasks       t  ON t.worker_id = w.id
    LEFT JOIN validations v  ON v.worker_id = w.id
    LEFT JOIN worker_scores ws ON ws.worker_id = w.id
    WHERE w.status = 'active'
      AND w.id = %(worker_id)s
    GROUP BY w.id, ws.streak_days
),
computed AS (
    SELECT
        worker_id,
        tenant_id,
        tier,
        accuracy_30d,
        accuracy_7d,
        accuracy_all,
        avg_quality,
        total_tasks,
        accepted_tasks,
        speed_flag_count,
        streak_days,
        GREATEST(0, LEAST(100, ROUND(
            (accuracy_30d * 0.30)
            + (avg_quality  * 0.25)
            - speed_penalty
            + (tenure_score * 0.10)
            + (LEAST(streak_days, 30) / 30.0 * 100 * 0.15)
        , 1))) AS trust_score
    FROM base
)
SELECT * FROM computed;
"""


def _compute_new_tier(trust_score: float, total_tasks: int, current_tier: str) -> str:
    if trust_score >= settings.ELITE_MIN_TRUST and total_tasks >= settings.ELITE_MIN_TASKS:
        return "elite"
    if trust_score >= settings.GOLD_MIN_TRUST and total_tasks >= settings.GOLD_MIN_TASKS:
        return "gold"
    if trust_score >= settings.SILVER_MIN_TRUST and total_tasks >= settings.SILVER_MIN_TASKS:
        return "silver"
    return "bronze"


# ─── Per-worker recalculation ─────────────────────────────────────────────────

@celery_app.task(name="ghostless.recalculate_worker_score")
def recalculate_worker_score(worker_id: str, tenant_id: str):
    """Recalculate trust score for a single worker."""
    conn = _get_sync_conn()
    try:
        cur = conn.cursor()
        cur.execute(SCORE_SQL, {"worker_id": worker_id})
        row = cur.fetchone()
        if not row:
            return {"skipped": True, "reason": "worker_not_found"}

        (
            wid, tid, current_tier, accuracy_30d, accuracy_7d, accuracy_all,
            avg_quality, total_tasks, accepted_tasks, speed_flag_count,
            streak_days, trust_score,
        ) = row

        # Upsert worker_scores
        cur.execute("""
            INSERT INTO worker_scores (
                worker_id, tenant_id, trust_score, accuracy_7d, accuracy_30d, accuracy_all,
                avg_quality_score, total_tasks, accepted_tasks, speed_flag_count,
                streak_days, last_calculated_at
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW())
            ON CONFLICT (worker_id) DO UPDATE SET
                trust_score        = EXCLUDED.trust_score,
                accuracy_7d        = EXCLUDED.accuracy_7d,
                accuracy_30d       = EXCLUDED.accuracy_30d,
                accuracy_all       = EXCLUDED.accuracy_all,
                avg_quality_score  = EXCLUDED.avg_quality_score,
                total_tasks        = EXCLUDED.total_tasks,
                accepted_tasks     = EXCLUDED.accepted_tasks,
                speed_flag_count   = EXCLUDED.speed_flag_count,
                streak_days        = EXCLUDED.streak_days,
                last_calculated_at = NOW()
        """, (
            wid, tid, trust_score, accuracy_7d, accuracy_30d, accuracy_all,
            avg_quality, total_tasks, accepted_tasks, speed_flag_count, streak_days,
        ))

        # Check tier promotion / demotion
        new_tier = _compute_new_tier(trust_score, total_tasks, current_tier)
        if new_tier != current_tier:
            cur.execute("UPDATE workers SET tier = %s WHERE id = %s", (new_tier, wid))
            cur.execute("""
                INSERT INTO promotions (id, worker_id, from_tier, to_tier, reason, trust_score_at)
                VALUES (gen_random_uuid(), %s, %s, %s, %s, %s)
            """, (
                wid, current_tier, new_tier,
                f"Automatic {'promotion' if new_tier > current_tier else 'demotion'} — trust score: {trust_score}",
                trust_score,
            ))

            # Fire webhook
            from app.tasks.webhooks import dispatch_webhook
            dispatch_webhook.delay(str(tid), "worker.promoted", {
                "worker_id":  str(wid),
                "from_tier":  current_tier,
                "to_tier":    new_tier,
                "trust_score": trust_score,
            })

        conn.commit()
        return {"updated": True, "trust_score": trust_score, "tier": new_tier}

    except Exception as e:
        conn.rollback()
        raise
    finally:
        conn.close()


# ─── Bulk recalculation (all active workers) ──────────────────────────────────

@celery_app.task(name="ghostless.recalculate_all_scores")
def recalculate_all_scores():
    """
    Fan-out task: fetch all active worker IDs and enqueue individual recalc tasks.
    Called every 15 minutes by Celery Beat.
    """
    conn = _get_sync_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT id, tenant_id FROM workers WHERE status = 'active'")
        workers = cur.fetchall()
    finally:
        conn.close()

    for worker_id, tenant_id in workers:
        recalculate_worker_score.delay(str(worker_id), str(tenant_id))

    return {"queued": len(workers)}
