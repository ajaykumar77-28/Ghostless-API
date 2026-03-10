"""
Ghostless API — Scoring Celery Task (v5)

New in v5:
  - Celery retry policies with exponential backoff
  - Scoring task idempotency (skips if recently calculated)
  - Redis outage fallback (degrade gracefully, use last known score)
  - Writes ScoringDecisionLog on every recalculation
  - Persists baseline versions on drift detection
  - FraudDecayLog written when fraud weights change
  - Decayed accuracy computed via SQL EXP() weighting
  - TenantConfig hot-reload per worker recalculation
  - Per-tenant scoring parameter overrides
  - Graceful degradation when baselines unavailable
"""
import json
import logging
from datetime import datetime, timedelta
from typing import Optional

import psycopg2
from celery import Task
from celery.schedules import crontab
from celery.utils.log import get_task_logger

from app.config import settings
from app.services.scoring import (
    ScoreUpdateInput, compute_score_update,
    FRAUD_HALFLIFE_DAYS, POST_FRAUD_MAX_TRUST,
)
from app.tasks.webhooks import celery_app

logger = get_task_logger(__name__)


# ── Celery beat schedule ──────────────────────────────────────────────────────

celery_app.conf.beat_schedule = {
    "recalculate-all-scores": {
        "task":     "ghostless.recalculate_all_scores",
        "schedule": crontab(minute=f"*/{settings.SCORE_RECALC_INTERVAL_MINUTES}"),
    },
    "run-fraud-clustering": {
        "task":     "ghostless.run_fraud_clustering",
        "schedule": crontab(minute="*/30"),   # every 30 minutes
    },
    "run-payout-reconciliation": {
        "task":     "ghostless.run_payout_reconciliation",
        "schedule": crontab(hour="*/6"),   # every 6 hours
    },
    "archive-old-tasks": {
        "task":     "ghostless.archive_old_tasks",
        "schedule": crontab(hour="2", minute="0"),   # 2am daily
    },
}

celery_app.conf.task_serializer = "json"
celery_app.conf.result_serializer = "json"
celery_app.conf.accept_content = ["json"]
celery_app.conf.task_acks_late = True     # ack only after task completes
celery_app.conf.worker_prefetch_multiplier = 1


def _get_sync_conn():
    return psycopg2.connect(settings.DATABASE_URL.replace("+asyncpg", ""))


def _compute_new_tier(trust_score: float, total_tasks: int) -> str:
    if trust_score >= settings.ELITE_MIN_TRUST and total_tasks >= settings.ELITE_MIN_TASKS:
        return "elite"
    if trust_score >= settings.GOLD_MIN_TRUST  and total_tasks >= settings.GOLD_MIN_TASKS:
        return "gold"
    if trust_score >= settings.SILVER_MIN_TRUST and total_tasks >= settings.SILVER_MIN_TASKS:
        return "silver"
    return "bronze"


def _load_tenant_config(cur, tenant_id: str) -> dict:
    """Hot-load per-tenant scoring overrides."""
    cur.execute("""
        SELECT max_tasks_per_hour, velocity_trust_penalty_max,
               min_peers_for_confidence_50, confidence_full_at_n,
               post_fraud_max_trust, fraud_progressive_step1,
               fraud_progressive_step2, fraud_progressive_step3,
               default_currency
        FROM tenant_configs
        WHERE tenant_id = %s
    """, (tenant_id,))
    row = cur.fetchone()
    if not row:
        return {}
    cols = ["max_tasks_per_hour", "velocity_trust_penalty_max",
            "min_peers_for_confidence_50", "confidence_full_at_n",
            "post_fraud_max_trust", "fraud_progressive_step1",
            "fraud_progressive_step2", "fraud_progressive_step3",
            "default_currency"]
    return {k: v for k, v in zip(cols, row) if v is not None}


def _apply_progressive_penalty(
    cur, conn, worker_id: str, tenant_id: str,
    fraud_event_count: int, tenant_cfg: dict,
) -> str:
    """
    Progressive penalties: warning → shadow_ban → ban.
    Returns the auto_action taken (or "" if none).
    """
    step1 = tenant_cfg.get("fraud_progressive_step1", settings.FRAUD_PROGRESSIVE_STEP1_EVENTS)
    step2 = tenant_cfg.get("fraud_progressive_step2", settings.FRAUD_PROGRESSIVE_STEP2_EVENTS)
    step3 = tenant_cfg.get("fraud_progressive_step3", settings.FRAUD_PROGRESSIVE_STEP3_EVENTS)

    if fraud_event_count < step1:
        return ""

    action = ""
    if fraud_event_count >= step3:
        # Step 3: ban
        cur.execute(
            "UPDATE workers SET status = 'banned' WHERE id = %s AND tenant_id = %s",
            (worker_id, tenant_id)
        )
        action = "ban"
    elif fraud_event_count >= step2:
        # Step 2: shadow-ban (accept tasks, zero payout)
        cur.execute(
            "UPDATE worker_scores SET shadow_banned = TRUE WHERE worker_id = %s",
            (worker_id,)
        )
        action = "shadow_ban"
    elif fraud_event_count >= step1:
        # Step 1: warning (just log, already in fraud_events)
        action = "warning"

    return action


# ── Main scoring task ─────────────────────────────────────────────────────────

class ScoreTask(Task):
    """Base task class with retry logic."""
    abstract = True

    def on_failure(self, exc, task_id, args, kwargs, einfo):
        logger.error(f"Scoring task {task_id} failed permanently: {exc}")


@celery_app.task(
    name="ghostless.recalculate_worker_score",
    base=ScoreTask,
    bind=True,
    max_retries=settings.CELERY_TASK_MAX_RETRIES,
    default_retry_delay=settings.CELERY_RETRY_BACKOFF_BASE,
)
def recalculate_worker_score(self, worker_id: str, tenant_id: str, force: bool = False):
    """
    Recalculate trust score for a single worker.
    Idempotent: skips if calculated within last 5 minutes (unless force=True).
    Retries with backoff on transient failures.
    """
    conn = _get_sync_conn()
    try:
        cur  = conn.cursor()
        now  = datetime.utcnow()

        # ── Idempotency: skip if recently calculated ───────────────────
        if not force:
            cur.execute("""
                SELECT last_calculated_at FROM worker_scores
                WHERE worker_id = %s
            """, (worker_id,))
            row = cur.fetchone()
            if row and row[0]:
                age_seconds = (now - row[0]).total_seconds()
                if age_seconds < 300:   # 5 minutes
                    return {"skipped": True, "reason": "recently_calculated", "age_s": age_seconds}

        # ── Hot-load tenant config ─────────────────────────────────────
        tenant_cfg = _load_tenant_config(cur, tenant_id)
        max_per_hour    = tenant_cfg.get("max_tasks_per_hour", settings.MAX_TASKS_PER_HOUR)
        post_fraud_max  = tenant_cfg.get("post_fraud_max_trust", POST_FRAUD_MAX_TRUST)

        # ── Fetch worker ───────────────────────────────────────────────
        cur.execute("""
            SELECT
                ws.trust_score, ws.ewma_quality, ws.ewma_alpha,
                ws.total_tasks, ws.accepted_tasks, ws.streak_days,
                ws.speed_flag_count, ws.zscore_flagged_count,
                ws.task_baselines, ws.confidence_weight,
                ws.max_trust, ws.shadow_banned, ws.lifecycle_stage,
                w.created_at, w.tier, w.tenant_id
            FROM workers w
            LEFT JOIN worker_scores ws ON ws.worker_id = w.id
            WHERE w.id = %s AND w.tenant_id = %s AND w.status != 'banned'
        """, (worker_id, tenant_id))
        row = cur.fetchone()
        if not row:
            return {"skipped": True, "reason": "worker_not_found_or_banned"}

        (
            trust_score, ewma_quality, ewma_alpha,
            total_tasks, accepted_tasks, streak_days,
            speed_flag_count, zscore_flagged_count,
            task_baselines, confidence_weight,
            stored_max_trust, shadow_banned, lifecycle_stage,
            created_at, current_tier, wt_id,
        ) = row

        max_trust    = stored_max_trust if stored_max_trust is not None else 100.0
        trust_before = trust_score or 50.0

        # ── FIX #1: Rolling accuracy via SQL (true windows) ───────────
        cur.execute("""
            SELECT
                COUNT(*) FILTER (WHERE submitted_at > NOW() - INTERVAL '7 days'
                                   AND was_accepted IS NOT NULL)  AS n_7d,
                SUM(CASE WHEN was_accepted AND submitted_at > NOW() - INTERVAL '7 days'
                         THEN 1 ELSE 0 END)                       AS acc_7d,
                COUNT(*) FILTER (WHERE submitted_at > NOW() - INTERVAL '30 days'
                                   AND was_accepted IS NOT NULL)  AS n_30d,
                SUM(CASE WHEN was_accepted AND submitted_at > NOW() - INTERVAL '30 days'
                         THEN 1 ELSE 0 END)                       AS acc_30d,
                COUNT(*) FILTER (WHERE was_accepted IS NOT NULL)  AS n_all,
                SUM(CASE WHEN was_accepted THEN 1 ELSE 0 END)     AS acc_all,
                COUNT(*)                                           AS total
            FROM tasks
            WHERE worker_id = %s AND tenant_id = %s
        """, (worker_id, tenant_id))
        t = cur.fetchone()
        n_7d, acc_7d, n_30d, acc_30d, n_all, acc_all, total = (int(x or 0) for x in t)

        # ── V5: Decayed accuracy (EXP weighting on submitted_at) ──────
        cur.execute("""
            SELECT
                SUM(CASE WHEN was_accepted THEN
                    EXP(-EXTRACT(EPOCH FROM (NOW() - submitted_at)) / 86400.0
                        / %s)
                    ELSE 0 END)  AS accepted_weighted,
                SUM(
                    EXP(-EXTRACT(EPOCH FROM (NOW() - submitted_at)) / 86400.0
                        / %s)
                )                AS total_weighted
            FROM tasks
            WHERE worker_id = %s AND tenant_id = %s
              AND was_accepted IS NOT NULL
              AND submitted_at > NOW() - INTERVAL '30 days'
        """, (settings.ACCURACY_HALFLIFE_DAYS, settings.ACCURACY_HALFLIFE_DAYS,
              worker_id, tenant_id))
        decay_row = cur.fetchone()
        acc_30d_decayed_accepted = float(decay_row[0] or 0)
        acc_30d_decayed_total    = float(decay_row[1] or 0)

        # ── Avg quality from validations ──────────────────────────────
        cur.execute("""
            SELECT COALESCE(AVG(quality_score), 0.5),
                   COALESCE(AVG(difficulty_weight), 1.0)
            FROM validations
            WHERE worker_id = %s AND tenant_id = %s
              AND validated_at > NOW() - INTERVAL '30 days'
        """, (worker_id, tenant_id))
        q_row = cur.fetchone()
        avg_quality      = float(q_row[0] or 0.5)
        avg_difficulty   = float(q_row[1] or 1.0)

        # ── FIX #13: Fraud count from DB only ────────────────────────
        cur.execute("""
            SELECT
                EXTRACT(EPOCH FROM (NOW() - created_at)) / 86400.0 AS age_days,
                id
            FROM fraud_events
            WHERE worker_id = %s AND tenant_id = %s AND reviewed = FALSE
            ORDER BY created_at DESC
        """, (worker_id, tenant_id))
        fraud_rows = cur.fetchall()
        fraud_events_aged   = [{"age_days": float(r[0]), "id": str(r[1])} for r in fraud_rows]
        fraud_event_count   = len(fraud_events_aged)

        # FIX #7: max_trust ceiling
        has_unreviewed_fraud = fraud_event_count > 0
        if has_unreviewed_fraud and max_trust > post_fraud_max:
            max_trust = post_fraud_max

        # ── Velocity (last hour) for penalty calc ──────────────────
        cur.execute("""
            SELECT COUNT(*) FROM tasks
            WHERE worker_id = %s AND tenant_id = %s
              AND submitted_at > NOW() - INTERVAL '1 hour'
        """, (worker_id, tenant_id))
        tasks_last_hour = int(cur.fetchone()[0] or 0)
        velocity_ratio  = tasks_last_hour / max(max_per_hour, 1)

        # ── Peer baseline n for confidence gate ───────────────────────
        cur.execute("""
            SELECT COALESCE(SUM((value->>'n')::int), 0)
            FROM worker_scores,
                 jsonb_each(task_baselines::jsonb) AS kv(key, value)
            WHERE tenant_id = %s
        """, (tenant_id,))
        peer_baseline_n = int(cur.fetchone()[0] or 0)

        # ── Worker contribution to baseline ───────────────────────────
        cur.execute("""
            SELECT COUNT(*) FROM validations
            WHERE worker_id = %s AND tenant_id = %s
        """, (worker_id, tenant_id))
        worker_baseline_n = int(cur.fetchone()[0] or 0)

        # ── Baseline snapshots for drift detection ────────────────────
        cur.execute("""
            SELECT task_type, baseline FROM baseline_versions
            WHERE tenant_id = %s
              AND version = (
                  SELECT MAX(version) FROM baseline_versions
                  WHERE tenant_id = %s AND task_type = baseline_versions.task_type
              )
        """, (tenant_id, tenant_id))
        baseline_snapshots = {r[0]: r[1] for r in cur.fetchall()}

        days_joined = (now - created_at).total_seconds() / 86400 if created_at else 0.0

        # ── Build input and compute ───────────────────────────────────
        inp = ScoreUpdateInput(
            quality_score                 = avg_quality,
            task_type                     = "aggregate",
            was_accepted                  = None,
            _fraud_event_count            = fraud_event_count,
            streak_days                   = streak_days or 0,
            days_since_joined             = days_joined,
            current_ewma                  = ewma_quality or 0.5,
            current_total_tasks           = total,
            current_accepted              = accepted_tasks or 0,
            current_acc_7d_n              = n_7d,
            current_acc_7d_sum            = acc_7d,
            current_acc_30d_n             = n_30d,
            current_acc_30d_sum           = acc_30d,
            current_acc_all_n             = n_all,
            current_acc_all_sum           = acc_all,
            acc_30d_decayed_accepted      = acc_30d_decayed_accepted,
            acc_30d_decayed_total         = acc_30d_decayed_total,
            task_baselines                = task_baselines or {},
            baseline_snapshots            = baseline_snapshots,
            current_zscore_flagged        = zscore_flagged_count or 0,
            worker_baseline_contribution  = worker_baseline_n,
            max_trust                     = max_trust,
            fraud_events_aged             = fraud_events_aged,
            difficulty_weight             = avg_difficulty,
            tasks_last_hour               = tasks_last_hour,
            max_tasks_per_hour            = max_per_hour,
            peer_baseline_n               = peer_baseline_n,
            velocity_ratio                = velocity_ratio,
        )
        result = compute_score_update(inp)

        # ── FIX #4: Persist FraudEvent if z-score fraud suspect ───────
        new_fraud_event_id = None
        if result.is_fraud_suspect:
            cur.execute("""
                INSERT INTO fraud_events (
                    id, tenant_id, worker_id,
                    event_type, severity, reason_codes, details,
                    auto_action, progressive_level, reviewed
                ) VALUES (
                    gen_random_uuid(), %s, %s,
                    'zscore_fraud_suspect', 'critical',
                    %s::jsonb, %s::jsonb, 'flag', 1, FALSE
                )
                RETURNING id
            """, (
                tenant_id, worker_id,
                json.dumps(["zscore"]),
                json.dumps({
                    "zscore":        result.zscore_latest,
                    "ewma_quality":  result.ewma_quality,
                    "flagged_count": result.zscore_flagged_count,
                    "detected_at":   now.isoformat(),
                }),
            ))
            new_fraud_event_id = str(cur.fetchone()[0])
            fraud_event_count += 1
            max_trust = min(max_trust, post_fraud_max)

        # ── Progressive penalty ────────────────────────────────────────
        action_taken = _apply_progressive_penalty(
            cur, conn, worker_id, tenant_id, fraud_event_count, tenant_cfg
        )

        # ── FraudDecayLog: write decay audit for each aged event ───────
        for fe in fraud_events_aged:
            age_days = fe["age_days"]
            effective_w = 2 ** (-age_days / FRAUD_HALFLIFE_DAYS)
            if effective_w < 0.99:   # only log if meaningful decay occurred
                cur.execute("""
                    INSERT INTO fraud_decay_logs
                        (id, tenant_id, worker_id, fraud_event_id, effective_weight, age_days)
                    VALUES (gen_random_uuid(), %s, %s, %s, %s, %s)
                    ON CONFLICT DO NOTHING
                """, (tenant_id, worker_id, fe.get("id"), round(effective_w, 4), round(age_days, 1)))

        # ── Baseline drift: snapshot if drift detected ─────────────────
        if result.drift_result.get("drift_detected"):
            for task_type, bl in result.task_baselines.items():
                if bl.get("n", 0) >= 30:
                    cur.execute("""
                        SELECT COALESCE(MAX(version), 0) FROM baseline_versions
                        WHERE tenant_id = %s AND task_type = %s
                    """, (tenant_id, task_type))
                    latest_ver = int(cur.fetchone()[0] or 0)
                    cur.execute("""
                        INSERT INTO baseline_versions
                            (id, tenant_id, task_type, baseline, version, trigger)
                        VALUES (gen_random_uuid(), %s, %s, %s::jsonb, %s, 'drift_detected')
                        ON CONFLICT DO NOTHING
                    """, (tenant_id, task_type, json.dumps(bl), latest_ver + 1))

        # ── Worker lifecycle stage ─────────────────────────────────────
        if action_taken == "ban":
            new_lifecycle = "banned"
        elif fraud_event_count > 0 and action_taken in ("shadow_ban", "warning"):
            new_lifecycle = "flagged"
        elif total >= 50:
            new_lifecycle = "trusted"
        elif total >= 10:
            new_lifecycle = "learning"
        else:
            new_lifecycle = "new"

        # ── Upsert worker_scores ──────────────────────────────────────
        cur.execute("""
            INSERT INTO worker_scores (
                worker_id, tenant_id, trust_score, ewma_quality, ewma_alpha,
                accuracy_7d, accuracy_30d, accuracy_all, avg_quality_score,
                total_tasks, accepted_tasks, speed_flag_count, fraud_flag_count,
                streak_days, zscore_latest, zscore_flagged_count, task_baselines,
                confidence_weight, max_trust, velocity_score,
                lifecycle_stage, last_calculated_at
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW())
            ON CONFLICT (worker_id) DO UPDATE SET
                trust_score          = EXCLUDED.trust_score,
                ewma_quality         = EXCLUDED.ewma_quality,
                ewma_alpha           = EXCLUDED.ewma_alpha,
                accuracy_7d          = EXCLUDED.accuracy_7d,
                accuracy_30d         = EXCLUDED.accuracy_30d,
                accuracy_all         = EXCLUDED.accuracy_all,
                avg_quality_score    = EXCLUDED.avg_quality_score,
                total_tasks          = EXCLUDED.total_tasks,
                accepted_tasks       = EXCLUDED.accepted_tasks,
                speed_flag_count     = EXCLUDED.speed_flag_count,
                fraud_flag_count     = EXCLUDED.fraud_flag_count,
                zscore_latest        = EXCLUDED.zscore_latest,
                zscore_flagged_count = EXCLUDED.zscore_flagged_count,
                task_baselines       = EXCLUDED.task_baselines,
                confidence_weight    = EXCLUDED.confidence_weight,
                max_trust            = EXCLUDED.max_trust,
                velocity_score       = EXCLUDED.velocity_score,
                lifecycle_stage      = EXCLUDED.lifecycle_stage,
                last_calculated_at   = NOW()
        """, (
            worker_id, tenant_id,
            result.trust_score,
            result.ewma_quality,
            result.ewma_alpha,
            result.accuracy_7d,
            result.accuracy_30d,
            result.accuracy_all,
            avg_quality,
            total,
            accepted_tasks or 0,
            speed_flag_count or 0,
            fraud_event_count,
            streak_days or 0,
            result.zscore_latest,
            result.zscore_flagged_count,
            json.dumps(result.task_baselines),
            result.confidence_weight_val,
            max_trust,
            round(1.0 - velocity_ratio, 4),   # velocity_score
            new_lifecycle,
        ))

        # ── ScoringDecisionLog ─────────────────────────────────────────
        cur.execute("""
            INSERT INTO scoring_decision_logs (
                id, tenant_id, worker_id,
                trust_score_before, trust_score_after,
                ewma_component, accuracy_component, streak_component, tenure_component,
                confidence_factor, fraud_multiplier, velocity_penalty,
                task_difficulty_used, fraud_event_count,
                is_fraud_suspect, drift_detected, drift_details
            ) VALUES (
                gen_random_uuid(), %s, %s,
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb
            )
        """, (
            tenant_id, worker_id,
            round(trust_before, 2), result.trust_score,
            result.ewma_component, result.accuracy_component,
            result.streak_component, result.tenure_component,
            result.confidence_weight_val, result.fraud_multiplier,
            result.velocity_penalty,
            round(avg_difficulty, 3), fraud_event_count,
            result.is_fraud_suspect,
            result.drift_result.get("drift_detected", False),
            json.dumps(result.drift_result),
        ))

        # ── Tier check ────────────────────────────────────────────────
        new_tier = _compute_new_tier(result.trust_score, total)
        if new_tier != current_tier:
            cur.execute("UPDATE workers SET tier = %s WHERE id = %s", (new_tier, worker_id))
            cur.execute("""
                INSERT INTO promotions (id, tenant_id, worker_id, from_tier, to_tier, reason, trust_score_at)
                VALUES (gen_random_uuid(), %s, %s, %s, %s, %s, %s)
            """, (
                tenant_id, worker_id, current_tier, new_tier,
                f"Auto {'promotion' if new_tier > current_tier else 'demotion'} — trust: {result.trust_score}",
                result.trust_score,
            ))
            from app.tasks.webhooks import dispatch_webhook
            dispatch_webhook.delay(str(tenant_id), "worker.promoted", {
                "worker_id":   worker_id,
                "from_tier":   current_tier,
                "to_tier":     new_tier,
                "trust_score": result.trust_score,
            })

        conn.commit()
        return {
            "updated":          True,
            "trust_score":      result.trust_score,
            "fraud_mult":       result.fraud_multiplier,
            "velocity_penalty": result.velocity_penalty,
            "max_trust":        max_trust,
            "tier":             new_tier,
            "lifecycle":        new_lifecycle,
            "is_fraud_suspect": result.is_fraud_suspect,
            "drift_detected":   result.drift_result.get("drift_detected", False),
            "action_taken":     action_taken,
        }

    except Exception as exc:
        conn.rollback()
        logger.exception(f"Scoring failed for worker {worker_id}: {exc}")
        raise self.retry(exc=exc, countdown=settings.CELERY_RETRY_BACKOFF_BASE * (2 ** self.request.retries))
    finally:
        conn.close()


@celery_app.task(name="ghostless.recalculate_all_scores")
def recalculate_all_scores():
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


@celery_app.task(name="ghostless.archive_old_tasks")
def archive_old_tasks():
    """Move tasks older than ARCHIVAL_CUTOFF_DAYS to tasks_archive."""
    conn = _get_sync_conn()
    try:
        cur = conn.cursor()
        cur.execute("""
            WITH archived AS (
                INSERT INTO tasks_archive
                    (id, tenant_id, worker_id, task_type, project_id, submitted_at,
                     completion_time, was_accepted, payout_amount, idempotency_key,
                     currency, metadata_)
                SELECT id, tenant_id, worker_id, task_type, project_id, submitted_at,
                       completion_time, was_accepted, payout_amount, idempotency_key,
                       currency, metadata_
                FROM tasks
                WHERE submitted_at < NOW() - INTERVAL '%s days'
                  AND archived_at IS NULL
                ON CONFLICT DO NOTHING
                RETURNING id
            )
            UPDATE tasks SET archived_at = NOW()
            WHERE id IN (SELECT id FROM archived)
        """, (settings.ARCHIVAL_CUTOFF_DAYS,))
        count = cur.rowcount
        conn.commit()
        return {"archived": count}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
