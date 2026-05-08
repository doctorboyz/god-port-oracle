"""ML Predictor — load trained model, predict direction + confidence."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

from broky.ml.labels import UP, FLAT, DOWN

logger = logging.getLogger(__name__)


class MLPredictor:
    """Load a trained XGBoost model and make predictions on feature snapshots."""

    def __init__(self, model_dir: str | Path):
        self.model_dir = Path(model_dir)
        self.model = None
        self.engineer = None
        self.config = None
        self._loaded = False

    def load(self) -> None:
        """Load model, feature engineer, and config from disk."""
        try:
            import xgboost as xgb
        except ImportError:
            raise ImportError("xgboost not installed. Run: pip install xgboost")

        import joblib

        model_path = self.model_dir / "model.json"
        if not model_path.exists():
            raise FileNotFoundError(f"Model not found: {model_path}")

        self.model = xgb.XGBClassifier()
        self.model.load_model(str(model_path))

        engineer_path = self.model_dir / "feature_engineer.joblib"
        if engineer_path.exists():
            self.engineer = joblib.load(str(engineer_path))

        config_path = self.model_dir / "config.json"
        if config_path.exists():
            self.config = json.loads(config_path.read_text())

        self._loaded = True
        logger.info("Loaded model from %s", self.model_dir)

    def predict(self, snapshot: dict[str, float | str]) -> dict:
        """Predict direction and confidence from a feature snapshot dict.

        Args:
            snapshot: Dict of indicator name -> value (same format as feature_snapshots).

        Returns:
            Dict with: direction (UP/FLAT/DOWN), confidence (0-1),
            probabilities {UP, FLAT, DOWN}, class (0/1/2).
        """
        if not self._loaded:
            self.load()

        # Convert snapshot to DataFrame
        df = pd.DataFrame([snapshot])

        # Apply feature engineering
        if self.engineer is not None:
            df_transformed = self.engineer.transform(df)
            feature_cols = self.engineer.get_feature_columns(df_transformed)
        else:
            feature_cols = [c for c in df.columns if c in self._numeric_columns()]
            df_transformed = df

        X = df_transformed[feature_cols]

        # Predict
        pred_class = int(self.model.predict(X)[0])
        proba = self.model.predict_proba(X)[0]

        direction_map = {DOWN: "DOWN", FLAT: "FLAT", UP: "UP"}
        direction = direction_map.get(pred_class, "FLAT")
        confidence = float(proba.max())

        return {
            "direction": direction,
            "class": pred_class,
            "confidence": confidence,
            "probabilities": {
                "DOWN": float(proba[0]),
                "FLAT": float(proba[1]),
                "UP": float(proba[2]),
            },
        }

    def predict_batch(self, snapshots: list[dict]) -> list[dict]:
        """Predict for multiple snapshots at once."""
        if not self._loaded:
            self.load()

        df = pd.DataFrame(snapshots)

        if self.engineer is not None:
            df_transformed = self.engineer.transform(df)
            feature_cols = self.engineer.get_feature_columns(df_transformed)
        else:
            feature_cols = [c for c in df.columns if c in self._numeric_columns()]
            df_transformed = df

        X = df_transformed[feature_cols]
        preds = self.model.predict(X)
        probas = self.model.predict_proba(X)

        direction_map = {DOWN: "DOWN", FLAT: "FLAT", UP: "UP"}
        results = []
        for i in range(len(preds)):
            results.append({
                "direction": direction_map.get(int(preds[i]), "FLAT"),
                "class": int(preds[i]),
                "confidence": float(probas[i].max()),
                "probabilities": {
                    "DOWN": float(probas[i][0]),
                    "FLAT": float(probas[i][1]),
                    "UP": float(probas[i][2]),
                },
            })

        return results

    def _numeric_columns(self) -> set[str]:
        """Fallback: get numeric feature columns from model."""
        from broky.ml.features import ALL_NUMERIC_FEATURES
        return set(ALL_NUMERIC_FEATURES)