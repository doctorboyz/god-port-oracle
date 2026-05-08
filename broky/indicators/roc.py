"""ROC (Rate of Change) calculation.

Pure function: takes a close price series, returns ROC values.
Measures percentage price change over a lookback period.
"""

from __future__ import annotations

import pandas as pd


def calculate_roc(close: pd.Series, period: int = 14) -> pd.Series:
    """Calculate Rate of Change from a close price series.

    ROC = ((close - close[period]) / close[period]) * 100

    Args:
        close: Series of close prices.
        period: Lookback period (default 14).

    Returns:
        Series of ROC values as percentage.
        First `period` values are NaN.

    Example:
        >>> roc = calculate_roc(close, period=14)
        >>> momentum_up = roc > 0
    """
    prev_close = close.shift(period)
    roc = ((close - prev_close) / prev_close.replace(0, pd.NA)) * 100
    return roc