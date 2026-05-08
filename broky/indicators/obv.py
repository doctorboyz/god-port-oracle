"""OBV (On-Balance Volume) calculation.

Pure function: takes close and volume series, returns OBV values.
OBV adds volume on up days, subtracts on down days, unchanged on flat days.
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def calculate_obv(close: pd.Series, volume: pd.Series) -> pd.Series:
    """Calculate On-Balance Volume from close and volume series.

    Args:
        close: Series of close prices.
        volume: Series of volume values.

    Returns:
        Series of OBV values. First value equals first volume, then cumulative.

    Example:
        >>> obv = calculate_obv(close, volume)
        >>> rising_obv = obv.diff() > 0  # OBV rising = buying pressure
    """
    direction = np.sign(close.diff())
    # Price unchanged => direction 0, no volume contribution
    signed_volume = direction * volume
    obv = signed_volume.cumsum()
    # OBV starts from 0; first row has no diff so direction is NaN
    # Set first OBV value to 0 (or first volume if up-day convention preferred)
    # Standard: first value is 0, then accumulate
    obv.iloc[0] = 0.0
    return obv