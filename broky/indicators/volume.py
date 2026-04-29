"""Volume analysis — simple moving average of volume for confirmation."""

from __future__ import annotations

import pandas as pd


def calculate_volume_ma(volume: pd.Series, period: int = 20) -> pd.Series:
    """Calculate volume moving average for confirmation.

    Args:
        volume: Series of volume values.
        period: Moving average period (default 20).

    Returns:
        Series of volume MA values.

    Example:
        >>> vol_ma = calculate_volume_ma(volume, period=20)
        >>> high_volume = volume > vol_ma * 1.5  # Unusual volume
    """
    return volume.rolling(window=period).mean()


def calculate_volume_ratio(volume: pd.Series, period: int = 20) -> pd.Series:
    """Calculate current volume relative to moving average.

    Args:
        volume: Series of volume values.
        period: Moving average period (default 20).

    Returns:
        Series of volume ratios (>1 means above average volume).

    Example:
        >>> ratio = calculate_volume_ratio(volume)
        >>> confirmed = ratio > 1.0  # Signal confirmed by above-average volume
    """
    vol_ma = calculate_volume_ma(volume, period)
    return volume / vol_ma.replace(0, pd.NA)