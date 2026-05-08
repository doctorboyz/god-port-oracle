"""XGBoost trainer — train, evaluate, and save models."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

from broky.ml.config import MLConfig
from broky.ml.dataset import MLDataset, TrainTestSplit
from metty.core.db import insert_ml_experiment, update_ml_experiment

logger = logging.getLogger(__name__)


class XGBoostTrainer:
    """Train XGBoost models for 3-class price direction prediction."""

    def __init__(self, config: Optional[MLConfig] = None):
        self.config = config or MLConfig()

    def train(self, db_path: Optional[Path] = None) -> dict:
        """Run full training pipeline: load → engineer → label → train → evaluate.

        Returns dict with metrics and model path.
        """
        try:
            import xgboost as xgb
        except ImportError:
            raise ImportError("xgboost not installed. Run: pip install xgboost")

        # Load and prepare data
        dataset = MLDataset(
            db_path=db_path,
            horizon_bars=self.config.horizon_bars,
            threshold_pct=self.config.threshold_pct,
            test_ratio=self.config.test_ratio,
        )
        split = dataset.prepare(group_filter=self.config.group_filter)

        if len(split.X_train) < self.config.min_samples:
            raise ValueError(
                f"Training data too small: {len(split.X_train)} (need {self.config.min_samples})"
            )

        # Create experiment record
        import json as _json
        experiment_id = insert_ml_experiment(
            name=self.config.experiment_name,
            config=_json.dumps(self.config.to_dict()),
            feature_columns=_json.dumps(split.feature_columns),
            min_samples=self.config.min_samples,
            description=self.config.description,
            group_filter=self.config.group_filter,
            db_path=db_path,
        )
        logger.info("Experiment %d: %s", experiment_id, self.config.experiment_name)

        # Train XGBoost
        logger.info("Training XGBoost on %d samples...", len(split.X_train))
        model = xgb.XGBClassifier(
            objective=self.config.objective,
            num_class=self.config.num_class,
            max_depth=self.config.max_depth,
            learning_rate=self.config.learning_rate,
            n_estimators=self.config.n_estimators,
            subsample=self.config.subsample,
            colsample_bytree=self.config.colsample_bytree,
            min_child_weight=self.config.min_child_weight,
            gamma=self.config.gamma,
            reg_alpha=self.config.reg_alpha,
            reg_lambda=self.config.reg_lambda,
            eval_metric="mlogloss",
            early_stopping_rounds=30,
            verbosity=0,
        )

        # Compute sample weights to handle class imbalance
        from sklearn.utils.class_weight import compute_sample_weight
        classes = split.y_train.unique()
        sample_weights = compute_sample_weight("balanced", split.y_train)

        model.fit(
            split.X_train, split.y_train,
            sample_weight=sample_weights,
            eval_set=[(split.X_test, split.y_test)],
            verbose=False,
        )

        # Evaluate
        from broky.ml.evaluator import ModelEvaluator
        evaluator = ModelEvaluator(model, split)
        metrics = evaluator.evaluate()

        # Save model
        model_dir = Path(self.config.model_dir) / self.config.experiment_name
        model_dir.mkdir(parents=True, exist_ok=True)
        model_path = model_dir / "model.json"
        model.save_model(str(model_path))

        # Save config
        config_path = model_dir / "config.json"
        config_path.write_text(json.dumps(self.config.to_dict(), indent=2))

        # Save feature engineer
        import joblib
        engineer_path = model_dir / "feature_engineer.joblib"
        joblib.dump(dataset.engineer, str(engineer_path))

        logger.info("Model saved to %s", model_path)
        logger.info("Metrics: accuracy=%.2f%%, profit_factor=%.2f",
                    metrics.get("accuracy", 0) * 100, metrics.get("profit_factor", 0))

        # Update experiment record
        update_ml_experiment(
            experiment_id=experiment_id,
            status="completed",
            results=json.dumps(metrics),
            model_path=str(model_dir),
            win_rate=metrics.get("accuracy"),
            profit_factor=metrics.get("profit_factor"),
            total_trades=metrics.get("total_trades"),
            db_path=db_path,
        )

        return {
            "experiment_id": experiment_id,
            "model_path": str(model_dir),
            "metrics": metrics,
        }


def main():
    """CLI entry point for training."""
    import argparse
    import logging

    parser = argparse.ArgumentParser(description="Train XGBoost model")
    parser.add_argument("--experiment", default="xgboost_v1", help="Experiment name")
    parser.add_argument("--min-samples", type=int, default=200, help="Min training samples")
    parser.add_argument("--horizon", type=int, default=12, help="Label horizon (M5 bars)")
    parser.add_argument("--threshold", type=float, default=0.15, help="Label threshold (%)")
    parser.add_argument("--max-depth", type=int, default=6, help="XGBoost max_depth")
    parser.add_argument("--lr", type=float, default=0.1, help="XGBoost learning_rate")
    parser.add_argument("--n-est", type=int, default=200, help="XGBoost n_estimators")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    config = MLConfig(
        experiment_name=args.experiment,
        min_samples=args.min_samples,
        horizon_bars=args.horizon,
        threshold_pct=args.threshold,
        max_depth=args.max_depth,
        learning_rate=args.lr,
        n_estimators=args.n_est,
    )

    trainer = XGBoostTrainer(config)
    result = trainer.train()

    print(f"\n=== Training Complete ===")
    print(f"Model: {result['model_path']}")
    m = result["metrics"]
    print(f"Accuracy: {m.get('accuracy', 0):.1%}")
    print(f"Profit Factor: {m.get('profit_factor', 0):.2f}")
    print(f"Total Trades: {m.get('total_trades', 0)}")


if __name__ == "__main__":
    main()