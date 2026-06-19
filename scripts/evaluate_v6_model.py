#!/usr/bin/env python3
"""Evaluate v6 model performance with detailed calibration and profit factor analysis."""

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.calibration import CalibratedClassifierCV
from sklearn.metrics import brier_score_loss, log_loss
from sklearn.model_selection import TimeSeriesSplit

sys.path.insert(0, str(Path(__file__).parent.parent))

from broky.ml.trade_outcome_trainer import TradeOutcomeTrainer, TradeOutcomeConfig
from broky.ml.features import FeatureEngineer, ALL_FEATURE_COLS
from metty.core.db import get_connection


def evaluate_model_calibration(model, X_test, y_test, name="model"):
    """Evaluate calibration: predicted probability vs actual win rate."""
    y_proba = model.predict_proba(X_test)
    # Get P(LOSS) = P(class 0)
    loss_idx = list(model.classes_).index(0) if 0 in model.classes_ else 0
    p_loss = y_proba[:, loss_idx]
    p_win = 1 - p_loss

    # Bin predictions by decile
    bins = [0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0]
    print(f"\n  Calibration for {name}:")
    print(f"  {'P(WIN) bin':<15} {'Count':>6} {'Actual WR':>10} {'Mean P(WIN)':>12} {'Brier':>8}")
    for i in range(len(bins) - 1):
        mask = (p_win >= bins[i]) & (p_win < bins[i + 1])
        if mask.sum() == 0:
            continue
        actual_wr = y_test[mask].mean()
        mean_pred = p_win[mask].mean()
        brier = ((p_win[mask] - y_test[mask]) ** 2).mean()
        print(f"  [{bins[i]:.1f}-{bins[i+1]:.1f})     {mask.sum():>6} {actual_wr:>10.1%} {mean_pred:>12.3f} {brier:>8.4f}")

    # Brier score (lower is better)
    brier = brier_score_loss(y_test, p_win)
    ll = log_loss(y_test, p_win)
    print(f"  Overall: Brier={brier:.4f}, LogLoss={ll:.4f}")

    return p_win, p_loss


def evaluate_at_thresholds(p_win, y_test, thresholds=[0.55, 0.60, 0.65, 0.70, 0.75, 0.80]):
    """Evaluate model as trade filter at different P(WIN) thresholds."""
    print(f"\n  Trade filter analysis (skip if P(WIN) < threshold):")
    print(f"  {'Threshold':>10} {'Trades':>7} {'Skipped':>8} {'Skip%':>7} {'WR_taken':>9} {'PF_taken':>9}")

    for thresh in thresholds:
        taken = p_win >= thresh
        n_taken = taken.sum()
        n_skipped = (~taken).sum()
        skip_pct = n_skipped / len(y_test) * 100

        if n_taken > 0:
            wr_taken = y_test[taken].mean()
            wins = (y_test[taken] == 1).sum()
            losses = (y_test[taken] == 0).sum()
            pf = wins / max(losses, 1)
        else:
            wr_taken = 0
            pf = 0

        print(f"  {thresh:>10.2f} {n_taken:>7} {n_skipped:>8} {skip_pct:>6.1f}% {wr_taken:>9.1%} {pf:>9.2f}")


def main():
    config = TradeOutcomeConfig(
        experiment_name="trade_outcome_v6",
        feature_set="extended",
        model_type="xgb",
        min_samples=50,
        regime_specific=True,
        direction_specific=True,
        xgb_max_depth=4,
        xgb_n_estimators=500,
        xgb_learning_rate=0.03,
        xgb_subsample=0.75,
        xgb_colsample_bytree=0.7,
        xgb_reg_lambda=3.0,
        xgb_min_child_weight=20,
        live_weight=3.0,
    )

    trainer = TradeOutcomeTrainer(config)
    df = trainer.load_data()
    print(f"Loaded {len(df)} rows")

    feature_cols = config.get_feature_cols()
    X, y, engineer, available_cols, sample_weights = trainer.prepare_features(df, feature_cols)

    # Focus on the most important models for evaluation
    focus_models = ["overall", "direction_BUY", "trending_BUY"]

    for model_name in focus_models:
        import joblib
        model_path = Path(f"data/models/trade_outcome_v6/{model_name}_model.pkl")
        if not model_path.exists():
            print(f"Model {model_name} not found, skipping")
            continue

        model = joblib.load(model_path)
        print(f"\n{'=' * 60}")
        print(f"  Evaluating: {model_name}")
        print(f"{'=' * 60}")

        # Time-series split for evaluation
        split_idx = int(len(X) * 0.8)
        X_test = X.iloc[split_idx:]
        y_test = y.iloc[split_idx:]

        # Calibration
        p_win, p_loss = evaluate_model_calibration(model, X_test, y_test, model_name)

        # Trade filter analysis
        evaluate_at_thresholds(p_win, y_test)


if __name__ == "__main__":
    main()