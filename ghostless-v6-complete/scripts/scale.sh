#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# Ghostless API — Dynamic Scaling Script
#
# Usage:
#   ./scripts/scale.sh <service> <count>
#   ./scripts/scale.sh worker 4           # scale workers to 4 replicas
#   ./scripts/scale.sh webhook-worker 3   # scale webhook workers
#   ./scripts/scale.sh auto               # auto-scale based on queue depth
#   ./scripts/scale.sh status             # show current replica counts
#
# Services: api, worker, webhook-worker
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

CYAN='\033[0;36m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'
info() { echo -e "${CYAN}[SCALE]${NC} $*"; }
ok()   { echo -e "${GREEN}[OK]${NC}    $*"; }
warn() { echo -e "${YELLOW}[WARN]${NC}  $*"; }

COMPOSE_FILES="-f docker-compose.yml -f docker-compose.prod.yml"
[ -f .env ] && { set -a; source .env; set +a; }

SERVICE="${1:-status}"
COUNT="${2:-}"

# ── Show status ────────────────────────────────────────────────────────────────
if [ "$SERVICE" = "status" ]; then
  echo -e "\n${CYAN}Current replica counts:${NC}"
  for svc in api worker webhook-worker scheduler; do
    count=$(docker compose $COMPOSE_FILES ps -q "$svc" 2>/dev/null | wc -l | tr -d ' ')
    printf "  %-20s %s replicas\n" "$svc" "$count"
  done

  echo -e "\n${CYAN}Queue depths:${NC}"
  Q_DEFAULT=$(docker compose $COMPOSE_FILES exec -T redis redis-cli llen celery 2>/dev/null || echo "?")
  Q_WEBHOOKS=$(docker compose $COMPOSE_FILES exec -T redis redis-cli llen "_kombu.binding.webhooks" 2>/dev/null || echo "?")
  printf "  %-20s %s tasks\n" "default" "$Q_DEFAULT"
  printf "  %-20s %s tasks\n" "webhooks" "$Q_WEBHOOKS"
  exit 0
fi

# ── Auto-scale ────────────────────────────────────────────────────────────────
if [ "$SERVICE" = "auto" ]; then
  info "Auto-scaling based on queue depths..."

  Q_DEFAULT=$(docker compose $COMPOSE_FILES exec -T redis redis-cli llen celery 2>/dev/null || echo "0")
  Q_WEBHOOKS=$(docker compose $COMPOSE_FILES exec -T redis redis-cli llen "_kombu.binding.webhooks" 2>/dev/null || echo "0")

  # Scale general workers
  if   [ "${Q_DEFAULT:-0}" -gt 20000 ]; then WORKER_COUNT=8
  elif [ "${Q_DEFAULT:-0}" -gt 5000  ]; then WORKER_COUNT=4
  elif [ "${Q_DEFAULT:-0}" -gt 1000  ]; then WORKER_COUNT=2
  else                                       WORKER_COUNT=1
  fi

  # Scale webhook workers
  if   [ "${Q_WEBHOOKS:-0}" -gt 5000 ]; then WEBHOOK_COUNT=4
  elif [ "${Q_WEBHOOKS:-0}" -gt 1000 ]; then WEBHOOK_COUNT=2
  else                                       WEBHOOK_COUNT=1
  fi

  info "Q_default=$Q_DEFAULT → worker=$WORKER_COUNT replicas"
  info "Q_webhooks=$Q_WEBHOOKS → webhook-worker=$WEBHOOK_COUNT replicas"

  docker compose $COMPOSE_FILES up -d --scale worker="$WORKER_COUNT" --scale webhook-worker="$WEBHOOK_COUNT" --no-recreate
  ok "Auto-scale complete"
  exit 0
fi

# ── Manual scale ──────────────────────────────────────────────────────────────
[ -z "$COUNT" ] && { echo "Usage: $0 <service> <count>"; exit 1; }

case "$SERVICE" in
  api|worker|webhook-worker) ;;
  scheduler) warn "Scheduler must always run exactly 1 replica!"; COUNT=1 ;;
  *) echo "Unknown service: $SERVICE. Valid: api, worker, webhook-worker, scheduler"; exit 1 ;;
esac

info "Scaling $SERVICE to $COUNT replicas..."
docker compose $COMPOSE_FILES up -d --scale "$SERVICE=$COUNT" --no-recreate
ok "$SERVICE scaled to $COUNT replicas"

# Verify
sleep 3
ACTUAL=$(docker compose $COMPOSE_FILES ps -q "$SERVICE" 2>/dev/null | wc -l | tr -d ' ')
info "Running replicas: $ACTUAL"
