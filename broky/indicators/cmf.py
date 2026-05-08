"""CMF (Chaikin Money Flow) calculation.

Pure function: takes OHLCV data, returns CMF values.
CMF = sum(AD, period) / sum(volume, period) — measures accumulation/distribution
relative to volume over a lookback window.
"""

from __future__ import annotations

import pandas as pd


def calculate_cmf(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    volume: pd.Series,
    period: int = 20,
) -> pd.Series:
    """Calculate Chaikin Money Flow from OHLCV data.

    CMF = rolling_sum(CLV * volume, period) / rolling_sum(volume, period)
    where CLV = (2*close - high - low) / (high - low).

    Args:
        high: Series of high prices.
        low: Series of low prices.
        close: Series of close prices.
        volume: Series of volume values.
        period: CMF period (default 20).

    Returns:
        Series of CMF values (range roughly -1 to +1).
        First `period` values are NaN. NaN where high == low or
        rolling volume sum is zero.

    Example:
        >>> cmf = calculate_cmf(high, low, close, volume, period=20)
        >>> accumulation = cmf > 0  # Positive CMF = buying pressure
    """
    price_range = high - low
    clv = (2 * close - high - low) / price_range.replace(0, pd.NA)
    money_flow_volume = clv * volume

    mf_sum = money_flow_volume.rolling(window=period).sum()
    vol_sum = volume.rolling(window=period).sum()

    cmf = mf_sum / vol_sum.replace(0, pd.NA)
    return cmf