"""Position sizing — ATR-based calculations for stop loss and take profit."""

from __future__ import annotations

from typing import Optional


def calculate_position_size(
    equity: float,
    risk_per_trade_pct: float,
    entry_price: float,
    stop_loss_price: float,
    contract_size: float = 100.0,
) -> float:
    """Calculate position size based on risk percentage.

    For XAUUSD: 1 lot = 100 oz (contract_size).
    PnL per lot = price_move * contract_size.

    Formula:
        lots = (equity * risk_pct) / (sl_distance * contract_size)

    Args:
        equity: Account equity in USD.
        risk_per_trade_pct: Risk per trade as decimal (e.g., 0.02 for 2%).
        entry_price: Entry price in USD/oz.
        stop_loss_price: Stop loss price in USD/oz.
        contract_size: Oz per lot (100 for standard XAUUSD lot).

    Returns:
        Position size in lots, rounded to 2 decimal places (min 0.01).

    Example:
        >>> # $1000 equity, 2% risk, SL 15 points away
        >>> calculate_position_size(1000, 0.02, 1900.0, 1885.0, 100.0)
        0.01
    """
    if entry_price <= 0 or stop_loss_price <= 0:
        return 0.01

    risk_amount = equity * risk_per_trade_pct
    sl_distance = abs(entry_price - stop_loss_price)

    if sl_distance == 0:
        return 0.01

    # Value per pip per lot = sl_distance * contract_size
    # lots = risk_amount / (sl_distance * contract_size)
    lots = risk_amount / (sl_distance * contract_size)

    return max(round(lots * 100) / 100, 0.01)


def calculate_stop_loss(
    entry_price: float,
    atr: float,
    direction: str = "BUY",
    atr_multiplier: float = 1.5,
    spread_buffer: float = 2.0,
) -> float:
    """Calculate ATR-based stop loss price.

    Args:
        entry_price: Entry price.
        atr: Current ATR value.
        direction: "BUY" or "SELL".
        atr_multiplier: ATR multiplier for stop distance (default 1.5).
        spread_buffer: Extra pips for spread (default 2.0).

    Returns:
        Stop loss price.

    Example:
        >>> calculate_stop_loss(1900.0, 15.0, "BUY")
        1875.5
    """
    sl_distance = atr * atr_multiplier + spread_buffer

    if direction.upper() == "BUY":
        return round(entry_price - sl_distance, 2)
    else:
        return round(entry_price + sl_distance, 2)


def calculate_take_profit(
    entry_price: float,
    stop_loss_price: float,
    direction: str = "BUY",
    risk_reward_ratio: float = 2.0,
) -> float:
    """Calculate take profit price based on risk:reward ratio.

    Args:
        entry_price: Entry price.
        stop_loss_price: Stop loss price.
        direction: "BUY" or "SELL".
        risk_reward_ratio: Target R:R ratio (default 2.0).

    Returns:
        Take profit price.

    Example:
        >>> calculate_take_profit(1900.0, 1875.0, "BUY", 2.0)
        1950.0
    """
    sl_distance = abs(entry_price - stop_loss_price)
    tp_distance = sl_distance * risk_reward_ratio

    if direction.upper() == "BUY":
        return round(entry_price + tp_distance, 2)
    else:
        return round(entry_price - tp_distance, 2)