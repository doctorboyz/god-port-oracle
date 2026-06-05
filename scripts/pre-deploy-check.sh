#!/usr/bin/env bash
# Pre-deploy safety check — syntax + import + SQL + scope verification
# Run before any deploy to VPS. Exits non-zero on failure.
set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

FAILURES=0
ROOT="$(cd "$(dirname "$0")/.." && pwd)"

echo "=== Pre-Deploy Check (5 steps) ==="
echo ""

# ── 1. Syntax check (py_compile) ──
echo "[1/5] Syntax check..."
SYNTAX_FAIL=0
while IFS= read -r -d '' pyfile; do
    if ! python3 -c "import py_compile; py_compile.compile('$pyfile', doraise=True)" 2>/dev/null; then
        echo -e "  ${RED}✗ SYNTAX ERROR${NC}: $pyfile"
        python3 -c "import py_compile; py_compile.compile('$pyfile', doraise=True)" 2>&1 | tail -3
        SYNTAX_FAIL=$((SYNTAX_FAIL + 1))
        FAILURES=$((FAILURES + 1))
    fi
done < <(find "$ROOT/broky" "$ROOT/metty" "$ROOT/shared" "$ROOT/scripts" -name '*.py' -not -path '*/__pycache__/*' -print0 2>/dev/null)

if [ $SYNTAX_FAIL -eq 0 ]; then
    echo -e "  ${GREEN}✓ All Python files pass syntax check${NC}"
else
    echo -e "  ${RED}✗ $SYNTAX_FAIL file(s) have syntax errors${NC}"
fi

# ── 2. Import test (key modules) ──
echo "[2/5] Import test..."
IMPORTS=(
    "broky.signals.generator:generate_signal"
    "broky.ml.features:FeatureEngineer"
    "broky.ml.trade_outcome_trainer:TradeOutcomeTrainer"
    "broky.ml.trade_outcome_predictor:TradeOutcomePredictor"
    "broky.ml.trade_outcome_predictor:compute_features_from_candles"
    "metty.execution.live_trader:LiveTrader"
    "metty.execution.m5_scalp_trader:M5ScalpTrader"
    "metty.execution.scalp_trader:ScalpTrader"
    "metty.execution.live_collector:LiveCollector"
    "metty.core.db:get_connection"
    "metty.core.db:insert_live_trade"
    "metty.core.db:close_live_trade"
    "metty.notify.telegram_bot:TelegramNotifier"
    "metty.bridge.client:PersistentMT5Bridge"
    "broky.signals.m5_scalp_generator:generate_m5_scalp_signal"
    "broky.signals.scalp_generator:generate_scalp_signal"
)

for entry in "${IMPORTS[@]}"; do
    module="${entry%:*}"
    attr="${entry#*:}"
    if python3 -c "from $module import $attr" 2>/dev/null; then
        echo -e "  ${GREEN}✓${NC} $module.$attr"
    else
        echo -e "  ${RED}✗ IMPORT FAILED${NC}: from $module import $attr"
        python3 -c "from $module import $attr" 2>&1 | tail -5
        FAILURES=$((FAILURES + 1))
    fi
done

# ── 3-4. SQL + Scope checks (Python script) ──
echo "[3/5] SQL + Scope checks..."
if python3 "$ROOT/scripts/pre_deploy_checks.py"; then
    :
else
    FAILURES=$((FAILURES + 1))
fi

echo ""
# ── 5. ML model smoke test ──
echo "[5/5] ML model loading test..."
ML_MODEL_DIR="${ML_MODEL_DIR:-data/models/v4}"
if python3 "$ROOT/scripts/smoke-test-ml.py" --model-dir "$ML_MODEL_DIR" > /dev/null 2>&1; then
    echo -e "  ${GREEN}✓ ML models load and predict successfully${NC}"
else
    echo -e "  ${YELLOW}⚠ ML model test failed — models may not load on VPS${NC}"
    echo -e "  ${YELLOW}  Run 'python3 scripts/smoke-test-ml.py' for details${NC}"
    echo -e "  ${YELLOW}  This is a WARNING, not a blocker — deploy may still work if VPS has models${NC}"
fi

echo ""
if [ $FAILURES -eq 0 ]; then
    echo -e "${GREEN}=== All checks passed — ready to deploy ===${NC}"
    exit 0
else
    echo -e "${RED}=== $FAILURES check(s) failed — fix before deploying ===${NC}"
    exit 1
fi