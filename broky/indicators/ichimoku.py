"""Ichimoku Cloud calculation.

Pure functions: takes high/low/close series, returns Ichimoku components.
Standard periods: tenkan=9, kijun=26, senkou_b=52, shift=26.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd


@dataclass
class IchimokuResult:
    """Ichimoku Cloud calculation output."""

    tenkan: pd.Series  # Conversion Line (period 9)
    kijun: pd.Series  # Base Line (period 26)
    senkou_a: pd.Series  # Leading Span A, shifted 26 periods ahead
    senkou_b: pd.Series  # Leading Span B, shifted 26 periods ahead
    chikou: pd.Series  # Lagging Span, close shifted 26 periods back


def _midpoint(high: pd.Series, low: pd.Series, period: int) -> pd.Series:
    """Calculate the midpoint (highest high + lowest low) / 2 over a period."""
    highest = high.rolling(window=period).max()
    lowest = low.rolling(window=period).min()
    return (highest + lowest) / 2


def calculate_ichimoku(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    tenkan_period: int = 9,
    kijun_period: int = 26,
    senkou_b_period: int = 52,
    shift: int = 26,
) -> IchimokuResult:
    """Calculate Ichimoku Cloud components.

    Args:
        high: Series of high prices.
        low: Series of low prices.
        close: Series of close prices.
        tenkan_period: Conversion Line period (default 9).
        kijun_period: Base Line period (default 26).
        senkou_b_period: Leading Span B period (default 52).
        shift: Number of periods to shift senkou spans ahead (default 26).

    Returns:
        IchimokuResult with tenkan, kijun, senkou_a, senkou_b, and chikou.
        First N periods contain NaN (expected for rolling calculations).

    Example:
        >>> result = calculate_ichimoku(high, low, close)
        >>> above_cloud = price_vs_cloud(close, result.senkou_a, result.senkou_b) == "above"
    """
    tenkan = _midpoint(high, low, tenkan_period)
    kijun = _midpoint(high, low, kijun_period)

    senkou_a = ((tenkan + kijun) / 2).shift(shift)
    senkou_b = _midpoint(high, low, senkou_b_period).shift(shift)

    chikou = close.shift(-shift)

    return IchimokuResult(
        tenkan=tenkan,
        kijun=kijun,
        senkou_a=senkou_a,
        senkou_b=senkou_b,
        chikou=chikou,
    )


def price_vs_cloud(
    close: pd.Series,
    senkou_a: pd.Series,
    senkou_b: pd.Series,
) -> pd.Series:
    """Classify price position relative to the Ichimoku cloud.

    Args:
        close: Series of close prices.
        senkou_a: Leading Span A from IchimokuResult.
        senkou_b: Leading Span B from IchimokuResult.

    Returns:
        String Series with values "above", "inside", or "below".

    Example:
        >>> position = price_vs_cloud(close, result.senkou_a, result.senkou_b)
        >>> position.value_counts()  # count each category
    """
    cloud_upper = np.maximum(senkou_a, senkou_b)
    cloud_lower = np.minimum(senkou_a, senkou_b)

    result = pd.Series(np.where(
        close > cloud_upper, "above",
        np.where(close < cloud_lower, "below", "inside"),
    ), index=close.index, dtype="string")

    return result