#!/bin/bash
# Post-deploy verification for ML model + config changes.
#
# Catches the silent-failure class of bugs (e.g., ISSUE-034 mixed_v12 bxau
# dependency: predictor loaded but produced 0 predictions because the
# feature_engineer.joblib referenced a module not in the Docker image).
#
# Usage:
#   ./scripts/verify_deploy.sh                   # verify both containers
#   ./scripts/verify_deploy.sh oracle-engine      # verify Real-A container
#   ./scripts/verify_deploy.sh oracle-engine-train  # verify B/C/D container
#
# Exits 0 on PASS, 1 on any FAIL — safe to use as a deploy gate.
#
# Checks per container:
#   1. Container is running
#   2. ML_MODEL_DIR env var is set and path exists in container
#   3. training_results.json parses and lists models
#   4. TradeOutcomePredictor loads (enabled=True, models>0, engineer loaded)
#   5. health_check() passes
#   6. Test prediction returns a numeric P(LOSS) — catches "no prediction" silent fail
#   7. (oracle-engine-train only) For each dir in ML_ENSEMBLE_MODEL_DIRS:
#      - dir exists in container volume
#      - training_results.json + feature_engineer.joblib present
#      - >=1 .pkl model file
#      - predict_loss_proba returns numeric AND sub-model count = N/N
#        (catches silent missing-dir bug — ensemble degraded to N-1/N)
#      Also iterates per-account ensemble modes (B/C/D) at per-account thresholds.

set -uo pipefail

VPS="${VPS_HOST:-vpsdeluna}"
COMPOSE="docker compose -f /root/god-port-oracle/docker-compose.vps.yml"

GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

FAILS=0
PASSES=0

pass() { echo -e "  ${GREEN}✅ PASS${NC}: $1"; PASSES=$((PASSES+1)); }
fail() { echo -e "  ${RED}❌ FAIL${NC}: $1"; FAILS=$((FAILS+1)); }
info() { echo -e "  ${CYAN}ℹ${NC}  $1"; }

verify_container() {
    local container="$1"
    echo -e "${CYAN}═══════════════════════════════════════════════════════${NC}"
    echo -e "${CYAN}  Verify: ${container}${NC}"
    echo -e "${CYAN}═══════════════════════════════════════════════════════${NC}"

    # 1. Container running
    local state
    state=$(ssh "$VPS" "$COMPOSE ps --format '{{.State}}' $container 2>/dev/null" | head -1)
    if [ "$state" = "running" ]; then
        pass "$container is running"
    else
        fail "$container not running (state='$state')"
        return
    fi

    # 2-6. Run Python verification inside container
    # oracle-engine (Real-A) uses ML_MODEL_DIR_A per-account suffix;
    # oracle-engine-train (B/C/D) uses ML_MODEL_DIR.
    local account_suffix=""
    if [ "$container" = "oracle-engine" ]; then
        account_suffix="_A"
    fi
    local result
    result=$(ssh "$VPS" "$COMPOSE exec -T -e ML_ACCOUNT_SUFFIX=$account_suffix $container python3" << 'PYEOF' 2>&1
import os, json, sys, traceback, warnings
warnings.filterwarnings("ignore")

errors = []
checks = []

def check(name, ok, detail=""):
    checks.append((name, ok, detail))

try:
    from broky.ml.trade_outcome_predictor import TradeOutcomePredictor
except Exception as e:
    check("import predictor", False, f"import failed: {e}")
    for name, ok, detail in checks:
        print(f"CHECK:{name}:{'PASS' if ok else 'FAIL'}:{detail}")
    sys.exit(0)

md = os.environ.get("ML_MODEL_DIR", "")
# oracle-engine uses per-account suffix ML_MODEL_DIR_A
suffix = os.environ.get("ML_ACCOUNT_SUFFIX", "")
if suffix and not md:
    md = os.environ.get(f"ML_MODEL_DIR{suffix}", "")
check("ML_MODEL_DIR env set", bool(md), f"value={md}")

if md:
    check("model dir exists", os.path.isdir(md), f"path={md}")
    tr = os.path.join(md, "training_results.json")
    if os.path.exists(tr):
        try:
            with open(tr) as f:
                d = json.load(f)
            n = len(d.get("models", []))
            check("training_results.json parses", True, f"models={n}")
            check("has >=1 model", n >= 1, f"n={n}")
        except Exception as e:
            check("training_results.json parses", False, f"err={e}")
    else:
        check("training_results.json parses", False, f"missing {tr}")

    fe = os.path.join(md, "feature_engineer.joblib")
    check("feature_engineer.joblib exists", os.path.exists(fe), f"path={fe}")

    # Load predictor
    try:
        p = TradeOutcomePredictor(model_dir=md)
        check("predictor enabled", p.enabled, f"enabled={p.enabled}")
        nm = len(p._models) if p._models else 0
        check("models loaded (>0)", nm > 0, f"n={nm}")
        eng = type(p._engineer).__module__ if p._engineer else None
        check("engineer loaded (not None)", p._engineer is not None, f"module={eng}")
        # Engineer must NOT reference bxau
        if eng and "bxau" in eng:
            check("engineer not bxau", False, f"module={eng} — bxau dep will fail in prod")
        else:
            check("engineer not bxau", True, f"module={eng}")
        # Health check
        try:
            ok, msg = p.health_check()
            check("health_check passes", ok, f"msg={msg}")
        except Exception as e:
            check("health_check passes", False, f"err={e}")
            ok = False
        # Test prediction returns numeric (catches "no prediction" silent fail)
        if ok:
            try:
                proba = p.predict_loss_proba({
                    "regime": "trending",
                    "direction": "BUY",
                    "session": "london",
                    "d1_trend": "bullish",
                    "h4_trend": "bullish",
                    "price_vs_cloud": "above",
                    "mfi_signal": "neutral",
                })
                # predict_loss_proba may return float or (proba, model_name) tuple
                if isinstance(proba, tuple):
                    val = proba[0]
                else:
                    val = proba
                is_num = isinstance(val, (int, float)) and not isinstance(val, bool)
                check("test prediction numeric", is_num, f"proba={val!r} (raw={proba!r})")
            except Exception as e:
                check("test prediction numeric", False, f"err={e}")
    except Exception as e:
        check("predictor load", False, f"exception={e}")
        traceback.print_exc()

    # ─── Ensemble verification (oracle-engine-train only) ───────────────
    # For each dir in ML_ENSEMBLE_MODEL_DIRS: assert dir exists, has
    # training_results.json + feature_engineer.joblib + >=1 model file.
    # Then build EnsemblePredictor and verify EVERY sub-model is loaded
    # (member count = N/N). Catches the silent missing-dir bug where
    # the ensemble silently degrades to N-1/N when a model dir is absent
    # from the named volume (hit 2026-06-30 — v6 missing → ensemble ran
    # as V4-alone for 12h with no error).
    try:
        from broky.ml.ensemble_predictor import EnsemblePredictor
        ensemble_dirs_raw = os.environ.get("ML_ENSEMBLE_MODEL_DIRS", "")
        ensemble_dirs = [d for d in ensemble_dirs_raw.split(":") if d]
        n_dirs = len(ensemble_dirs)
        check("ML_ENSEMBLE_MODEL_DIRS set", n_dirs > 0, f"n_dirs={n_dirs}")
        if n_dirs > 0:
            # 7a. Per-dir presence check
            for i, d in enumerate(ensemble_dirs, 1):
                exists = os.path.isdir(d)
                check(f"ensemble dir [{i}/{n_dirs}] exists", exists, f"path={d}")
                if not exists:
                    # Surface the fix command — named volume, rsync does NOT sync into it
                    print(f"INFO:FIX: docker cp <repo>/data/models/{os.path.basename(d)} "
                          f"<container>:{os.path.dirname(d)}/")
                    continue
                tr = os.path.join(d, "training_results.json")
                check(f"  dir [{i}/{n_dirs}] training_results.json",
                      os.path.exists(tr), f"path={tr}")
                fe = os.path.join(d, "feature_engineer.joblib")
                check(f"  dir [{i}/{n_dirs}] feature_engineer.joblib",
                      os.path.exists(fe), f"path={fe}")
                pkls = [f for f in os.listdir(d) if f.endswith("_model.pkl")]
                check(f"  dir [{i}/{n_dirs}] has >=1 .pkl model", len(pkls) > 0,
                      f"n={len(pkls)}")
                # Engineer module must NOT be bxau (catches ISSUE-034 class)
                if os.path.exists(fe):
                    try:
                        import joblib
                        eng = joblib.load(fe)
                        mod = type(eng).__module__ if eng else None
                        check(f"  dir [{i}/{n_dirs}] engineer not bxau",
                              not (mod and "bxau" in mod), f"module={mod}")
                    except Exception as e:
                        check(f"  dir [{i}/{n_dirs}] engineer loads", False, f"err={e}")

            # 7b. Per-account ensemble modes + sub-model count check
            # members is list of (mdir, TradeOutcomePredictor) tuples;
            # check per-sub-model .enabled to catch silent degradation.
            for acct in ("B", "C", "D"):
                mode = os.environ.get(f"ML_ENSEMBLE_MODE_{acct}", "").strip().lower()
                if not mode:
                    continue
                thr = (os.environ.get(f"ML_ENSEMBLE_THRESH_{acct}", "").strip()
                       or os.environ.get("ML_ENSEMBLE_THRESH", "0.50").strip())
                check(f"ensemble {acct} mode set", bool(mode), f"mode={mode} thresh={thr}")
                try:
                    ep = EnsemblePredictor(model_dirs=ensemble_dirs, mode=mode,
                                            loss_threshold=0.99)
                    n_total = len(ep.members)
                    n_enabled = sum(1 for _, p in ep.members if p.enabled)
                    check(f"ensemble {acct} sub-models {n_enabled}/{n_total}",
                          n_enabled == n_total and n_total == n_dirs,
                          f"enabled={n_enabled}/{n_total} expected={n_dirs}")
                    if n_enabled != n_total:
                        names = [d for d, p in ep.members if not p.enabled]
                        print(f"INFO:DEGRADED: ensemble {acct} sub-models disabled: {names}")
                    ok, msg = ep.health_check()
                    check(f"ensemble {acct} health_check", ok, f"msg={msg}")
                    if ok:
                        proba, label = ep.predict_loss_proba({
                            "regime": "trending",
                            "direction": "BUY",
                            "session": "london",
                            "d1_trend": "bullish",
                            "h4_trend": "bullish",
                            "price_vs_cloud": "above",
                            "mfi_signal": "neutral",
                        })
                        is_num = isinstance(proba, (int, float)) and not isinstance(proba, bool)
                        check(f"ensemble {acct} prediction numeric", is_num,
                              f"proba={proba!r} label={label!r}")
                except Exception as e:
                    check(f"ensemble {acct} load", False, f"exception={e}")
    except Exception as e:
        check("ensemble import", False, f"exception={e}")

for name, ok, detail in checks:
    print(f"CHECK:{name}:{'PASS' if ok else 'FAIL'}:{detail}")
PYEOF
)

    # Parse CHECK: lines
    while IFS= read -r line; do
        if [[ "$line" == CHECK:* ]]; then
            local name status detail
            name=$(echo "$line" | cut -d: -f2)
            status=$(echo "$line" | cut -d: -f3)
            detail=$(echo "$line" | cut -d: -f4-)
            if [ "$status" = "PASS" ]; then
                pass "$name — $detail"
            else
                fail "$name — $detail"
            fi
        fi
    done <<< "$result"

    echo ""
}

# ─── Select containers ─────────────────────────────────────────────────
if [ $# -gt 0 ]; then
    CONTAINERS="$@"
else
    CONTAINERS="oracle-engine oracle-engine-train"
fi

echo -e "${CYAN}Post-deploy ML verification${NC}"
echo -e "${CYAN}VPS: $VPS${NC}"
echo ""

for c in $CONTAINERS; do
    verify_container "$c"
done

# ─── Summary ───────────────────────────────────────────────────────────
echo -e "${CYAN}═══════════════════════════════════════════════════════${NC}"
echo -e "${CYAN}  SUMMARY${NC}"
echo -e "${CYAN}═══════════════════════════════════════════════════════${NC}"
echo -e "  Passes: ${GREEN}$PASSES${NC}"
echo -e "  Fails:  ${RED}$FAILS${NC}"
echo ""

if [ "$FAILS" -gt 0 ]; then
    echo -e "${RED}❌ DEPLOY VERIFICATION FAILED — do not trust this deploy${NC}"
    echo -e "  Common causes:"
    echo -e "    • feature_engineer.joblib references a module not in the image (e.g., bxau)"
    echo -e "    • training_results.json missing — predictor silently disables ML filter"
    echo -e "    • sklearn/xgboost version mismatch — model files fail to load"
    echo -e "    • ML_MODEL_DIR env var wrong or path not mounted"
    exit 1
else
    echo -e "${GREEN}✅ DEPLOY VERIFIED — ML filter active and predicting${NC}"
    exit 0
fi