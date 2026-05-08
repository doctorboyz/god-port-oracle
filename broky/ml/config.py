"""ML training configuration — all hyperparameters in one place."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class MLConfig:
    """XGBoost training configuration."""

    # Experiment identity
    experiment_name: str = "xgboost_v1"
    description: str = ""

    # Label parameters
    horizon_bars: int = 12       # 1 hour on M5
    threshold_pct: float = 0.15  # ~$3 on $2000 gold

    # Train/test split
    test_ratio: float = 0.2

    # XGBoost hyperparameters
    objective: str = "multi:softprob"
    num_class: int = 3           # UP/FLAT/DOWN
    max_depth: int = 6
    learning_rate: float = 0.1
    n_estimators: int = 200
    subsample: float = 0.8
    colsample_bytree: float = 0.8
    min_child_weight: int = 5
    gamma: float = 0.1
    reg_alpha: float = 0.0
    reg_lambda: float = 1.0

    # Cross-validation
    cv_folds: int = 5

    # Minimum samples to train
    min_samples: int = 200

    # Feature selection
    group_filter: Optional[str] = None
    feature_selection: Optional[list[str]] = None

    # Paths
    model_dir: str = "models"

    def to_dict(self) -> dict:
        """Serialize config to dict."""
        return {
            k: v for k, v in self.__dict__.items()
            if not k.startswith("_")
        }

    @classmethod
    def from_dict(cls, d: dict) -> "MLConfig":
        """Deserialize config from dict."""
        return cls(**{k: v for k, v in d.items() if k in cls.__dict__})