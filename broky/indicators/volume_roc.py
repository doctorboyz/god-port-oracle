"""Volume Rate of Change calculation.

Pure function: takes volume series, returns volume ROC as percentage.
Volume ROC measures the pace of volume change over a lookback period.
"""

from __future__ import annotations

import pandas as pd


def calculate_volume_roc(volume: pd.Series, period: int = 14) -> pd.Series:
    """Calculate Volume Rate of Change as a percentage.

    Args:
        volume: Series of volume values.
        period: Lookback period (default 14).

    Returns:
        Series of volume ROC values as percentage.
        First `period` values are NaN. Division by zero yields NaN.

    Example:
        >>> roc = calculate_volume_roc(volume, period=14)
        >>> volume_surge = roc > 50  # Volume >50% above 14-bar ago
    """
    previous_volume = volume.shift(period)
    return ((volume - previous_volume) / previous_volume.replace(0, pd.NA)) * 100