"""Bollinger Bands calculation."""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd


@dataclass
class BollingerResult:
    """Bollinger Bands calculation output."""
    upper: pd.Series
    middle: pd.Series
    lower: pd.Series
    bandwidth: pd.Series  # (upper - lower) / middle
    percent_b: pd.Series  # (close - lower) / (upper - lower)


def calculate_bollinger(
    close: pd.Series,
    period: int = 20,
    std_dev: float = 2.0,
) -> BollingerResult:
    """Calculate Bollinger Bands from a close price series.

    Args:
        close: Series of close prices.
        period: Moving average period (default 20).
        std_dev: Number of standard deviations (default 2.0).

    Returns:
        BollingerResult with upper, middle, lower, bandwidth, and %B.

    Example:
        >>> result = calculate_bollinger(close)
        >>> oversold = close < result.lower
    """
    middle = close.rolling(window=period).mean()
    rolling_std = close.rolling(window=period).std()
    upper = middle + (rolling_std * std_dev)
    lower = middle - (rolling_std * std_dev)
    bandwidth = (upper - lower) / middle.replace(0, pd.NA)
    percent_b = (close - lower) / (upper - lower).replace(0, pd.NA)

    return BollingerResult(
        upper=upper,
        middle=middle,
        lower=lower,
        bandwidth=bandwidth,
        percent_b=percent_b,
    )