"""Label computation for ML pipeline — 3-class (UP/FLAT/DOWN) and binary labeling."""

from __future__ import annotations

import logging
from typing import Optional

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

# Label classes
UP = 2     # Price went up > threshold
FLAT = 1   # Price stayed within threshold
DOWN = 0   # Price went down > threshold


def compute_labels(
    df: pd.DataFrame,
    price_col: str = "price",
    timestamp_col: str = "timestamp",
    horizon_bars: int = 12,
    threshold_pct: float = 0.15,
) -> pd.Series:
    """Compute 3-class labels (UP/FLAT/DOWN) based on future price movement.

    For each row, look ahead `horizon_bars` rows and compute the return.
    - Return > threshold_pct → UP (class 2)
    - Return < -threshold_pct → DOWN (class 0)
    - Otherwise → FLAT (class 1)

    Args:
        df: DataFrame sorted by timestamp with price column.
        price_col: Column name for close price.
        timestamp_col: Column name for timestamp.
        horizon_bars: Number of bars to look ahead.
        threshold_pct: Minimum % move for UP/DOWN (e.g., 0.15 = $3 on $2000).

    Returns:
        Series of integer labels (0=DOWN, 1=FLAT, 2=UP). Last horizon_bars rows = NaN.
    """
    prices = df[price_col].values
    n = len(prices)
    labels = np.full(n, np.nan)

    for i in range(n - horizon_bars):
        future_price = prices[i + horizon_bars]
        current_price = prices[i]
        if current_price == 0:
            continue
        ret = ((future_price - current_price) / current_price) * 100

        if ret > threshold_pct:
            labels[i] = UP
        elif ret < -threshold_pct:
            labels[i] = DOWN
        else:
            labels[i] = FLAT

    return pd.Series(labels, index=df.index, name="label")


def compute_binary_labels(
    df: pd.DataFrame,
    price_col: str = "price",
    horizon_bars: int = 12,
    threshold_pct: float = 0.15,
) -> pd.Series:
    """Compute binary labels (UP=1, NOT_UP=0) — useful for simpler models.

    Same as compute_labels but collapsed: UP stays UP, everything else → NOT_UP.
    """
    labels = compute_labels(df, price_col, horizon_bars=horizon_bars, threshold_pct=threshold_pct)
    return (labels == UP).astype(int)


def compute_returns(
    df: pd.DataFrame,
    price_col: str = "price",
    horizon_bars: int = 12,
) -> pd.Series:
    """Compute raw forward returns for each row (useful for regression targets)."""
    prices = df[price_col].values
    n = len(prices)
    returns = np.full(n, np.nan)

    for i in range(n - horizon_bars):
        future_price = prices[i + horizon_bars]
        current_price = prices[i]
        if current_price == 0:
            continue
        returns[i] = ((future_price - current_price) / current_price) * 100

    return pd.Series(returns, index=df.index, name="forward_return")


def label_distribution(labels: pd.Series) -> dict[str, int]:
    """Return class distribution for labels."""
    valid = labels.dropna()
    return {
        "DOWN": int((valid == DOWN).sum()),
        "FLAT": int((valid == FLAT).sum()),
        "UP": int((valid == UP).sum()),
        "total": len(valid),
    }