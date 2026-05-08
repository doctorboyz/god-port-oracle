"""MFI (Money Flow Index) calculation.

Pure function: takes OHLCV data, returns MFI values (0-100).
MFI is a volume-weighted RSI — oscillates between 0 and 100.
"""

from __future__ import annotations

import pandas as pd


def calculate_mfi(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    volume: pd.Series,
    period: int = 14,
) -> pd.Series:
    """Calculate Money Flow Index from OHLCV data.

    Args:
        high: Series of high prices.
        low: Series of low prices.
        close: Series of close prices.
        volume: Series of volume values.
        period: MFI period (default 14).

    Returns:
        Series of MFI values (0-100). First `period` values are NaN.

    Example:
        >>> mfi = calculate_mfi(high, low, close, volume, period=14)
        >>> overbought = mfi > 80  # MFI above 80 = overbought
    """
    typical_price = (high + low + close) / 3
    raw_money_flow = typical_price * volume

    # Positive MF: days where typical price rose
    positive_mf = raw_money_flow.where(typical_price > typical_price.shift(1), 0.0)
    negative_mf = raw_money_flow.where(typical_price < typical_price.shift(1), 0.0)

    positive_mf_sum = positive_mf.rolling(window=period).sum()
    negative_mf_sum = negative_mf.rolling(window=period).sum()

    money_flow_ratio = positive_mf_sum / negative_mf_sum.replace(0, pd.NA)
    mfi = 100 - (100 / (1 + money_flow_ratio))
    return mfi