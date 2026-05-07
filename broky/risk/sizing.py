"""Position sizing library — multiple sizing methods for risk management.

Methods:
- risk_per_trade: Fixed percentage of equity per trade (current default)
- fixed_fraction: Fixed lot size per trade
- kelly: Kelly criterion for optimal position sizing
- volatility_adjusted: ATR-scaled position sizing
"""

from __future__ import annotations

import math
from typing import Optional


def risk_per_trade_size(
    equity: float,
    risk_pct: float,
    entry_price: float,
    stop_loss: float,
    contract_size: float = 100.0,
    min_lots: float = 0.01,
    max_lots: float = 10.0,
) -> float:
    """Calculate position size based on fixed risk percentage.

    Lots = (equity * risk_pct) / (sl_distance * contract_size)

    Args:
        equity: Account equity in USD.
        risk_pct: Risk per trade as decimal (e.g., 0.02 for 2%).
        entry_price: Entry price in USD/oz.
        stop_loss: Stop loss price in USD/oz.
        contract_size: Oz per lot (100 for standard XAUUSD).
        min_lots: Minimum lot size.
        max_lots: Maximum lot size.

    Returns:
        Position size in lots, clamped to [min_lots, max_lots].
    """
    if entry_price <= 0 or stop_loss <= 0 or equity <= 0:
        return min_lots

    sl_distance = abs(entry_price - stop_loss)
    if sl_distance == 0:
        return min_lots

    risk_amount = equity * risk_pct
    lots = risk_amount / (sl_distance * contract_size)
    lots = math.floor(lots * 100) / 100  # Round down to 0.01

    return max(min(lots, max_lots), min_lots)


def fixed_fraction_size(
    fixed_lots: float = 0.01,
) -> float:
    """Return a fixed lot size regardless of equity or market conditions.

    Useful for testing or when you want constant exposure per trade.

    Args:
        fixed_lots: The constant lot size to use.

    Returns:
        The fixed lot size.
    """
    return max(fixed_lots, 0.01)


def kelly_size(
    equity: float,
    win_rate: float,
    avg_win: float,
    avg_loss: float,
    entry_price: float,
    stop_loss: float,
    contract_size: float = 100.0,
    fraction: float = 0.5,
    min_lots: float = 0.01,
    max_lots: float = 10.0,
) -> float:
    """Calculate position size using the Kelly criterion.

    Kelly% = WR - ((1 - WR) / (avg_win / avg_loss))
    Lots = (equity * Kelly% * fraction) / (sl_distance * contract_size)

    Uses fractional Kelly (default 50%) to reduce variance.

    Args:
        equity: Account equity in USD.
        win_rate: Historical win rate as decimal (e.g., 0.55).
        avg_win: Average winning trade PnL in USD.
        avg_loss: Average losing trade PnL in USD (positive value).
        entry_price: Entry price in USD/oz.
        stop_loss: Stop loss price in USD/oz.
        contract_size: Oz per lot (100 for standard XAUUSD).
        fraction: Fraction of Kelly to use (0.5 = half-Kelly, reduces variance).
        min_lots: Minimum lot size.
        max_lots: Maximum lot size.

    Returns:
        Position size in lots. Returns min_lots if Kelly is negative or invalid.
    """
    if avg_loss == 0 or equity <= 0:
        return min_lots

    win_loss_ratio = abs(avg_win / avg_loss)
    kelly_pct = win_rate - ((1 - win_rate) / win_loss_ratio)

    if kelly_pct <= 0:
        return min_lots  # Negative Kelly = don't trade

    sl_distance = abs(entry_price - stop_loss)
    if sl_distance == 0:
        return min_lots

    risk_amount = equity * kelly_pct * fraction
    lots = risk_amount / (sl_distance * contract_size)
    lots = math.floor(lots * 100) / 100

    return max(min(lots, max_lots), min_lots)


def volatility_adjusted_size(
    equity: float,
    risk_pct: float,
    entry_price: float,
    stop_loss: float,
    atr: float,
    contract_size: float = 100.0,
    atr_scale: float = 1.5,
    min_lots: float = 0.01,
    max_lots: float = 10.0,
) -> float:
    """Calculate position size scaled by current volatility (ATR).

    When volatility is high, reduce size. When low, increase size.
    This uses ATR to dynamically adjust the risk amount.

    risk_amount = equity * risk_pct * (atr_base / atr)
    where atr_base = atr * atr_scale (baseline ATR for normalization)

    In practice: if current ATR > baseline, size DOWN; if < baseline, size UP.

    Args:
        equity: Account equity in USD.
        risk_pct: Base risk per trade as decimal.
        entry_price: Entry price in USD/oz.
        stop_loss: Stop loss price in USD/oz.
        atr: Current ATR value.
        contract_size: Oz per lot (100 for standard XAUUSD).
        atr_scale: ATR multiplier for baseline (default 1.5).
        min_lots: Minimum lot size.
        max_lots: Maximum lot size.

    Returns:
        Position size in lots, volatility-adjusted.
    """
    if entry_price <= 0 or stop_loss <= 0 or equity <= 0 or atr <= 0:
        return min_lots

    sl_distance = abs(entry_price - stop_loss)
    if sl_distance == 0:
        return min_lots

    atr_baseline = atr * atr_scale
    vol_factor = atr_baseline / max(sl_distance, 0.01)
    vol_factor = max(min(vol_factor, 3.0), 0.1)  # Clamp between 0.1x and 3x

    adjusted_risk = equity * risk_pct * vol_factor
    lots = adjusted_risk / (sl_distance * contract_size)
    lots = math.floor(lots * 100) / 100

    return max(min(lots, max_lots), min_lots)


# Mapping of method names to functions for env-based selection
SIZING_METHODS = {
    "risk_per_trade": risk_per_trade_size,
    "fixed_fraction": fixed_fraction_size,
    "kelly": kelly_size,
    "volatility_adjusted": volatility_adjusted_size,
}


def get_sizing_method(name: str):
    """Get a sizing function by name.

    Args:
        name: One of 'risk_per_trade', 'fixed_fraction', 'kelly', 'volatility_adjusted'.

    Returns:
        The sizing function.

    Raises:
        ValueError: If name is not a recognized sizing method.
    """
    if name not in SIZING_METHODS:
        raise ValueError(f"Unknown sizing method '{name}'. Choose from: {list(SIZING_METHODS.keys())}")
    return SIZING_METHODS[name]