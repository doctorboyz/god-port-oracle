"""SMA (Simple Moving Average) calculation.

Pure function: takes a close price series, returns SMA values.
Includes convenience functions for common periods.
"""

from __future__ import annotations

import pandas as pd


def calculate_sma(close: pd.Series, period: int) -> pd.Series:
    """Calculate Simple Moving Average from a close price series.

    Args:
        close: Series of close prices.
        period: SMA period (no default — must be explicit).

    Returns:
        Series of SMA values. First `period-1` values are NaN.

    Example:
        >>> sma = calculate_sma(close, period=20)
        >>> above_sma = close > sma
    """
    return close.rolling(window=period).mean()


def calculate_sma_10(close: pd.Series) -> pd.Series:
    """Calculate 10-period SMA from a close price series.

    Args:
        close: Series of close prices.

    Returns:
        Series of SMA-10 values.
    """
    return calculate_sma(close, period=10)


def calculate_sma_20(close: pd.Series) -> pd.Series:
    """Calculate 20-period SMA from a close price series.

    Args:
        close: Series of close prices.

    Returns:
        Series of SMA-20 values.
    """
    return calculate_sma(close, period=20)


def calculate_sma_50(close: pd.Series) -> pd.Series:
    """Calculate 50-period SMA from a close price series.

    Args:
        close: Series of close prices.

    Returns:
        Series of SMA-50 values.
    """
    return calculate_sma(close, period=50)