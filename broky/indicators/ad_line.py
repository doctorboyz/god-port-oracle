"""Accumulation/Distribution Line calculation.

Pure functions: AD line and AD line slope.
AD line measures cumulative money flow volume.
AD line slope identifies trend direction of accumulation/distribution.
"""

from __future__ import annotations

import pandas as pd


def calculate_ad_line(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    volume: pd.Series,
) -> pd.Series:
    """Calculate Accumulation/Distribution Line from OHLCV data.

    CLV (Close Location Value) measures where close sits within the bar's range.
    AD line is the cumulative sum of CLV * volume.

    Args:
        high: Series of high prices.
        low: Series of low prices.
        close: Series of close prices.
        volume: Series of volume values.

    Returns:
        Series of AD line values (cumulative). NaN where high == low
        (division by zero in CLV).

    Example:
        >>> ad = calculate_ad_line(high, low, close, volume)
        >>> accumulating = ad.diff() > 0  # AD rising = accumulation
    """
    price_range = high - low
    # CLV: ((close - low) - (high - close)) / (high - low)
    # Simplified: (2*close - high - low) / (high - low)
    clv = (2 * close - high - low) / price_range.replace(0, pd.NA)
    ad_line = (clv * volume).cumsum()
    return ad_line


def calculate_ad_line_slope(ad_line: pd.Series, period: int = 20) -> pd.Series:
    """Calculate the slope of the AD line over a rolling window.

    Uses linear regression slope (via rolling covariance/variance)
    to measure the trend direction of accumulation/distribution.

    Args:
        ad_line: Series of AD line values (from calculate_ad_line).
        period: Rolling window for slope calculation (default 20).

    Returns:
        Series of AD line slope values. First `period` values are NaN.

    Example:
        >>> ad = calculate_ad_line(high, low, close, volume)
        >>> slope = calculate_ad_line_slope(ad, period=20)
        >>> strong_accumulation = slope > 0  # Positive slope = accumulation
    """
    return ad_line.diff(period) / period