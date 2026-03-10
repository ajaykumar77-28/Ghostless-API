#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# Ghostless API — Health Check Script
#
# Usage: ./scripts/health.sh [--json] [--watch]
#   --json    output JSON (for monitoring integrations)
#   --watch   run continuously every 30s
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

JSON_MODE=false
WATCH_MODE=false

for arg in "$@"; do
  case $arg in
    --json)  JSON_MODE=true ;;
    --watch) WATCH_MODE=true ;;
  esac
done

[ -f .env ] && { set -a; source .env; set +a; }
API_PORT="${API_PORT:-8000}"

# ── Check functions ───────────────────────────────────────────────────────────
check_http() {
  local name="$1" url="$2" expected="${3:-200}"
  local code
  code=$(curl -s -o /dev/null -w "%{http_code}" --max-time 5 "$url" 2>/dev/null || echo "000")
  if [ "$code" = "$expected" ]; then
    echo "ok:$name:$code"
  else
    echo "fail:$name:$code (expected $expected)"
  fi
}

check_container() {
  local name="$1"
  local status
  status=$(docker inspect --format='{{.State.Health.Status}}' "$(docker compose ps -q "$name" 2>/dev/null | head -1)" 2>/dev/null || echo "not_found")
  echo "${status}:$name"
}

check_queue_depth() {
  local queue="$1"
  local depth
  depth=$(docker compose exec -T redis redis-cli llen "celery_$queue" 2>/dev/null || echo "?")
  echo "$queue:$depth"
}

do_check() {
  local ts=$(date -u +"%Y-%m-%dT%H:%M:%SZ")

  # ── API health ──────────────────────────────────────────────────────────────
  API_HEALTH=$(curl -s --max-time 5 "http://localhost:${API_PORT}/health" 2>/dev/null || echo '{"status":"unreachable"}')
  API_STATUS=$(echo "$API_HEALTH" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('status','unknown'))" 2>/dev/null || echo "parse_error")
  API_VERSION=$(echo "$API_HEALTH" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('version','?'))" 2>/dev/null || echo "?")
  API_DB=$(echo "$API_HEALTH" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('database','?'))" 2>/dev/null || echo "?")

  # ── Container health ────────────────────────────────────────────────────────
  CONTAINERS=()
  for svc in api worker webhook-worker scheduler db redis nginx; do
    CONTAINERS+=("$(check_container "$svc")")
  done

  # ── Queue depths ────────────────────────────────────────────────────────────
  Q_DEFAULT=$(docker compose exec -T redis redis-cli llen "celery" 2>/dev/null || echo "?")
  Q_WEBHOOKS=$(docker compose exec -T redis redis-cli llen "_kombu.binding.webhooks" 2>/dev/null || echo "?")
  Q_SCORING=$(docker compose exec -T redis redis-cli llen "_kombu.binding.scoring" 2>/dev/null || echo "?")

  # ── Metrics snapshot ────────────────────────────────────────────────────────
  METRICS_SNIPPET=$(curl -s --max-time 3 "http://localhost:${API_PORT}/metrics" 2>/dev/null | \
    grep -E "^(http_requests_total|http_request_duration_seconds_sum)" | head -5 || echo "unavailable")

  if [ "$JSON_MODE" = true ]; then
    python3 - <<EOF
import json
data = {
    "timestamp": "$ts",
    "api": {
        "status": "$API_STATUS",
        "version": "$API_VERSION",
        "database": "$API_DB"
    },
    "containers": {$(for c in "${CONTAINERS[@]}"; do
        IFS=: read -r status name <<< "$c"
        printf '"'"$name"'": "'"$status"'",'
      done | sed 's/,$//')},
    "queues": {
        "default":  "$Q_DEFAULT",
        "webhooks": "$Q_WEBHOOKS",
        "scoring":  "$Q_SCORING"
    }
}
print(json.dumps(data, indent=2))
EOF
  else
    # ── Human-readable output ─────────────────────────────────────────────────
    echo "╔═══════════════════════════════════════════════╗"
    echo "║      Ghostless API — Health Report            ║"
    echo "║  $(date '+%Y-%m-%d %H:%M:%S UTC')              ║"
    echo "╠═══════════════════════════════════════════════╣"
    printf "║ %-15s  %-10s  %-16s ║\n" "COMPONENT" "STATUS" "DETAIL"
    echo "╠═══════════════════════════════════════════════╣"
    printf "║ %-15s  %-10s  %-16s ║\n" "API" "$API_STATUS" "v${API_VERSION}"
    printf "║ %-15s  %-10s  %-16s ║\n" "API→DB" "$API_DB" ""
    echo "╠═══════════════════════════════════════════════╣"

    for c in "${CONTAINERS[@]}"; do
      IFS=: read -r status name <<< "$c"
      ICON="✅"; [ "$status" != "healthy" ] && [ "$status" != "running" ] && ICON="❌"
      printf "║ %-3s %-12s  %-10s  %-16s ║\n" "$ICON" "$name" "$status" ""
    done

    echo "╠═══════════════════════════════════════════════╣"
    printf "║ %-15s  %-10s  %-16s ║\n" "Q: default" "${Q_DEFAULT} jobs" ""
    printf "║ %-15s  %-10s  %-16s ║\n" "Q: webhooks" "${Q_WEBHOOKS} jobs" ""
    printf "║ %-15s  %-10s  %-16s ║\n" "Q: scoring" "${Q_SCORING} jobs" ""
    echo "╚═══════════════════════════════════════════════╝"

    # Warnings
    if [ "${Q_DEFAULT:-0}" -gt 1000 ] 2>/dev/null; then
      echo "⚠️  WARNING: default queue depth ${Q_DEFAULT} > 1000 — consider scaling workers"
    fi
    if [ "${Q_WEBHOOKS:-0}" -gt 500 ] 2>/dev/null; then
      echo "⚠️  WARNING: webhook queue depth ${Q_WEBHOOKS} > 500 — webhooks may be backing up"
    fi
    if [ "$API_STATUS" != "ok" ]; then
      echo "❌ CRITICAL: API status is not ok — check docker compose logs api"
    fi
  fi
}

if [ "$WATCH_MODE" = true ]; then
  while true; do
    clear
    do_check
    echo -e "\nRefreshing every 30s. Ctrl+C to stop."
    sleep 30
  done
else
  do_check
fi
