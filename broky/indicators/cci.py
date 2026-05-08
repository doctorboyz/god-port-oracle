"""CCI (Commodity Channel Index) calculation.

Pure function: takes HLC series, returns CCI values.
Above +100 = overbought, below -100 = oversold.
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def calculate_cci(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    period: int = 20,
) -> pd.Series:
    """Calculate CCI from HLC data.

    CCI measures deviation from statistical mean:
    - Above +100: overbought (price well above average)
    - Below -100: oversold (price well below average)

    Args:
        high: Series of high prices.
        low: Series of low prices.
        close: Series of close prices.
        period: CCI period (default 20).

    Returns:
        Series of CCI values. First `period-1` values are NaN.

    Example:
        >>> cci = calculate_cci(high, low, close, period=20)
        >>> overbought = cci > 100
    """
    typical_price = (high + low + close) / 3
    sma_tp = typical_price.rolling(window=period).mean()

    mean_deviation = typical_price.rolling(window=period).apply(
        lambda x: np.abs(x - x.mean()).mean(), raw=True,
    )

    cci = (typical_price - sma_tp) / (0.015 * mean_deviation.replace(0, pd.NA))

    return cci