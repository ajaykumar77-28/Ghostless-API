#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# Ghostless API — One-Command Setup
#
# Usage:
#   ./scripts/setup.sh              # development (default)
#   ./scripts/setup.sh --prod       # production
#   ./scripts/setup.sh --reset      # destroy and rebuild from scratch
#   ./scripts/setup.sh --monitoring # include Prometheus + Grafana
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

# ── Colors ────────────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BLUE='\033[0;34m'; CYAN='\033[0;36m'; BOLD='\033[1m'; NC='\033[0m'

info()    { echo -e "${CYAN}[INFO]${NC}  $*"; }
success() { echo -e "${GREEN}[OK]${NC}    $*"; }
warn()    { echo -e "${YELLOW}[WARN]${NC}  $*"; }
error()   { echo -e "${RED}[ERROR]${NC} $*"; exit 1; }
step()    { echo -e "\n${BOLD}${BLUE}▶ $*${NC}"; }

# ── Parse args ────────────────────────────────────────────────────────────────
MODE=development
RESET=false
MONITORING=false
COMPOSE_FILES="-f docker-compose.yml"

for arg in "$@"; do
  case $arg in
    --prod)        MODE=production; COMPOSE_FILES="$COMPOSE_FILES -f docker-compose.prod.yml" ;;
    --reset)       RESET=true ;;
    --monitoring)  MONITORING=true; COMPOSE_FILES="$COMPOSE_FILES --profile monitoring" ;;
    --help|-h)     echo "Usage: $0 [--prod] [--reset] [--monitoring]"; exit 0 ;;
  esac
done

echo -e "${BOLD}"
echo "╔════════════════════════════════════════╗"
echo "║       Ghostless API v6 — Setup         ║"
echo "║       Mode: ${MODE}${NC}${BOLD}                       ║"
echo "╚════════════════════════════════════════╝${NC}"
echo

# ── Prerequisites check ───────────────────────────────────────────────────────
step "Checking prerequisites"

check_cmd() {
  if command -v "$1" &> /dev/null; then
    success "$1 found ($(command -v "$1"))"
  else
    error "$1 not found. Please install it first."
  fi
}

check_cmd docker
check_cmd "docker compose" || check_cmd docker-compose

# Docker daemon
if ! docker info &> /dev/null; then
  error "Docker daemon not running. Start Docker first."
fi

# Docker Compose version
COMPOSE_VERSION=$(docker compose version --short 2>/dev/null || docker-compose --version | grep -oP '\d+\.\d+' | head -1)
info "Docker Compose: $COMPOSE_VERSION"

# ── Reset ─────────────────────────────────────────────────────────────────────
if [ "$RESET" = true ]; then
  step "Resetting environment (destroying all containers and volumes)"
  warn "This will DELETE all data. Sleeping 5 seconds. Ctrl+C to abort."
  sleep 5
  docker compose $COMPOSE_FILES down -v --remove-orphans 2>/dev/null || true
  success "Reset complete"
fi

# ── Environment file ──────────────────────────────────────────────────────────
step "Setting up environment"

if [ ! -f .env ]; then
  if [ "$MODE" = production ]; then
    cp .env.production .env
    warn ".env created from .env.production template"
    warn "⚠️  You MUST edit .env and replace all CHANGE_ME values before continuing!"
    echo
    echo "  Required fields:"
    echo "  - SECRET_KEY          (run: openssl rand -hex 32)"
    echo "  - POSTGRES_PASSWORD   (strong random password)"
    echo "  - REDIS_PASSWORD      (strong random password)"
    echo "  - DATABASE_URL        (update with your password)"
    echo "  - REDIS_URL           (update with your password)"
    echo
    read -rp "Have you updated .env? [y/N] " confirm
    [[ "${confirm,,}" == "y" ]] || error "Aborted. Edit .env first."
  else
    cp .env.development .env
    success ".env created from .env.development"
  fi
else
  success ".env already exists"
fi

# Security check for production
if [ "$MODE" = production ]; then
  if grep -q "CHANGE_ME" .env; then
    error "Found CHANGE_ME in .env. Please replace all placeholder values."
  fi
  SECRET_LEN=$(grep "^SECRET_KEY=" .env | cut -d= -f2 | tr -d '"' | wc -c)
  if [ "$SECRET_LEN" -lt 32 ]; then
    error "SECRET_KEY is too short. Minimum 32 characters. Generate with: openssl rand -hex 32"
  fi
  success "Production secrets validated"
fi

# ── Pull / build images ───────────────────────────────────────────────────────
step "Building images"
export GIT_SHA=$(git rev-parse --short HEAD 2>/dev/null || echo "unknown")
export BUILD_DATE=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
export APP_VERSION=$(grep "^APP_VERSION=" .env | cut -d= -f2 | tr -d '"' || echo "6.0.0")

docker compose $COMPOSE_FILES build --parallel
success "Images built (SHA: $GIT_SHA)"

# ── Start infrastructure ──────────────────────────────────────────────────────
step "Starting infrastructure (DB + Redis)"
docker compose $COMPOSE_FILES up -d db redis
info "Waiting for database to be ready..."

RETRIES=30
until docker compose $COMPOSE_FILES exec -T db pg_isready -U ghost -d ghostless &>/dev/null; do
  RETRIES=$((RETRIES - 1))
  [ $RETRIES -le 0 ] && error "Database did not become ready in time."
  printf "."
  sleep 2
done
echo
success "Database ready"

info "Waiting for Redis..."
RETRIES=15
until docker compose $COMPOSE_FILES exec -T redis redis-cli ping 2>/dev/null | grep -q PONG; do
  RETRIES=$((RETRIES - 1))
  [ $RETRIES -le 0 ] && error "Redis did not become ready in time."
  sleep 1
done
success "Redis ready"

# ── Database migrations ───────────────────────────────────────────────────────
step "Running database migrations"
docker compose $COMPOSE_FILES run --rm migrate
success "Migrations complete"

# ── Start all services ────────────────────────────────────────────────────────
step "Starting all services"
docker compose $COMPOSE_FILES up -d
success "All services started"

# ── Health verification ───────────────────────────────────────────────────────
step "Verifying health"
API_PORT=$(grep "^API_PORT=" .env | cut -d= -f2 | tr -d '"' || echo "8000")
info "Checking API health at http://localhost:${API_PORT}/health ..."

RETRIES=20
until curl -sf "http://localhost:${API_PORT}/health" | grep -q '"status"'; do
  RETRIES=$((RETRIES - 1))
  [ $RETRIES -le 0 ] && error "API did not become healthy. Check: docker compose logs api"
  sleep 3
done

HEALTH=$(curl -s "http://localhost:${API_PORT}/health")
success "API is healthy: $HEALTH"

# ── Show status ───────────────────────────────────────────────────────────────
echo
echo -e "${GREEN}${BOLD}✅ Ghostless API v6 is running!${NC}"
echo
echo "  Services:"
docker compose $COMPOSE_FILES ps --format "  {{.Service}}\t{{.Status}}" 2>/dev/null || \
docker compose $COMPOSE_FILES ps
echo
echo "  Endpoints:"
echo -e "  ${CYAN}API${NC}       http://localhost:${API_PORT}/v1"
echo -e "  ${CYAN}Docs${NC}      http://localhost:${API_PORT}/docs"
echo -e "  ${CYAN}Health${NC}    http://localhost:${API_PORT}/health"
echo -e "  ${CYAN}Metrics${NC}   http://localhost:${API_PORT}/metrics"
if [ "$MONITORING" = true ]; then
  echo -e "  ${CYAN}Flower${NC}    http://localhost:5555"
  echo -e "  ${CYAN}Prometheus${NC} http://localhost:9090"
  echo -e "  ${CYAN}Grafana${NC}   http://localhost:3000"
fi
echo
echo "  Quick commands:"
echo "  docker compose logs -f api          # follow API logs"
echo "  docker compose logs -f worker       # follow worker logs"
echo "  ./scripts/health.sh                 # detailed health check"
echo "  ./scripts/deploy.sh                 # deploy new version"
echo
