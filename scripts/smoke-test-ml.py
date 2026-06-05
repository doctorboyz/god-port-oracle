#!/usr/bin/env python3
"""Deploy smoke test — verify ML model loading and prediction works.

Runs inside the Docker container (or locally with correct env vars).
Checks:
  1. sklearn version compatibility
  2. Model directory exists and contains expected files
  3. TradeOutcomePredictor loads all models (not silently disabled)
  4. health_check() passes with a test prediction
  5. Each sub-model can predict individually

Exit codes:
  0 — all checks passed
  1 — one or more checks failed

Usage:
  # On VPS (inside container):
  docker compose -f docker-compose.vps.yml exec oracle-engine python scripts/smoke-test-ml.py

  # Locally:
  python scripts/smoke-test-ml.py --model-dir data/models/v4
  python scripts/smoke-test-ml.py --model-dir data/models/trade_outcome_v4
"""

from __future__ import annotations

import json
import os
import sys
import traceback
from pathlib import Path

# Add project root to sys.path so broky/metty/shared are importable
_SCRIPT_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _SCRIPT_DIR.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

# ── Colors ──────────────────────────────────────────────────────────────────
RED = "\033[0;31m"
GREEN = "\033[0;32m"
YELLOW = "\033[1;33m"
NC = "\033[0m"

FAILURES = 0


def _fail(msg: str) -> None:
    global FAILURES
    FAILURES += 1
    print(f"  {RED}✗ FAIL{NC}: {msg}")


def _pass(msg: str) -> None:
    print(f"  {GREEN}✓ PASS{NC}: {msg}")


def _warn(msg: str) -> None:
    print(f"  {YELLOW}⚠ WARN{NC}: {msg}")


def _info(msg: str) -> None:
    print(f"  {msg}")


# ── Check 1: sklearn version ────────────────────────────────────────────────
def check_sklearn_version() -> str | None:
    """Verify sklearn version is compatible with trained models."""
    print("\n[1/5] Checking sklearn version compatibility...")
    try:
        import sklearn
        version = sklearn.__version__
        major, minor = [int(x) for x in version.split(".")[:2]]
        _info(f"sklearn version: {version}")

        # Models trained with sklearn 1.8.0 break on 1.9.0+ due to _loss module
        if major > 1 or (major == 1 and minor >= 9):
            _fail(f"sklearn {version} is >= 1.9.0 — models trained with 1.8.x will break "
                   "due to _loss module reorganization. Pin sklearn<1.9.0 in Dockerfile.")
            return version
        else:
            _pass(f"sklearn {version} is compatible (must be < 1.9.0)")
            return version
    except ImportError:
        _fail("sklearn not installed")
        return None


# ── Check 2: Model directory ────────────────────────────────────────────────
def check_model_dir(model_dir: Path) -> list[str] | None:
    """Verify model directory exists and contains expected files."""
    print("\n[2/5] Checking model directory...")
    if not model_dir.exists():
        _fail(f"Model directory not found: {model_dir}")
        return None

    _pass(f"Model directory exists: {model_dir}")

    # Check for training_results.json
    results_path = model_dir / "training_results.json"
    if not results_path.exists():
        _fail(f"training_results.json not found in {model_dir}")
        return None

    _pass(f"training_results.json found")

    # Parse model names
    try:
        with open(results_path) as f:
            results = json.load(f)
        model_names = [m["name"] for m in results.get("models", [])]
        _info(f"Expected models: {', '.join(model_names)}")

        # Check each model file exists
        missing = []
        for name in model_names:
            pkl_path = model_dir / f"{name}_model.pkl"
            if pkl_path.exists():
                size_kb = pkl_path.stat().st_size / 1024
                _pass(f"  {name}_model.pkl ({size_kb:.1f} KB)")
            else:
                _fail(f"  {name}_model.pkl not found")
                missing.append(name)

        # Check feature engineer
        engineer_path = model_dir / "feature_engineer.joblib"
        if engineer_path.exists():
            size_kb = engineer_path.stat().st_size / 1024
            _pass(f"feature_engineer.joblib ({size_kb:.1f} KB)")
        else:
            _fail("feature_engineer.joblib not found")

        if missing:
            _warn(f"Missing {len(missing)} model file(s): {', '.join(missing)}")
            return [n for n in model_names if n not in missing]

        return model_names
    except json.JSONDecodeError as e:
        _fail(f"training_results.json is invalid JSON: {e}")
        return None


# ── Check 3: TradeOutcomePredictor loads ────────────────────────────────────
def check_predictor_loads(model_dir: Path) -> object | None:
    """Verify TradeOutcomePredictor initializes and enables."""
    print("\n[3/5] Loading TradeOutcomePredictor...")
    try:
        from broky.ml.trade_outcome_predictor import TradeOutcomePredictor

        predictor = TradeOutcomePredictor(model_dir=str(model_dir))

        if predictor.enabled:
            loaded_count = len(predictor._models)
            _pass(f"Predictor enabled with {loaded_count} model(s)")
            _info(f"  Models loaded: {', '.join(predictor._models.keys())}")
            _info(f"  Loss threshold: {predictor.loss_threshold:.0%}")
            return predictor
        else:
            _fail("Predictor is DISABLED — no models loaded successfully")
            _warn("This usually means sklearn version mismatch or corrupted model files")
            _info(f"  Model dir: {model_dir}")
            # Try to give more detail
            if predictor._model_info:
                _info(f"  Model info entries: {list(predictor._model_info.keys())}")
            else:
                _warn("  No model info found — training_results.json may be missing")
            return None
    except Exception as e:
        _fail(f"Failed to import/create TradeOutcomePredictor: {e}")
        traceback.print_exc()
        return None


# ── Check 4: health_check() ─────────────────────────────────────────────────
def check_health(predictor: object) -> bool:
    """Run health_check() and verify prediction works."""
    print("\n[4/5] Running health_check()...")
    try:
        healthy, reason = predictor.health_check()
        if healthy:
            _pass(f"health_check: {reason}")
            return True
        else:
            _fail(f"health_check FAILED: {reason}")
            return False
    except Exception as e:
        _fail(f"health_check raised exception: {e}")
        traceback.print_exc()
        return False


# ── Check 5: Per-model prediction ───────────────────────────────────────────
def check_per_model_prediction(predictor: object) -> None:
    """Verify each loaded model can produce a prediction.

    Uses predict_loss_proba() which handles missing features via FeatureEngineer,
    then falls back through model hierarchy (regime_direction → direction → regime → overall).
    """
    print("\n[5/5] Testing prediction pipeline (regime x direction combinations)...")

    test_cases = [
        ("trending", "BUY"),
        ("trending", "SELL"),
        ("ranging", "BUY"),
        ("ranging", "SELL"),
        ("volatile", "BUY"),
        ("volatile", "SELL"),
    ]

    for regime, direction in test_cases:
        # Use the same minimal features as health_check — FeatureEngineer fills the rest
        test_features = {
            "rsi_14": 50.0, "macd": 0.0, "macd_signal": 0.0,
            "atr_14": 5.0, "bb_position": 0.5, "adx_14": 25.0,
            "session": "london", "d1_trend": "bullish",
            "h4_trend": "bullish", "spread": 0.3,
        }

        try:
            proba = predictor.predict_loss_proba(test_features, regime=regime, direction=direction)
            if proba is not None:
                _pass(f"  {regime}/{direction}: LOSS proba={proba:.3f}")
            else:
                _warn(f"  {regime}/{direction}: predict_loss_proba returned None")
        except Exception as e:
            _fail(f"  {regime}/{direction}: predict_loss_proba raised {type(e).__name__}: {e}")

    # Also test get_risk_multiplier (the actual entry point used by traders)
    print("\n  Testing get_risk_multiplier (trader entry point)...")
    for regime, direction in [("trending", "BUY"), ("ranging", "SELL")]:
        test_features = {
            "rsi_14": 50.0, "macd": 0.0, "macd_signal": 0.0,
            "atr_14": 5.0, "bb_position": 0.5, "adx_14": 25.0,
            "session": "london", "d1_trend": "bullish",
            "h4_trend": "bullish", "spread": 0.3,
        }
        try:
            result = predictor.get_risk_multiplier(test_features, regime, direction)
            if result is not None:
                multiplier, reason = result
                _pass(f"  {regime}/{direction}: risk={multiplier:.1f}x ({reason})")
            else:
                _fail(f"  {regime}/{direction}: get_risk_multiplier returned None")
        except Exception as e:
            _fail(f"  {regime}/{direction}: get_risk_multiplier raised {type(e).__name__}: {e}")


# ── Main ─────────────────────────────────────────────────────────────────────
def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="ML model deploy smoke test")
    parser.add_argument(
        "--model-dir",
        type=str,
        default=None,
        help="Path to model directory (default: ML_MODEL_DIR env var or data/models/v4)",
    )
    args = parser.parse_args()

    # Resolve model directory
    model_dir_str = args.model_dir or os.environ.get("ML_MODEL_DIR", "data/models/v4")
    model_dir = Path(model_dir_str)

    print("╔══════════════════════════════════════════════════════════╗")
    print("║          ML Model Deploy Smoke Test                     ║")
    print("╚══════════════════════════════════════════════════════════╝")
    print(f"  Model dir: {model_dir.resolve()}")

    # Also check xgboost version
    try:
        import xgboost
        _info(f"xgboost version: {xgboost.__version__}")
    except ImportError:
        _fail("xgboost not installed")

    # Run all checks
    check_sklearn_version()
    model_names = check_model_dir(model_dir)
    predictor = check_predictor_loads(model_dir)

    if predictor:
        check_health(predictor)
        check_per_model_prediction(predictor)

    # Summary
    print("\n" + "═" * 58)
    if FAILURES == 0:
        print(f"{GREEN}=== All ML smoke tests passed — deploy is safe ==={NC}")
        return 0
    else:
        print(f"{RED}=== {FAILURES} check(s) failed — DO NOT deploy ==={NC}")
        print(f"{YELLOW}Tip: If sklearn version failed, pin 'scikit-learn>=1.3.0,<1.9.0' in Dockerfile{NC}")
        print(f"{YELLOW}Tip: If models failed to load, check logs for '_loss' or 'No module' errors{NC}")
        return 1


if __name__ == "__main__":
    sys.exit(main())