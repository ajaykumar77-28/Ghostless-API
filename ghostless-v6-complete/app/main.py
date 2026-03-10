"""
Ghostless API — Main Application

Production-ready FastAPI app.
All routes, middleware, startup/shutdown, health checks.
"""
import structlog
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import JSONResponse
from prometheus_fastapi_instrumentator import Instrumentator

from app.config import settings
from app.database import check_db_connection
from app.routers import validate, scoring, earnings, hub, tenants
from app.routers import admin as admin_router

log = structlog.get_logger()


# ─── Lifespan (startup / shutdown) ───────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    log.info("ghostless_api_starting", version=settings.APP_VERSION, env=settings.ENVIRONMENT)

    # Register Celery task modules for beat schedule
    import app.tasks.scoring           # noqa: F401
    import app.tasks.fraud_clustering  # noqa: F401
    import app.tasks.reconciliation    # noqa: F401

    db_ok = await check_db_connection()
    if not db_ok:
        log.error("database_unavailable_on_startup")
    else:
        log.info("database_connected")

    yield  # App is running

    # Shutdown
    log.info("ghostless_api_shutting_down")


# ─── App Factory ─────────────────────────────────────────────────────────────

app = FastAPI(
    title=settings.APP_NAME,
    version=settings.APP_VERSION,
    description="""
## Ghostless API

**Infrastructure for crowdsourcing platforms.**

Solve the biggest problem in crowdsourcing: workers feeling invisible.

### What you get:
- **Validation API** — Pre-validate task submissions in < 20ms
- **Scoring Engine** — Trust scores, accuracy metrics, tier promotions
- **Earnings API** — Real-time payout tracking with streak bonuses
- **Community Hub** — WebSocket chat, bug reports, announcements
- **Webhook System** — Signed event delivery to your platform

### Authentication
All endpoints require:
- `X-Tenant-ID` header (your tenant slug)
- Either `X-API-Key` (server-to-server) or `Authorization: Bearer <jwt>` (worker-facing)

### Base URL
```
https://api.ghostless.io/v1
```
""",
    openapi_url="/openapi.json",
    docs_url="/docs",
    redoc_url="/redoc",
    lifespan=lifespan,
)


# ─── Middleware ───────────────────────────────────────────────────────────────

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["*"],
    expose_headers=["X-Request-ID", "X-Rate-Limit-Remaining"],
)

# Request timing + structured logging
@app.middleware("http")
async def request_logging(request: Request, call_next):
    start = time.perf_counter()
    request_id = request.headers.get("X-Request-ID", f"req_{int(time.time()*1000)}")

    response = await call_next(request)

    duration_ms = int((time.perf_counter() - start) * 1000)
    log.info(
        "http_request",
        method=request.method,
        path=request.url.path,
        status=response.status_code,
        duration_ms=duration_ms,
        request_id=request_id,
    )
    response.headers["X-Request-ID"] = request_id
    response.headers["X-Response-Time"] = f"{duration_ms}ms"
    return response


# Prometheus metrics
Instrumentator().instrument(app).expose(app, endpoint="/metrics")


# ─── Routers ─────────────────────────────────────────────────────────────────

API_PREFIX = "/v1"

app.include_router(validate.router,       prefix=API_PREFIX)
app.include_router(scoring.router,        prefix=API_PREFIX)
app.include_router(earnings.router,       prefix=API_PREFIX)
app.include_router(hub.router,            prefix=API_PREFIX)
app.include_router(tenants.router,        prefix=API_PREFIX)
app.include_router(admin_router.router,   prefix=API_PREFIX)  # FIX #15


# ─── Health & Status ─────────────────────────────────────────────────────────

@app.get("/health", tags=["System"], summary="Health check")
async def health_check():
    db_ok = await check_db_connection()
    return {
        "status":   "ok" if db_ok else "degraded",
        "version":  settings.APP_VERSION,
        "database": "connected" if db_ok else "unavailable",
        "ts":       int(time.time()),
    }


@app.get("/", tags=["System"], include_in_schema=False)
async def root():
    return {
        "api":        settings.APP_NAME,
        "version":    settings.APP_VERSION,
        "docs":       "/docs",
        "health":     "/health",
        "openapi":    "/openapi.json",
    }


# ─── Global Exception Handlers ───────────────────────────────────────────────

@app.exception_handler(404)
async def not_found_handler(request: Request, exc):
    return JSONResponse(
        status_code=404,
        content={"error": "not_found", "path": str(request.url.path)},
    )


@app.exception_handler(500)
async def server_error_handler(request: Request, exc):
    log.error("unhandled_server_error", path=str(request.url.path), error=str(exc))
    return JSONResponse(
        status_code=500,
        content={"error": "internal_server_error", "message": "Something went wrong. Request ID: " + request.headers.get("X-Request-ID", "unknown")},
    )
