"""VWAP (Volume Weighted Average Price) calculation.

Pure functions: VWAP and VWAP offset (% distance from VWAP).
VWAP = cumsum(typical_price * volume) / cumsum(volume).
"""

from __future__ import annotations

import pandas as pd


def calculate_vwap(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    volume: pd.Series,
) -> pd.Series:
    """Calculate Volume Weighted Average Price from OHLCV data.

    Note: This computes cumulative VWAP across the entire series.
    For intraday session-reset VWAP, reset the cumsum at session boundaries
    before calling this function.

    Args:
        high: Series of high prices.
        low: Series of low prices.
        close: Series of close prices.
        volume: Series of volume values.

    Returns:
        Series of VWAP values. NaN where cumulative volume is zero.

    Example:
        >>> vwap = calculate_vwap(high, low, close, volume)
        >>> above_vwap = close > vwap  # Bullish above VWAP
    """
    typical_price = (high + low + close) / 3
    cum_tp_vol = (typical_price * volume).cumsum()
    cum_vol = volume.cumsum()
    vwap = cum_tp_vol / cum_vol.replace(0, pd.NA)
    return vwap


def calculate_vwap_offset(close: pd.Series, vwap: pd.Series) -> pd.Series:
    """Calculate percentage distance of close price from VWAP.

    Args:
        close: Series of close prices.
        vwap: Series of VWAP values (from calculate_vwap).

    Returns:
        Series of percentage offsets. Positive = price above VWAP.
        NaN where VWAP is zero or NaN.

    Example:
        >>> vwap = calculate_vwap(high, low, close, volume)
        >>> offset = calculate_vwap_offset(close, vwap)
        >>> far_above = offset > 1.0  # Price >1% above VWAP
    """
    return ((close - vwap) / vwap.replace(0, pd.NA)) * 100