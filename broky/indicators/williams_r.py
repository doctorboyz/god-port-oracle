"""Williams %R calculation.

Pure function: takes HLC series, returns Williams %R values.
Range: -100 (oversold) to 0 (overbought).
"""

from __future__ import annotations

import pandas as pd


def calculate_williams_r(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    period: int = 14,
) -> pd.Series:
    """Calculate Williams %R from HLC data.

    Williams %R measures overbought/oversold levels:
    - Below -80: oversold
    - Above -20: overbought

    Args:
        high: Series of high prices.
        low: Series of low prices.
        close: Series of close prices.
        period: Lookback period (default 14).

    Returns:
        Series of Williams %R values (-100 to 0).
        First `period-1` values are NaN.

    Example:
        >>> wr = calculate_williams_r(high, low, close, period=14)
        >>> oversold = wr < -80
    """
    highest_high = high.rolling(window=period).max()
    lowest_low = low.rolling(window=period).min()

    range_diff = (highest_high - lowest_low).replace(0, pd.NA)
    williams_r = ((highest_high - close) / range_diff) * -100

    return williams_r