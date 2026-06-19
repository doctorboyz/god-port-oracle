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
ENV_FILE="${ENV_FILE:-.env}"

echo "=== Oracle Engine Deployment ==="
echo "Host: $VPS_HOST"
echo "Phase: $TRADING_PHASE | Dry run: $DRY_RUN | M5 Scalp: $M5_SCALP_ENABLED"

# ── .env validation ──
# Critical vars that MUST be present for trading to work
CRITICAL_VARS="MT5_LOGIN_A MT5_PASSWORD_A MT5_SERVER_A MT5_LOGIN_B MT5_PASSWORD_B MT5_SERVER_B"

if [ ! -f "$ENV_FILE" ]; then
    echo "ERROR: $ENV_FILE not found — deploy requires a valid .env file with broker credentials"
    echo "  Copy .env.example to .env and fill in your credentials"
    exit 1
fi

MISSING_VARS=0
for var in $CRITICAL_VARS; do
    if ! grep -q "^${var}=" "$ENV_FILE" || [ -z "$(grep "^${var}=" "$ENV_FILE" | cut -d= -f2-)" ]; then
        echo "  MISSING: $var is empty or not set in $ENV_FILE"
        MISSING_VARS=$((MISSING_VARS + 1))
    fi
done

if [ $MISSING_VARS -gt 0 ]; then
    echo "ERROR: $MISSING_VARS critical env var(s) missing — containers will fail without broker credentials"
    echo "  Fix $ENV_FILE before deploying"
    exit 1
fi
echo "✓ .env validated — all critical vars present"

# Pre-deploy safety check
echo "[0/3] Running pre-deploy checks..."
if ! bash "$(dirname "$0")/pre-deploy-check.sh"; then
    echo "ERROR: Pre-deploy checks failed — aborting deploy"
    exit 1
fi

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
scp "$ENV_FILE" "$VPS_HOST:$REMOTE_DIR/.env"

case "$ACTION" in
    start)
        echo "[3/3] Starting oracle-engine on VPS..."
        ssh "$VPS_HOST" "cd $REMOTE_DIR && \
            set -a && source .env && set +a && \
            docker compose -f $COMPOSE_FILE build oracle-engine && \
            docker compose -f $COMPOSE_FILE up -d oracle-engine && \
            echo 'Oracle engine started!' && \
            docker compose -f $COMPOSE_FILE ps && \
            echo '' && \
            echo '=== Running ML smoke test ===' && \
            sleep 5 && \
            docker compose -f $COMPOSE_FILE exec oracle-engine python scripts/smoke-test-ml.py || \
            echo '⚠️  ML smoke test failed — check model loading!'"
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
            set -a && source .env && set +a && \
            docker compose -f $COMPOSE_FILE build --no-cache oracle-engine && \
            docker compose -f $COMPOSE_FILE up -d oracle-engine && \
            echo 'Oracle engine rebuilt!' && \
            docker compose -f $COMPOSE_FILE ps && \
            echo '' && \
            echo '=== Running ML smoke test ===' && \
            sleep 5 && \
            docker compose -f $COMPOSE_FILE exec oracle-engine python scripts/smoke-test-ml.py || \
            echo '⚠️  ML smoke test failed — check model loading!'"
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