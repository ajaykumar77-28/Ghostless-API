"""
Ghostless API — Configuration
Load from environment variables or .env file
"""
from pydantic_settings import BaseSettings
from typing import List
import secrets


class Settings(BaseSettings):
    # ── App ──────────────────────────────────────────────────────────────
    APP_NAME: str = "Ghostless API"
    APP_VERSION: str = "1.0.0"
    DEBUG: bool = False
    ENVIRONMENT: str = "production"  # development | staging | production

    # ── Security ─────────────────────────────────────────────────────────
    SECRET_KEY: str = secrets.token_urlsafe(32)
    JWT_ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60
    API_KEY_PREFIX: str = "sk_live_gl_"

    # ── Database ──────────────────────────────────────────────────────────
    DATABASE_URL: str = "postgresql+asyncpg://ghost:ghost@localhost:5432/ghostless"
    DATABASE_POOL_SIZE: int = 20
    DATABASE_MAX_OVERFLOW: int = 10

    # ── Redis ─────────────────────────────────────────────────────────────
    REDIS_URL: str = "redis://localhost:6379/0"
    CACHE_TTL_WORKER_HISTORY: int = 300      # 5 minutes
    CACHE_TTL_TENANT_CONFIG: int = 60        # 1 minute
    DEDUP_WINDOW_SECONDS: int = 600          # 10 minute dedup window

    # ── Celery ────────────────────────────────────────────────────────────
    CELERY_BROKER_URL: str = "redis://localhost:6379/0"
    CELERY_RESULT_BACKEND: str = "redis://localhost:6379/1"
    SCORE_RECALC_INTERVAL_MINUTES: int = 15

    # ── Rate Limits (requests/minute per tenant tier) ─────────────────────
    RATE_LIMIT_STARTER: int = 100
    RATE_LIMIT_GROWTH: int = 1000
    RATE_LIMIT_ENTERPRISE: int = 10000

    # ── Validation ────────────────────────────────────────────────────────
    MIN_QUALITY_SCORE_TO_SUBMIT: float = 0.30
    SPEED_FLAG_THRESHOLD_PCT: float = 0.15    # < 15% of avg time = error
    SPEED_WARNING_THRESHOLD_PCT: float = 0.35 # < 35% of avg time = warning

    # ── Scoring Weights ──────────────────────────────────────────────────
    WEIGHT_ACCURACY_30D: float = 0.30
    WEIGHT_AVG_QUALITY: float = 0.25
    WEIGHT_SPEED_PENALTY_MAX: float = 20.0
    WEIGHT_TENURE: float = 0.10
    WEIGHT_STREAK: float = 0.15

    # ── Promotion Thresholds ─────────────────────────────────────────────
    SILVER_MIN_TASKS: int = 50
    SILVER_MIN_TRUST: float = 50.0
    GOLD_MIN_TASKS: int = 200
    GOLD_MIN_TRUST: float = 70.0
    ELITE_MIN_TASKS: int = 500
    ELITE_MIN_TRUST: float = 85.0

    # ── Earnings ─────────────────────────────────────────────────────────
    MAX_STREAK_BONUS_DAYS: int = 30
    STREAK_BONUS_PER_DAY: float = 0.002   # +0.2% per day, max +6%
    PAYOUT_DAY_OF_WEEK: int = 4           # Friday = 4

    # ── Webhooks ─────────────────────────────────────────────────────────
    WEBHOOK_TIMEOUT_SECONDS: int = 10
    WEBHOOK_MAX_RETRIES: int = 5
    WEBHOOK_RETRY_DELAY_SECONDS: int = 60

    # ── CORS ─────────────────────────────────────────────────────────────
    CORS_ORIGINS: List[str] = ["*"]

    class Config:
        env_file = ".env"
        case_sensitive = True


settings = Settings()
