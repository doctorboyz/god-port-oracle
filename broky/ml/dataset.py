"""ML Dataset — load snapshots from SQLite, label, and split for training."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

from broky.ml.features import FeatureEngineer, ALL_NUMERIC_FEATURES
from broky.ml.labels import compute_labels, label_distribution, UP, FLAT, DOWN
from metty.core.db import query_snapshots_for_training, get_snapshot_count

logger = logging.getLogger(__name__)


@dataclass
class TrainTestSplit:
    """Train/test split with feature matrices and label vectors."""
    X_train: pd.DataFrame
    X_test: pd.DataFrame
    y_train: pd.Series
    y_test: pd.Series
    feature_columns: list[str]
    label_distribution: dict


class MLDataset:
    """Load feature snapshots from SQLite, label, and split for ML training.

    Uses time-based 80/20 split (no random shuffle) to prevent look-ahead bias.
    """

    def __init__(
        self,
        db_path: Optional[Path] = None,
        horizon_bars: int = 12,
        threshold_pct: float = 0.15,
        test_ratio: float = 0.2,
    ):
        self.db_path = db_path
        self.horizon_bars = horizon_bars
        self.threshold_pct = threshold_pct
        self.test_ratio = test_ratio
        self.engineer = FeatureEngineer(fillna=True)

    def prepare(self, group_filter: Optional[str] = None) -> TrainTestSplit:
        """Load data, engineer features, compute labels, and split.

        Returns TrainTestSplit with X_train, X_test, y_train, y_test.
        """
        # Load raw snapshots
        logger.info("Loading snapshots from database...")
        rows = query_snapshots_for_training(
            min_samples=100,
            group_filter=group_filter,
            db_path=self.db_path,
        )
        if len(rows) < 100:
            raise ValueError(f"Not enough data: {len(rows)} rows (need 100+)")

        df = pd.DataFrame(rows)
        logger.info("Loaded %d snapshots", len(df))

        # Sort by timestamp
        df = df.sort_values("timestamp").reset_index(drop=True)

        # Engineer features
        logger.info("Engineering features...")
        self.engineer.fit(df)
        df_transformed = self.engineer.transform(df)
        feature_cols = self.engineer.get_feature_columns(df_transformed)

        # Compute labels (using price from snapshots, not future data)
        logger.info(
            "Computing labels (horizon=%d bars, threshold=%.2f%%)",
            self.horizon_bars, self.threshold_pct,
        )
        labels = compute_labels(
            df_transformed,
            price_col="price",
            horizon_bars=self.horizon_bars,
            threshold_pct=self.threshold_pct,
        )
        dist = label_distribution(labels)
        logger.info("Label distribution: %s", dist)

        # Drop rows with NaN labels (last horizon_bars)
        valid_mask = labels.notna()
        X = df_transformed.loc[valid_mask, feature_cols]
        y = labels.loc[valid_mask]

        # Time-based split (no shuffle — prevents look-ahead bias)
        split_idx = int(len(X) * (1 - self.test_ratio))
        X_train = X.iloc[:split_idx]
        X_test = X.iloc[split_idx:]
        y_train = y.iloc[:split_idx]
        y_test = y.iloc[split_idx:]

        logger.info(
            "Split: train=%d (%.0f%%), test=%d (%.0f%%)",
            len(X_train), (1 - self.test_ratio) * 100,
            len(X_test), self.test_ratio * 100,
        )
        logger.info("Train distribution: %s", label_distribution(y_train))
        logger.info("Test distribution: %s", label_distribution(y_test))

        return TrainTestSplit(
            X_train=X_train,
            X_test=X_test,
            y_train=y_train,
            y_test=y_test,
            feature_columns=feature_cols,
            label_distribution=dist,
        )