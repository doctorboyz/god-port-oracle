#!/usr/bin/env python3
"""Train V12 BUY models with Optuna + session-aware features.

This script trains ONLY BUY direction models using V12 features:
- Session cyclical features (hour_sin, hour_cos, day_of_week_sin, day_of_week_cos)
- Candle pattern features (close_position, body_ratio, direction_streak)
- Multi-TF alignment features (h1_h4_aligned, h4_d1_aligned, all_tf_aligned)
- Combo features (rsi_adx_combo, ema_cross_volume, boll_rsi_combo)
- Volatility features (rolling_sharpe_20, vol_of_vol_20)

Uses Optuna hyperparameter optimization (200 trials per model).

Outputs to: data/models/trade_outcome_v12_buy/

Usage:
    python -m scripts.train_v12_buy_models
    python -m scripts.train_v12_buy_models --trials 100
    python -m scripts.train_v12_buy_models --no-optuna  # Use default params
"""

import argparse
import logging
import shutil
from pathlib import Path

from broky.ml.trade_outcome_trainer import TradeOutcomeConfig, TradeOutcomeTrainer

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = PROJECT_ROOT / "data" / "oracle.db"
V12_BUY_DIR = PROJECT_ROOT / "data" / "models" / "trade_outcome_v12_buy"
MIXED_V12_DIR = PROJECT_ROOT / "data" / "models" / "trade_outcome_mixed_v12"


def train_v12_buy_models(db_path: Path, optuna_trials: int, use_optuna: bool) -> None:
    """Train V12 BUY models with session-aware features."""
    config = TradeOutcomeConfig(
        experiment_name="trade_outcome_v12_buy",
        description="V12 BUY models with session cyclical + candle pattern + multi-TF alignment features",
        feature_set="direction_specific",  # Use BUY_TOP_FEATURES (now with V12 features)
        min_confidence=0.45,
        min_samples=100,
        regime_specific=True,
        direction_specific=True,
        model_type="xgb",
        use_optuna=use_optuna,
        optuna_trials=optuna_trials,
        live_weight=3.0,
        exclude_phantom=True,
        exclude_low_confidence=True,
        train_directions=["BUY"],  # ONLY train BUY models
        model_dir=str(V12_BUY_DIR),
    )

    logger.info("=" * 60)
    logger.info("V12 BUY Model Training")
    logger.info("=" * 60)
    logger.info("Feature set: direction_specific (BUY_TOP_FEATURES with V12 additions)")
    logger.info("Optuna: %s (trials=%d)", "enabled" if use_optuna else "disabled", optuna_trials)
    logger.info("Directions: BUY only")
    logger.info("Output: %s", V12_BUY_DIR)
    logger.info("=" * 60)

    trainer = TradeOutcomeTrainer(config)
    results = trainer.train(db_path=db_path)

    # Print results
    logger.info("\n" + "=" * 60)
    logger.info("V12 BUY Training Results")
    logger.info("=" * 60)
    for r in results:
        logger.info(
            "%-25s  acc=%.3f  cv=%.3f±%.3f  WR=%.3f  PF=%.2f  n=%d",
            r.name, r.test_accuracy, r.cv_accuracy, r.cv_std,
            r.win_rate, r.profit_factor, r.n_samples,
        )

    # Copy BUY models to mixed_v12 directory
    logger.info("\nCopying BUY models to mixed V12 directory: %s", MIXED_V12_DIR)
    MIXED_V12_DIR.mkdir(parents=True, exist_ok=True)
    for model_file in V12_BUY_DIR.glob("*_BUY_*"):
        dest = MIXED_V12_DIR / model_file.name
        shutil.copy2(model_file, dest)
        logger.info("  Copied %s → %s", model_file.name, dest.name)

    # Also copy training_results.json
    results_src = V12_BUY_DIR / "training_results.json"
    if results_src.exists():
        results_dest = MIXED_V12_DIR / "v12_buy_training_results.json"
        shutil.copy2(results_src, results_dest)
        logger.info("  Copied training_results.json → v12_buy_training_results.json")

    logger.info("\nV12 BUY models ready in: %s", V12_BUY_DIR)
    logger.info("Mixed V12 directory updated: %s", MIXED_V12_DIR)

    # Print model comparison summary
    buy_results = [r for r in results if "BUY" in r.name.upper()]
    if buy_results:
        logger.info("\n" + "=" * 60)
        logger.info("V12 BUY Models Summary")
        logger.info("=" * 60)
        for r in buy_results:
            logger.info(
                "  %-25s  test_acc=%.3f  cv=%.3f±%.3f  WR=%.1f%%  PF=%.2f",
                r.name, r.test_accuracy, r.cv_accuracy, r.cv_std,
                r.win_rate * 100, r.profit_factor,
            )
            if r.feature_importance:
                top5 = list(r.feature_importance.items())[:5]
                logger.info("    Top features: %s", ", ".join(f"{k}({v:.3f})" for k, v in top5))


def main() -> None:
    parser = argparse.ArgumentParser(description="Train V12 BUY models")
    parser.add_argument("--db", type=str, default=str(DEFAULT_DB),
                        help="Path to SQLite database")
    parser.add_argument("--trials", type=int, default=200,
                        help="Number of Optuna trials per model")
    parser.add_argument("--no-optuna", action="store_true",
                        help="Disable Optuna (use default XGBoost params)")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )

    db_path = Path(args.db)
    if not db_path.exists():
        logger.error("Database not found: %s", db_path)
        return

    train_v12_buy_models(
        db_path=db_path,
        optuna_trials=args.trials,
        use_optuna=not args.no_optuna,
    )


if __name__ == "__main__":
    main()