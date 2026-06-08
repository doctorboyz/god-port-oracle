#!/usr/bin/env bash
# Deploy god-port-oracle to VPS with verification
#
# Usage:
#   ./scripts/deploy_vps.sh              # Full deploy: push code, restart, verify
#   ./scripts/deploy_vps.sh --verify     # Verify only (no deploy)
#   ./scripts/deploy_vps.sh --push       # Push only (no restart/verify)
#   ./scripts/deploy_vps.sh --restart    # Restart only (assumes code already pushed)
#
# Requires: ssh access to VPS, docker compose on VPS

set -euo pipefail

VPS_HOST="root@100.68.106.101"
VPS_DIR="/root/god-port-oracle"
COMPOSE_FILE="docker-compose.vps.yml"

# Files that must be deployed (relative to repo root)
CRITICAL_FILES=(
    "broky/ml/features.py"
    "broky/ml/trade_outcome_predictor.py"
    "broky/ml/trade_outcome_trainer.py"
    "metty/core/db.py"
    "metty/execution/live_trader.py"
    "metty/execution/m5_scalp_trader.py"
    "metty/execution/scalp_trader.py"
    "metty/notify/telegram_bot.py"
    "shared/events.py"
    "Dockerfile"
    "docker-compose.vps.yml"
)

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

info()  { echo -e "${GREEN}[DEPLOY]${NC} $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*"; exit 1; }

# ─── Phase 1: Compute local checksums ───────────────────────────────────────

compute_checksums() {
    info "Computing local checksums..."
    for f in "${CRITICAL_FILES[@]}"; do
        if [ -f "$f" ]; then
            echo "$(md5 -q "$f" 2>/dev/null || md5sum "$f" | cut -d' ' -f1)  $f"
        else
            warn "Local file missing: $f"
            echo "MISSING  $f"
        fi
    done
}

# ─── Phase 2: Push files to VPS ────────────────────────────────────────────

push_files() {
    info "Pushing critical files to VPS..."

    # Push each critical file
    for f in "${CRITICAL_FILES[@]}"; do
        if [ -f "$f" ]; then
            info "  Pushing $f..."
            # Create directory on VPS if needed
            ssh "$VPS_HOST" "mkdir -p $(dirname "$VPS_DIR/$f")"
            scp -q "$f" "$VPS_HOST:$VPS_DIR/$f"
        else
            warn "  Skipping missing file: $f"
        fi
    done

    # Also push the features module directory
    info "  Pushing broky/ml/ directory..."
    scp -q -r broky/ml/ "$VPS_HOST:$VPS_DIR/broky/ml/"

    info "Push complete."
}

# ─── Phase 3: Restart container ─────────────────────────────────────────────

restart_container() {
    info "Restarting oracle-engine container..."
    ssh "$VPS_HOST" "cd $VPS_DIR && docker compose -f $COMPOSE_FILE restart oracle-engine"

    info "Waiting for container to become healthy (max 60s)..."
    for i in $(seq 1 12); do
        sleep 5
        status=$(ssh "$VPS_HOST" "cd $VPS_DIR && docker compose -f $COMPOSE_FILE ps --format '{{.Status}}' oracle-engine 2>/dev/null" || echo "unknown")
        if echo "$status" | grep -q "healthy"; then
            info "Container is healthy!"
            return 0
        fi
        info "  Waiting... ($((i*5))s) status: $status"
    done

    error "Container did not become healthy within 60s. Check logs with: ssh $VPS_HOST 'docker logs oracle-engine --tail 50'"
}

# ─── Phase 4: Verify deployment ─────────────────────────────────────────────

verify_deployment() {
    info "Verifying deployment on VPS..."

    local failures=0

    # 4a. Check containers are running
    info "  Checking containers..."
    for svc in mt5a mt5b mt5c oracle-engine; do
        status=$(ssh "$VPS_HOST" "cd $VPS_DIR && docker compose -f $COMPOSE_FILE ps --format '{{.Status}}' $svc 2>/dev/null" || echo "unknown")
        if echo "$status" | grep -q "healthy"; then
            info "  ✅ $svc: $status"
        else
            warn "  ❌ $svc: $status"
            failures=$((failures + 1))
        fi
    done

    # 4b. Check ML model loads correctly
    info "  Checking ML model..."
    model_check=$(ssh "$VPS_HOST" "cd $VPS_DIR && docker exec oracle-engine python3 -c '
from broky.ml.trade_outcome_predictor import TradeOutcomePredictor
p = TradeOutcomePredictor()
print(f\"enabled={p.enabled}\")
print(f\"models={len(p._models)}\")
print(f\"model_list={list(p._models.keys())}\")
'" 2>&1)

    if echo "$model_check" | grep -q "enabled=True"; then
        model_count=$(echo "$model_check" | grep "models=" | head -1 | sed 's/.*models=//')
        info "  ✅ ML model loaded: $model_count models"
    else
        warn "  ❌ ML model not loading correctly"
        echo "$model_check" | head -5
        failures=$((failures + 1))
    fi

    # 4c. Check DB schema (critical columns)
    info "  Checking DB schema..."
    schema_check=$(ssh "$VPS_HOST" "cd $VPS_DIR && docker exec oracle-engine python3 -c '
import sqlite3
conn = sqlite3.connect(\"/app/data/oracle.db\")
cur = conn.cursor()
cur.execute(\"PRAGMA table_info(live_trades)\")
cols = [row[1] for row in cur.fetchall()]
required = [\"tp1_price\", \"parent_trade_id\", \"tp_level\", \"remaining_lots\", \"atr_multiplier\", \"rr_ratio\", \"min_confidence_threshold\"]
	# h4_trend is stored inside indicator_scores_json, not a separate column
missing = [c for c in required if c not in cols]
if missing:
    print(f\"MISSING: {missing}\")
else:
    print(\"OK: all required columns present\")
cur.execute(\"SELECT COUNT(*) FROM live_trades\")
print(f\"trades: {cur.fetchone()[0]}\")
conn.close()
'" 2>&1)

    if echo "$schema_check" | grep -q "OK"; then
        trades=$(echo "$schema_check" | grep "trades:" | sed 's/.*trades: //')
        info "  ✅ DB schema OK ($trades trades)"
    else
        warn "  ❌ DB schema issues"
        echo "$schema_check" | head -5
        failures=$((failures + 1))
    fi

    # 4d. Check environment variables
    info "  Checking env vars..."
    env_check=$(ssh "$VPS_HOST" "cd $VPS_DIR && docker exec oracle-engine env" 2>&1)
    ml_dir=$(echo "$env_check" | grep "^ML_MODEL_DIR=" | cut -d= -f2)
    ml_enabled=$(echo "$env_check" | grep "^ML_FILTER_ENABLED=" | cut -d= -f2)
    partial_c=$(echo "$env_check" | grep "^PARTIAL_TP_ENABLED_C=" | cut -d= -f2)

    info "  ML_MODEL_DIR=$ml_dir"
    info "  ML_FILTER_ENABLED=$ml_enabled"
    info "  PARTIAL_TP_ENABLED_C=$partial_c"

    if [[ "$ml_dir" == *"v4"* ]] || [[ "$ml_dir" == *"trade_outcome_v4"* ]]; then
        info "  ✅ Using v4 model"
    else
        warn "  ⚠️  Not using v4 model: $ml_dir"
        failures=$((failures + 1))
    fi

    # 4e. Verify recent logs (no errors)
    info "  Checking recent logs for errors..."
    error_count=$(ssh "$VPS_HOST" "docker logs oracle-engine --tail 20 2>&1 | grep -ci 'ERROR\|Traceback\|exception'" 2>/dev/null || echo "0")
    error_count=$(echo "$error_count" | tr -d '[:space:]')
    if [ "$error_count" -le 1 ] 2>/dev/null; then
        info "  ✅ No recent errors in logs"
    else
        warn "  ⚠️  $error_count potential errors in recent logs"
    fi

    # Summary
    echo ""
    if [ $failures -eq 0 ]; then
        info "🎉 Deployment verified successfully! All checks passed."
    else
        warn "⚠️  $failures check(s) failed. Review output above."
    fi

    return $failures
}

# ─── Main ──────────────────────────────────────────────────────────────────

MODE="${1:-full}"

case "$MODE" in
    --verify)
        verify_deployment
        ;;
    --push)
        push_files
        ;;
    --restart)
        restart_container
        verify_deployment
        ;;
    --checksums)
        compute_checksums
        ;;
    full|"")
        compute_checksums
        push_files
        restart_container
        verify_deployment
        ;;
    *)
        echo "Usage: $0 [--verify|--push|--restart|--checksums|full]"
        echo ""
        echo "  full         Push files, restart, and verify (default)"
        echo "  --verify     Verify deployment only (no push/restart)"
        echo "  --push       Push files only (no restart/verify)"
        echo "  --restart    Restart container and verify"
        echo "  --checksums  Show local file checksums"
        exit 1
        ;;
esac