"""RSI (Relative Strength Index) calculation.

Pure function: takes a price series, returns RSI values.
Standard Wilder's smoothing with configurable period.
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def calculate_rsi(close: pd.Series, period: int = 14) -> pd.Series:
    """Calculate RSI from a close price series.

    Args:
        close: Series of close prices.
        period: RSI period (default 14).

    Returns:
        Series of RSI values (0-100). First `period` values are NaN.

    Example:
        >>> close = pd.Series([44, 44.34, 44.09, 43.61, ...])
        >>> rsi = calculate_rsi(close, period=14)
    """
    delta = close.diff()
    gain = delta.where(delta > 0, 0.0)
    loss = (-delta).where(delta < 0, 0.0)

    avg_gain = gain.ewm(alpha=1 / period, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period).mean()

    rs = avg_gain / avg_loss.replace(0, np.nan)
    rsi = 100 - (100 / (1 + rs))
    return rsi