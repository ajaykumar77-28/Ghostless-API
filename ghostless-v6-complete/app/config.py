"""
Ghostless API — Configuration (v5)

All settings are environment-variable overridable via .env.
Per-tenant overrides are stored in DB (TenantConfig) and hot-reloaded
by the config service — see services/tenant_config.py.
"""
from pydantic_settings import BaseSettings
from typing import List
import secrets


class Settings(BaseSettings):
    # ── App ──────────────────────────────────────────────────────────────
    APP_NAME: str = "Ghostless API"
    APP_VERSION: str = "5.0.0"
    DEBUG: bool = False
    ENVIRONMENT: str = "production"

    # ── Security ─────────────────────────────────────────────────────────
    SECRET_KEY: str = secrets.token_urlsafe(32)
    JWT_ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60
    REFRESH_TOKEN_EXPIRE_DAYS: int = 30
    API_KEY_PREFIX: str = "sk_live_gl_"
    # HMAC request signature validation (optional)
    HMAC_SIGNATURE_REQUIRED: bool = False
    HMAC_TOLERANCE_SECONDS: int = 300      # replay attack window

    # ── Database ──────────────────────────────────────────────────────────
    DATABASE_URL: str = "postgresql+asyncpg://ghost:ghost@localhost:5432/ghostless"
    DATABASE_POOL_SIZE: int = 20
    DATABASE_MAX_OVERFLOW: int = 10
    # Archival: tasks older than this many days are moved to tasks_archive
    ARCHIVAL_CUTOFF_DAYS: int = 365

    # ── Redis ─────────────────────────────────────────────────────────────
    REDIS_URL: str = "redis://localhost:6379/0"
    CACHE_TTL_WORKER_HISTORY: int = 300
    CACHE_TTL_TENANT_CONFIG: int = 60
    DEDUP_WINDOW_SECONDS: int = 600
    # Redis failure fallback: use degraded-mode scoring (no z-score, no baseline)
    REDIS_FALLBACK_ENABLED: bool = True
    REDIS_CONNECT_TIMEOUT: float = 1.0

    # ── Celery ────────────────────────────────────────────────────────────
    CELERY_BROKER_URL: str = "redis://localhost:6379/0"
    CELERY_RESULT_BACKEND: str = "redis://localhost:6379/1"
    SCORE_RECALC_INTERVAL_MINUTES: int = 15
    CELERY_TASK_MAX_RETRIES: int = 3
    CELERY_RETRY_BACKOFF_BASE: int = 60    # seconds

    # ── Rate Limits ───────────────────────────────────────────────────────
    RATE_LIMIT_STARTER: int = 100
    RATE_LIMIT_GROWTH: int = 1000
    RATE_LIMIT_ENTERPRISE: int = 10000

    # ── Validation ────────────────────────────────────────────────────────
    MIN_QUALITY_SCORE_TO_SUBMIT: float = 0.30
    SPEED_FLAG_THRESHOLD_PCT: float = 0.15
    SPEED_WARNING_THRESHOLD_PCT: float = 0.35

    # ── Velocity Limits ───────────────────────────────────────────────────
    MAX_TASKS_PER_MINUTE: int = 3
    MAX_TASKS_PER_HOUR: int = 60
    MAX_TASKS_PER_DAY: int = 300
    MAX_WORKERS_PER_IP: int = 5
    MAX_COORD_WORKERS: int = 5          # coordinated-attack detection threshold

    # ── Scoring Weights ───────────────────────────────────────────────────
    WEIGHT_ACCURACY_30D: float = 0.30
    WEIGHT_AVG_QUALITY: float = 0.25
    WEIGHT_SPEED_PENALTY_MAX: float = 20.0
    WEIGHT_TENURE: float = 0.10
    WEIGHT_STREAK: float = 0.15
    # Velocity penalty contribution to trust score (max deduction points)
    VELOCITY_TRUST_PENALTY_MAX: float = 15.0
    # Minimum peers needed before confidence can exceed this threshold
    MIN_PEERS_FOR_CONFIDENCE_50: int = 10   # need 10 peers before conf > 0.5
    MIN_PEERS_FOR_CONFIDENCE_80: int = 30   # need 30 peers before conf > 0.8
    # Cap on how much a single task can move EWMA (prevents trivial task spam)
    MAX_EWMA_DELTA_PER_TASK: float = 0.05
    # Accuracy history decay: half-life in days (older accepted tasks count less)
    ACCURACY_HALFLIFE_DAYS: float = 60.0
    # Task difficulty: higher difficulty weight = more trust impact per task
    DEFAULT_TASK_DIFFICULTY: float = 1.0
    # Baseline drift detection: alert if mean shifts by this many std devs
    BASELINE_DRIFT_THRESHOLD: float = 2.0

    # ── Promotion Thresholds ─────────────────────────────────────────────
    SILVER_MIN_TASKS: int = 50
    SILVER_MIN_TRUST: float = 50.0
    GOLD_MIN_TASKS: int = 200
    GOLD_MIN_TRUST: float = 70.0
    ELITE_MIN_TASKS: int = 500
    ELITE_MIN_TRUST: float = 85.0

    # ── Earnings / Payout ────────────────────────────────────────────────
    MAX_STREAK_BONUS_DAYS: int = 30
    STREAK_BONUS_PER_DAY: float = 0.002
    PAYOUT_DAY_OF_WEEK: int = 4
    DEFAULT_CURRENCY: str = "USD"
    SUPPORTED_CURRENCIES: List[str] = ["USD", "EUR", "GBP", "CAD", "AUD"]
    NEGATIVE_BALANCE_LIMIT: float = 0.0    # workers cannot go below 0 by default

    # ── Fraud Thresholds (defaults; overridable per-tenant) ──────────────
    FRAUD_SHADOW_BAN_ENABLED: bool = True
    FRAUD_PROGRESSIVE_STEP1_EVENTS: int = 1    # 1st event → warning
    FRAUD_PROGRESSIVE_STEP2_EVENTS: int = 3    # 3rd event → lock (shadow-ban)
    FRAUD_PROGRESSIVE_STEP3_EVENTS: int = 7    # 7th event → ban
    # Long-horizon coordination: look back this many days for clustering
    FRAUD_CLUSTER_LOOKBACK_DAYS: int = 7
    FRAUD_CLUSTER_MIN_WORKERS: int = 3
    FRAUD_HALFLIFE_DAYS: float = 90.0
    POST_FRAUD_MAX_TRUST: float = 70.0

    # ── Webhooks ─────────────────────────────────────────────────────────
    WEBHOOK_TIMEOUT_SECONDS: int = 10
    WEBHOOK_MAX_RETRIES: int = 5
    WEBHOOK_RETRY_DELAY_SECONDS: int = 60
    WEBHOOK_SIGNATURE_REQUIRED: bool = True

    # ── CORS ─────────────────────────────────────────────────────────────
    CORS_ORIGINS: List[str] = ["*"]

    class Config:
        env_file = ".env"
        case_sensitive = True


settings = Settings()
