#!/bin/bash
# Deploy oracle-engine service to VPS
# Usage: ./scripts/deploy-vps.sh [start|stop|logs|status|restart|rebuild] [SERVICE]
#
# This script:
# 1. Rsyncs the project to VPS
# 2. Builds the target Docker image on VPS
# 3. Starts the container with docker-compose.vps.yml
#
# Args:
#   ACTION:  start|stop|logs|status|restart|rebuild  (default: start)
#   SERVICE: oracle-engine|oracle-engine-train       (default: oracle-engine-train)
#
# Environment:
# - VPS_HOST: SSH host (default: vpsdeluna)
# - DRY_RUN: 1=dry run, 0=live trading (default: 1 for safety)
# - TRADING_PHASE: collect|trade|both (default: both)
# - M5_SCALP_ENABLED: 1=enable M5 scalp mode (default: 0)
#
# IRON LAW: NEVER deploy to oracle-engine (Real-A) unless explicitly passing
# "oracle-engine" as SERVICE. Default is oracle-engine-train (B/C/D demo).

set -e

VPS_HOST="${VPS_HOST:-vpsdeluna}"
REMOTE_DIR="/root/god-port-oracle"
DRY_RUN="${DRY_RUN:-1}"
TRADING_PHASE="${TRADING_PHASE:-both}"
M5_SCALP_ENABLED="${M5_SCALP_ENABLED:-0}"
COMPOSE_FILE="docker-compose.vps.yml"
ACTION="${1:-start}"
SERVICE="${2:-oracle-engine-train}"
ENV_FILE="${ENV_FILE:-.env}"

# IRON LAW guard — refuse to deploy oracle-engine (Real-A) by accident
if [ "$SERVICE" = "oracle-engine" ]; then
    echo "⚠️  IRON LAW WARNING: target is 'oracle-engine' (Real-A account)."
    echo "    BCD deploys must target 'oracle-engine-train'."
    echo "    Type the literal phrase 'I AM DEPLOYING TO REAL A' to proceed:"
    read -r CONFIRM
    if [ "$CONFIRM" != "I AM DEPLOYING TO REAL A" ]; then
        echo "Aborted — Real-A protected."
        exit 1
    fi
fi

echo "=== Oracle Engine Deployment ==="
echo "Host: $VPS_HOST"
echo "Service: $SERVICE | Action: $ACTION"
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
        echo "[3/3] Starting $SERVICE on VPS..."
        ssh "$VPS_HOST" "cd $REMOTE_DIR && \
            set -a && source .env && set +a && \
            docker compose -f $COMPOSE_FILE build $SERVICE && \
            docker compose -f $COMPOSE_FILE up -d $SERVICE && \
            echo '$SERVICE started!' && \
            docker compose -f $COMPOSE_FILE ps && \
            echo '' && \
            echo '=== Running ML smoke test ===' && \
            sleep 5 && \
            docker compose -f $COMPOSE_FILE exec $SERVICE python scripts/smoke-test-ml.py || \
            echo '⚠️  ML smoke test failed — check model loading!'"
        ;;
    stop)
        echo "[3/3] Stopping $SERVICE on VPS..."
        ssh "$VPS_HOST" "cd $REMOTE_DIR && docker compose -f $COMPOSE_FILE stop $SERVICE"
        ;;
    logs)
        ssh "$VPS_HOST" "cd $REMOTE_DIR && docker compose -f $COMPOSE_FILE logs -f --tail 50 $SERVICE"
        ;;
    status)
        ssh "$VPS_HOST" "cd $REMOTE_DIR && docker compose -f $COMPOSE_FILE ps && echo '---' && docker compose -f $COMPOSE_FILE logs --tail 20 $SERVICE"
        ;;
    rebuild)
        echo "[3/3] Rebuilding + restarting $SERVICE on VPS..."
        ssh "$VPS_HOST" "cd $REMOTE_DIR && \
            set -a && source .env && set +a && \
            docker compose -f $COMPOSE_FILE build --no-cache $SERVICE && \
            docker compose -f $COMPOSE_FILE up -d $SERVICE && \
            echo '$SERVICE rebuilt!' && \
            docker compose -f $COMPOSE_FILE ps && \
            echo '' && \
            echo '=== Running ML smoke test ===' && \
            sleep 5 && \
            docker compose -f $COMPOSE_FILE exec $SERVICE python scripts/smoke-test-ml.py || \
            echo '⚠️  ML smoke test failed — check model loading!'"
        ;;
    restart)
        echo "[3/3] Restarting $SERVICE on VPS..."
        ssh "$VPS_HOST" "cd $REMOTE_DIR && \
            docker compose -f $COMPOSE_FILE restart $SERVICE && \
            docker compose -f $COMPOSE_FILE ps"
        ;;
    *)
        echo "Usage: $0 [start|stop|logs|status|restart|rebuild] [oracle-engine-train|oracle-engine]"
        exit 1
        ;;
esac