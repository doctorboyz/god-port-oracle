"""DEMA (Double Exponential Moving Average) calculation.

Pure function: takes a close price series, returns DEMA values.
DEMA reduces lag compared to standard EMA by applying double smoothing.
"""

from __future__ import annotations

import pandas as pd


def calculate_dema(close: pd.Series, period: int = 21) -> pd.Series:
    """Calculate DEMA from a close price series.

    DEMA = 2 * EMA(close, period) - EMA(EMA(close, period), period)

    Args:
        close: Series of close prices.
        period: DEMA period (default 21).

    Returns:
        Series of DEMA values. First `period-1` values may be NaN.

    Example:
        >>> dema = calculate_dema(close, period=21)
        >>> bullish = close > dema
    """
    ema1 = close.ewm(span=period, adjust=False).mean()
    ema2 = ema1.ewm(span=period, adjust=False).mean()
    return 2 * ema1 - ema2