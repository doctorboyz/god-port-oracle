"""Resample OHLCV data between timeframes.

Convert M5 data to H1, H4, D1 etc. using standard OHLCV aggregation.
"""

from __future__ import annotations

import pandas as pd

from broky.data.loader import TIMEFRAME_MAP


def resample_timeframe(
    df: pd.DataFrame,
    target_timeframe: str,
) -> pd.DataFrame:
    """Resample OHLCV data to a higher timeframe.

    Args:
        df: DataFrame with OHLCV columns, DatetimeIndex.
        target_timeframe: Target timeframe (M15, H1, H4, D1).

    Returns:
        Resampled DataFrame with the same columns.

    Example:
        >>> m5 = load_timeframe("data/xau-data", "M5")
        >>> h1 = resample_timeframe(m5, "H1")
    """
    if target_timeframe not in TIMEFRAME_MAP:
        raise ValueError(f"Unknown timeframe: {target_timeframe}. Valid: {list(TIMEFRAME_MAP.keys())}")

    pandas_freq = TIMEFRAME_MAP[target_timeframe]

    resampled = df.resample(pandas_freq).agg({
        "Open": "first",
        "High": "max",
        "Low": "min",
        "Close": "last",
        "Volume": "sum",
    }).dropna()

    return resampled