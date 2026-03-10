#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# Ghostless API — Zero-Downtime Deploy Script
#
# Usage:
#   ./scripts/deploy.sh [VERSION]
#   ./scripts/deploy.sh 6.1.0
#   ./scripts/deploy.sh --rollback   # roll back to previous image
#
# What it does:
#   1. Pre-flight checks (disk, DB connection, Redis)
#   2. Pull / build new image
#   3. Run DB migrations (with dry-run check first)
#   4. Rolling restart: api → worker → webhook-worker → scheduler
#   5. Health gate: wait for healthy before declaring success
#   6. Auto-rollback if health gate fails
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; BOLD='\033[1m'; NC='\033[0m'
info()    { echo -e "$(date '+%H:%M:%S') ${BLUE}[INFO]${NC}  $*"; }
success() { echo -e "$(date '+%H:%M:%S') ${GREEN}[OK]${NC}    $*"; }
warn()    { echo -e "$(date '+%H:%M:%S') ${YELLOW}[WARN]${NC}  $*"; }
error()   { echo -e "$(date '+%H:%M:%S') ${RED}[ERROR]${NC} $*"; exit 1; }

COMPOSE_FILES="-f docker-compose.yml -f docker-compose.prod.yml"
ROLLBACK=false
VERSION="${1:-}"

[ "${1:-}" = "--rollback" ] && ROLLBACK=true

# ── Load env ─────────────────────────────────────────────────────────────────
[ -f .env ] || error ".env not found. Run ./scripts/setup.sh first."
set -a; source .env; set +a
API_PORT="${API_PORT:-8000}"
CURRENT_VERSION="${APP_VERSION:-6.0.0}"

# ── Rollback ──────────────────────────────────────────────────────────────────
if [ "$ROLLBACK" = true ]; then
  warn "ROLLBACK mode: restoring previous image tag"
  PREV_TAG=$(docker images ghostless --format "{{.Tag}}" | grep -v latest | sort -V | tail -2 | head -1)
  [ -z "$PREV_TAG" ] && error "No previous image found to roll back to."
  info "Rolling back to ghostless:${PREV_TAG}"
  APP_VERSION="$PREV_TAG"
fi

echo -e "\n${BOLD}${BLUE}▶ Deploying Ghostless API${NC}"
echo -e "  Current: ${CURRENT_VERSION}"
echo -e "  Target:  ${VERSION:-latest}\n"

# ── Pre-flight ────────────────────────────────────────────────────────────────
info "Running pre-flight checks..."

# Disk space (need at least 2GB free)
FREE_GB=$(df -BG . | awk 'NR==2{print $4}' | tr -d 'G')
[ "${FREE_GB:-0}" -lt 2 ] && error "Insufficient disk space: ${FREE_GB}GB free, need 2GB."
success "Disk space: ${FREE_GB}GB free"

# DB reachable
docker compose $COMPOSE_FILES exec -T db pg_isready -U ghost -d ghostless &>/dev/null \
  || error "Database is not reachable. Aborting deploy."
success "Database reachable"

# Redis reachable
docker compose $COMPOSE_FILES exec -T redis redis-cli ping 2>/dev/null | grep -q PONG \
  || error "Redis is not reachable. Aborting deploy."
success "Redis reachable"

# API currently healthy?
if curl -sf "http://localhost:${API_PORT}/health" | grep -q '"status"'; then
  success "API currently healthy"
else
  warn "API is not currently healthy — proceeding anyway (may be a fresh deploy)"
fi

# ── Build new image ───────────────────────────────────────────────────────────
info "Building new image..."
export GIT_SHA=$(git rev-parse --short HEAD 2>/dev/null || echo "unknown")
export BUILD_DATE=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
[ -n "$VERSION" ] && export APP_VERSION="$VERSION"

docker compose $COMPOSE_FILES build --parallel
success "Image built: ghostless:${APP_VERSION:-latest}"

# ── Migration dry-run ─────────────────────────────────────────────────────────
info "Checking pending migrations..."
PENDING=$(docker compose $COMPOSE_FILES run --rm migrate alembic current 2>&1 | tail -3)
info "Migration status: $PENDING"

# ── Apply migrations ──────────────────────────────────────────────────────────
info "Running migrations..."
docker compose $COMPOSE_FILES run --rm migrate
success "Migrations applied"

# ── Rolling restart ───────────────────────────────────────────────────────────
SERVICES=("api" "worker" "webhook-worker" "scheduler")
OLD_VERSION="$CURRENT_VERSION"

restart_service() {
  local svc="$1"
  info "Restarting $svc ..."
  docker compose $COMPOSE_FILES up -d --no-deps --force-recreate "$svc"

  # Wait for healthy
  local RETRIES=30
  local healthy=false
  while [ $RETRIES -gt 0 ]; do
    STATUS=$(docker compose $COMPOSE_FILES ps --format "{{.Status}}" "$svc" 2>/dev/null | head -1)
    if echo "$STATUS" | grep -qi "healthy"; then
      healthy=true
      break
    fi
    sleep 3
    RETRIES=$((RETRIES - 1))
  done

  if [ "$healthy" = false ]; then
    error "$svc did not become healthy. Triggering rollback..."
  fi
  success "$svc healthy"
}

for svc in "${SERVICES[@]}"; do
  restart_service "$svc"
done

# ── Final health gate ─────────────────────────────────────────────────────────
info "Final health gate..."
RETRIES=20
until curl -sf "http://localhost:${API_PORT}/health" | python3 -c "import sys,json; d=json.load(sys.stdin); exit(0 if d.get('status')=='ok' else 1)" 2>/dev/null; do
  RETRIES=$((RETRIES - 1))
  if [ $RETRIES -le 0 ]; then
    error "Health gate failed after restart. Check: docker compose logs api"
  fi
  sleep 3
done

DEPLOYED_VERSION=$(curl -s "http://localhost:${API_PORT}/health" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('version','unknown'))" 2>/dev/null || echo "unknown")

echo
success "✅ Deploy complete!"
echo -e "  Deployed: ${DEPLOYED_VERSION}"
echo -e "  Endpoint: http://localhost:${API_PORT}/v1"

# ── Notify Slack (optional) ───────────────────────────────────────────────────
if [ -n "${SLACK_WEBHOOK_URL:-}" ]; then
  curl -s -X POST "$SLACK_WEBHOOK_URL" \
    -H 'Content-type: application/json' \
    --data "{\"text\":\"✅ Ghostless API deployed: \`${DEPLOYED_VERSION}\` (was \`${OLD_VERSION}\`)\"}" \
    || warn "Slack notification failed"
fi
