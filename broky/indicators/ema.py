"""EMA (Exponential Moving Average) calculation."""

from __future__ import annotations

import pandas as pd


def calculate_ema(close: pd.Series, period: int = 21) -> pd.Series:
    """Calculate EMA from a close price series.

    Args:
        close: Series of close prices.
        period: EMA period (default 21).

    Returns:
        Series of EMA values. First `period-1` values may be NaN.

    Example:
        >>> close = pd.Series([100, 101, 102, ...])
        >>> ema = calculate_ema(close, period=9)
    """
    return close.ewm(span=period, adjust=False).mean()


def calculate_ema_cross(
    close: pd.Series,
    fast_period: int = 9,
    slow_period: int = 21,
) -> tuple[pd.Series, pd.Series, pd.Series]:
    """Calculate EMA crossover signals.

    Args:
        close: Series of close prices.
        fast_period: Fast EMA period (default 9).
        slow_period: Slow EMA period (default 21).

    Returns:
        Tuple of (fast_ema, slow_ema, crossover) where crossover is:
         1 = golden cross (fast crosses above slow)
        -1 = death cross (fast crosses below slow)
         0 = no crossover

    Example:
        >>> fast, slow, cross = calculate_ema_cross(close)
        >>> golden_cross = cross == 1
    """
    fast = calculate_ema(close, fast_period)
    slow = calculate_ema(close, slow_period)

    diff = fast - slow
    crossover = diff.diff().apply(lambda x: 1 if x > 0 else (-1 if x < 0 else 0))

    return fast, slow, crossover