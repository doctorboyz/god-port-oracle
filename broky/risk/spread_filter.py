"""Spread filter — skip trades when spread exceeds threshold.

For M1 scalping, spread is critical. A wide spread eats into the
already-tight profit target. This module provides a simple check
that can be called before indicator computation to skip high-spread bars.
"""


def check_spread(spread: float, max_spread: float) -> bool:
    """Check if spread is within acceptable range.

    Args:
        spread: Current spread in points.
        max_spread: Maximum allowed spread in points.

    Returns:
        True if spread is acceptable (spread <= max_spread), False otherwise.
    """
    return spread <= max_spread


def spread_from_candle(high: float, low: float, open_price: float, close: float) -> float | None:
    """Estimate spread from candle data when explicit spread is unavailable.

    Uses the high-low range as an upper bound proxy. Not ideal but
    works as a rough filter when tick-level spread data is missing.

    Args:
        high: Candle high price.
        low: Candle low price.
        open_price: Candle open price.
        close: Candle close price.

    Returns:
        Estimated spread in price units, or None if data is invalid.
    """
    if high <= 0 or low <= 0:
        return None
    # Use high-low as proxy (overestimates, but safe for filtering)
    return round(high - low, 2)