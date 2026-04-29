"""Stochastic Oscillator calculation."""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd


@dataclass
class StochasticResult:
    """Stochastic Oscillator calculation output."""
    k_line: pd.Series  # %K line
    d_line: pd.Series  # %D line (smoothed %K)


def calculate_stochastic(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    k_period: int = 14,
    d_period: int = 3,
    slowing: int = 3,
) -> StochasticResult:
    """Calculate Stochastic Oscillator.

    Args:
        high: Series of high prices.
        low: Series of low prices.
        close: Series of close prices.
        k_period: %K period (default 14).
        d_period: %D smoothing period (default 3).
        slowing: Slowing period for %K (default 3).

    Returns:
        StochasticResult with %K and %D lines (0-100 range).

    Example:
        >>> result = calculate_stochastic(high, low, close)
        >>> overbought = result.k_line > 80
    """
    lowest_low = low.rolling(window=k_period).min()
    highest_high = high.rolling(window=k_period).max()

    raw_k = 100 * (close - lowest_low) / (highest_high - lowest_low).replace(0, pd.NA)
    k_line = raw_k.rolling(window=slowing).mean()
    d_line = k_line.rolling(window=d_period).mean()

    return StochasticResult(k_line=k_line, d_line=d_line)