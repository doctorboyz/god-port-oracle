"""DeMarker Oscillator calculation.

Pure function: takes HL series, returns DeMarker values.
Range: 0 to 1. Above 0.7 = overbought, below 0.3 = oversold.
"""

from __future__ import annotations

import pandas as pd


def calculate_demarker(
    high: pd.Series,
    low: pd.Series,
    period: int = 14,
) -> pd.Series:
    """Calculate DeMarker Oscillator from HL data.

    DeMarker measures demand zones and price exhaustion:
    - Above 0.7: overbought (potential reversal down)
    - Below 0.3: oversold (potential reversal up)

    Args:
        high: Series of high prices.
        low: Series of low prices.
        period: DeMarker period (default 14).

    Returns:
        Series of DeMarker values (0 to 1 range).
        First `period` values are NaN.

    Example:
        >>> demarker = calculate_demarker(high, low, period=14)
        >>> overbought = demarker > 0.7
    """
    prev_high = high.shift(1)
    prev_low = low.shift(1)

    de_max = (high - prev_high).clip(lower=0)
    de_min = (prev_low - low).clip(lower=0)

    sma_de_max = de_max.rolling(window=period).mean()
    sma_de_min = de_min.rolling(window=period).mean()

    denominator = (sma_de_max + sma_de_min).replace(0, pd.NA)
    demarker = sma_de_max / denominator

    return demarker