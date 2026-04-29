"""ATR (Average True Range) calculation."""

from __future__ import annotations

import pandas as pd


def calculate_atr(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    period: int = 14,
) -> pd.Series:
    """Calculate ATR (Average True Range) from OHLC data.

    ATR measures volatility — used for position sizing and stop loss placement.

    Args:
        high: Series of high prices.
        low: Series of low prices.
        close: Series of close prices (previous close used for True Range).
        period: ATR period (default 14).

    Returns:
        Series of ATR values. First value is NaN.

    Example:
        >>> atr = calculate_atr(high, low, close, period=14)
        >>> stop_loss = close - 1.5 * atr  # ATR-based stop loss
    """
    prev_close = close.shift(1)
    tr1 = high - low
    tr2 = (high - prev_close).abs()
    tr3 = (low - prev_close).abs()
    true_range = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    return true_range.ewm(alpha=1 / period, min_periods=period).mean()