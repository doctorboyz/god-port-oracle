#!/bin/bash
# Deploy oracle-engine to VPS
# Usage: ./scripts/deploy-vps.sh [start|stop|logs|status|restart]
#
# This script:
# 1. Rsyncs the project to VPS
# 2. Builds the oracle-engine Docker image on VPS
# 3. Starts the containers with docker-compose.vps.yml
#
# Environment:
# - VPS_HOST: SSH host (default: vpsdeluna)
# - DRY_RUN: 1=dry run, 0=live trading (default: 1 for safety)
# - TRADING_PHASE: collect|trade|both (default: both)
# - M5_SCALP_ENABLED: 1=enable M5 scalp mode (default: 0)

set -e

VPS_HOST="${VPS_HOST:-vpsdeluna}"
REMOTE_DIR="/root/god-port-oracle"
DRY_RUN="${DRY_RUN:-1}"
TRADING_PHASE="${TRADING_PHASE:-both}"
M5_SCALP_ENABLED="${M5_SCALP_ENABLED:-0}"
COMPOSE_FILE="docker-compose.vps.yml"
ACTION="${1:-start}"

echo "=== Oracle Engine Deployment ==="
echo "Host: $VPS_HOST"
echo "Phase: $TRADING_PHASE | Dry run: $DRY_RUN | M5 Scalp: $M5_SCALP_ENABLED"

# Copy project to VPS
echo "[1/3] Syncing project files..."
rsync -avz --delete \
    --exclude='.git' \
    --exclude='__pycache__' \
    --exclude='*.pyc' \
    --exclude='data/xau-data' \
    --exclude='data/forward_test_results.json' \
    --exclude='.env' \
    --exclude='ψ/' \
    --exclude='.claude/' \
    --exclude='node_modules' \
    --exclude='.venv' \
    --exclude='.synapse' \
    ./ "$VPS_HOST:$REMOTE_DIR/"

# Copy .env separately (contains secrets)
echo "[2/3] Copying environment config..."
scp .env "$VPS_HOST:$REMOTE_DIR/.env" 2>/dev/null || echo "  (no .env file found, using VPS defaults)"

case "$ACTION" in
    start)
        echo "[3/3] Starting oracle-engine on VPS..."
        ssh "$VPS_HOST" "cd $REMOTE_DIR && \
            set -a && source .env 2>/dev/null && set +a && \
            docker compose -f $COMPOSE_FILE build oracle-engine && \
            docker compose -f $COMPOSE_FILE up -d oracle-engine && \
            echo 'Oracle engine started!' && \
            docker compose -f $COMPOSE_FILE ps"
        ;;
    stop)
        echo "[3/3] Stopping oracle-engine on VPS..."
        ssh "$VPS_HOST" "cd $REMOTE_DIR && docker compose -f $COMPOSE_FILE stop oracle-engine"
        ;;
    logs)
        ssh "$VPS_HOST" "cd $REMOTE_DIR && docker compose -f $COMPOSE_FILE logs -f --tail 50 oracle-engine"
        ;;
    status)
        ssh "$VPS_HOST" "cd $REMOTE_DIR && docker compose -f $COMPOSE_FILE ps && echo '---' && docker compose -f $COMPOSE_FILE logs --tail 20 oracle-engine"
        ;;
    rebuild)
        echo "[3/3] Rebuilding + restarting oracle-engine on VPS..."
        ssh "$VPS_HOST" "cd $REMOTE_DIR && \
            set -a && source .env 2>/dev/null && set +a && \
            docker compose -f $COMPOSE_FILE build --no-cache oracle-engine && \
            docker compose -f $COMPOSE_FILE up -d oracle-engine && \
            echo 'Oracle engine rebuilt!' && \
            docker compose -f $COMPOSE_FILE ps"
        ;;
    restart)
        echo "[3/3] Restarting oracle-engine on VPS..."
        ssh "$VPS_HOST" "cd $REMOTE_DIR && \
            docker compose -f $COMPOSE_FILE restart oracle-engine && \
            docker compose -f $COMPOSE_FILE ps"
        ;;
    *)
        echo "Usage: $0 [start|stop|logs|status|restart|rebuild]"
        exit 1
        ;;
esac