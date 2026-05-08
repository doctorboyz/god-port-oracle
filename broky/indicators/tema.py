"""TEMA (Triple Exponential Moving Average) calculation.

Pure function: takes a close price series, returns TEMA values.
TEMA further reduces lag compared to DEMA by applying triple smoothing.
"""

from __future__ import annotations

import pandas as pd


def calculate_tema(close: pd.Series, period: int = 21) -> pd.Series:
    """Calculate TEMA from a close price series.

    TEMA = 3*EMA - 3*EMA(EMA) + EMA(EMA(EMA))

    Args:
        close: Series of close prices.
        period: TEMA period (default 21).

    Returns:
        Series of TEMA values. First `period-1` values may be NaN.

    Example:
        >>> tema = calculate_tema(close, period=21)
        >>> bullish = close > tema
    """
    ema1 = close.ewm(span=period, adjust=False).mean()
    ema2 = ema1.ewm(span=period, adjust=False).mean()
    ema3 = ema2.ewm(span=period, adjust=False).mean()
    return 3 * ema1 - 3 * ema2 + ema3