"""Model evaluator — metrics, classification reports, profit factor simulation."""

from __future__ import annotations

import logging
from typing import Optional

import numpy as np
import pandas as pd

from broky.ml.labels import UP, FLAT, DOWN

logger = logging.getLogger(__name__)


class ModelEvaluator:
    """Evaluate trained XGBoost models on test data."""

    def __init__(self, model, split):
        self.model = model
        self.split = split

    def evaluate(self) -> dict[str, float | int | dict]:
        """Compute all evaluation metrics."""
        X_test = self.split.X_test
        y_test = self.split.y_test

        y_pred = self.model.predict(X_test)
        y_proba = self.model.predict_proba(X_test)

        accuracy = float((y_pred == y_test).mean())

        # Per-class metrics
        class_metrics = self._per_class_metrics(y_test, y_pred)

        # Profit factor simulation
        pf_result = self._profit_factor_simulation(y_test, y_pred, y_proba)

        # Feature importance
        feature_importance = self._feature_importance()

        return {
            "accuracy": accuracy,
            "class_metrics": class_metrics,
            "profit_factor": pf_result["profit_factor"],
            "total_trades": pf_result["total_trades"],
            "win_rate": pf_result["win_rate"],
            "avg_win": pf_result["avg_win"],
            "avg_loss": pf_result["avg_loss"],
            "feature_importance": feature_importance,
        }

    def _per_class_metrics(self, y_true: pd.Series, y_pred: np.ndarray) -> dict:
        """Compute precision, recall, F1 per class."""
        result = {}
        for label, name in [(DOWN, "DOWN"), (FLAT, "FLAT"), (UP, "UP")]:
            true_mask = (y_true == label)
            pred_mask = (y_pred == label)

            tp = int((true_mask & pred_mask).sum())
            fp = int((~true_mask & pred_mask).sum())
            fn = int((true_mask & ~pred_mask).sum())

            precision = tp / (tp + fp) if (tp + fp) > 0 else 0.0
            recall = tp / (tp + fn) if (tp + fn) > 0 else 0.0
            f1 = 2 * precision * recall / (precision + recall) if (precision + recall) > 0 else 0.0

            result[name] = {"precision": precision, "recall": recall, "f1": f1, "support": int(true_mask.sum())}

        return result

    def _profit_factor_simulation(self, y_true: pd.Series, y_pred: np.ndarray, y_proba: np.ndarray) -> dict:
        """Simulate trades based on model predictions to compute profit factor.

        Rules:
        - Only trade when model predicts UP or DOWN (skip FLAT)
        - Confidence threshold: max probability > 1/num_classes (better than random)
        - Correct direction = win, wrong direction = loss
        - Risk 1% per trade, reward proportional to confidence
        """
        wins = []
        losses = []
        random_baseline = 1.0 / len(y_proba[0]) if len(y_proba) > 0 else 0.33

        for i in range(len(y_pred)):
            pred = y_pred[i]
            actual = y_true.iloc[i]

            if pred == FLAT:
                continue

            # Confidence = max probability (must beat random baseline)
            confidence = float(y_proba[i].max())
            if confidence <= random_baseline:
                continue

            # Determine if prediction was correct
            if pred == actual:
                # Win: gain proportional to confidence
                wins.append(confidence)
            else:
                # Loss: lose 1% (base risk)
                losses.append(1.0)

        total_wins = sum(wins)
        total_losses = sum(losses) if losses else 0.001  # Avoid division by zero
        profit_factor = total_wins / total_losses if total_losses > 0 else float("inf")
        total_trades = len(wins) + len(losses)
        win_rate = len(wins) / total_trades if total_trades > 0 else 0.0
        avg_win = total_wins / len(wins) if wins else 0.0
        avg_loss = total_losses / len(losses) if losses else 0.0

        return {
            "profit_factor": profit_factor,
            "total_trades": total_trades,
            "win_rate": win_rate,
            "avg_win": avg_win,
            "avg_loss": avg_loss,
        }

    def _feature_importance(self, top_n: int = 20) -> dict[str, float]:
        """Return top N features by importance."""
        try:
            importances = self.model.feature_importances_
            features = self.split.feature_columns
            if len(features) != len(importances):
                # Feature names may not align exactly
                return {}
            sorted_idx = np.argsort(importances)[::-1][:top_n]
            return {
                features[i]: float(importances[i])
                for i in sorted_idx
                if i < len(features)
            }
        except Exception:
            return {}