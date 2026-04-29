"""MACD (Moving Average Convergence Divergence) calculation."""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd


@dataclass
class MACDResult:
    """MACD calculation output."""
    macd_line: pd.Series
    signal_line: pd.Series
    histogram: pd.Series


def calculate_macd(
    close: pd.Series,
    fast_period: int = 12,
    slow_period: int = 26,
    signal_period: int = 9,
) -> MACDResult:
    """Calculate MACD from a close price series.

    Args:
        close: Series of close prices.
        fast_period: Fast EMA period (default 12).
        slow_period: Slow EMA period (default 26).
        signal_period: Signal line period (default 9).

    Returns:
        MACDResult with macd_line, signal_line, and histogram.

    Example:
        >>> result = calculate_macd(close)
        >>> bullish = result.histogram > 0
    """
    fast_ema = close.ewm(span=fast_period, adjust=False).mean()
    slow_ema = close.ewm(span=slow_period, adjust=False).mean()
    macd_line = fast_ema - slow_ema
    signal_line = macd_line.ewm(span=signal_period, adjust=False).mean()
    histogram = macd_line - signal_line

    return MACDResult(
        macd_line=macd_line,
        signal_line=signal_line,
        histogram=histogram,
    )