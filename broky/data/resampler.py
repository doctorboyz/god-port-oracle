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
        df: DataFrame with OHLCV columns and DatetimeIndex (or 'timestamp' column).
            Accepts both Title Case (Open, High, Low, Close, Volume) and
            lowercase (open, high, low, close, volume) column names.
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

    # Work on a copy
    df = df.copy()

    # Ensure DatetimeIndex
    if not isinstance(df.index, pd.DatetimeIndex):
        for col in ("timestamp", "time", "Date", "date"):
            if col in df.columns:
                df[col] = pd.to_datetime(df[col])
                df = df.set_index(col)
                break

    # Normalize column names to Title Case for resampling
    col_map = {}
    for lower, title in [("open", "Open"), ("high", "High"), ("low", "Low"),
                         ("close", "Close"), ("volume", "Volume")]:
        if lower in df.columns and title not in df.columns:
            col_map[lower] = title
    if col_map:
        df = df.rename(columns=col_map)

    # Only resample columns that exist
    agg_map = {}
    for col, func in [("Open", "first"), ("High", "max"), ("Low", "min"),
                      ("Close", "last"), ("Volume", "sum")]:
        if col in df.columns:
            agg_map[col] = func

    resampled = df.resample(pandas_freq).agg(agg_map).dropna()

    # Restore original column name casing if we renamed
    reverse_map = {v: k for k, v in col_map.items()}
    if reverse_map:
        resampled = resampled.rename(columns=reverse_map)

    return resampled