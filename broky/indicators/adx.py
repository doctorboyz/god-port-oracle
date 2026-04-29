"""ADX (Average Directional Index) calculation.

Pure function: takes OHLC data, returns ADX, +DI, and -DI values.
Wilder's smoothing (same as ATR/RSI) for consistent calculation.
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def calculate_adx(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    period: int = 14,
) -> tuple[pd.Series, pd.Series, pd.Series]:
    """Calculate ADX, +DI, and -DI from OHLC data.

    ADX measures trend strength (not direction):
    - ADX < 20: no clear trend (ranging market)
    - ADX 20-25: trend forming
    - ADX > 25: strong trend
    - ADX > 50: very strong trend

    +DI > -DI: bullish pressure
    -DI > +DI: bearish pressure

    Args:
        high: Series of high prices.
        low: Series of low prices.
        close: Series of close prices.
        period: ADX period (default 14).

    Returns:
        Tuple of (adx, plus_di, minus_di) as Series.
        First `period * 2 - 1` values may be NaN.

    Example:
        >>> adx, pdi, mdi = calculate_adx(high, low, close, period=14)
        >>> trending = adx > 25
        >>> bullish = pdi > mdi
    """
    prev_high = high.shift(1)
    prev_low = low.shift(1)

    # Directional Movement
    up_move = high - prev_high
    down_move = prev_low - low

    plus_dm = pd.Series(np.where((up_move > down_move) & (up_move > 0), up_move, 0.0), index=high.index)
    minus_dm = pd.Series(np.where((down_move > up_move) & (down_move > 0), down_move, 0.0), index=high.index)

    # True Range (reuse ATR logic)
    prev_close = close.shift(1)
    tr1 = high - low
    tr2 = (high - prev_close).abs()
    tr3 = (low - prev_close).abs()
    true_range = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)

    # Wilder's smoothing
    alpha = 1 / period
    atr = true_range.ewm(alpha=alpha, min_periods=period).mean()
    smooth_plus_dm = plus_dm.ewm(alpha=alpha, min_periods=period).mean()
    smooth_minus_dm = minus_dm.ewm(alpha=alpha, min_periods=period).mean()

    # +DI and -DI
    plus_di = 100 * smooth_plus_dm / atr.replace(0, np.nan)
    minus_di = 100 * smooth_minus_dm / atr.replace(0, np.nan)

    # DX (Directional Index)
    di_sum = plus_di + minus_di
    dx = 100 * (plus_di - minus_di).abs() / di_sum.replace(0, np.nan)

    # ADX = smoothed DX
    adx = dx.ewm(alpha=alpha, min_periods=period).mean()

    return adx, plus_di, minus_di