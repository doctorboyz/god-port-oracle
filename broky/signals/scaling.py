"""JPMorgan Chase position scaling strategy.

Implements the disciplined scaling rules from a senior JPMorgan executive:

Drop rules (scale in on dips):
  -10% → Hold
  -20% → Buy +15%
  -30% → Buy +30%

Rise rules (scale out on strength):
  +10% → Hold
  +20% → Hold
  +30% → Sell 10%
  +40% → Sell 20%
  +50% → Sell 30%
  +60% → Sell 40%
  +100% → Sell 60%

Discipline and long-term growth. Every function is pure and testable.
"""

from __future__ import annotations

from shared.models import ScalingAction, ScalingDecision


def calculate_scaling_action(price_change_pct: float) -> ScalingDecision:
    """Determine the scaling action for a given price change percentage.

    Args:
        price_change_pct: Percentage change from entry price.
            Positive = profit (price went up).
            Negative = loss (price went down).

    Returns:
        ScalingDecision with action, adjustment percentage, and reason.

    Examples:
        >>> calculate_scaling_action(-20.0)
        ScalingDecision(price_change_pct=-20.0, action=<BUY:...>, adjustment_pct=15.0, ...)
        >>> calculate_scaling_action(30.0)
        ScalingDecision(price_change_pct=30.0, action=<SELL:...>, adjustment_pct=10.0, ...)
    """
    change = price_change_pct

    # Drop rules (negative price change → scale in)
    if change <= -30.0:
        return ScalingDecision(
            price_change_pct=change,
            action=ScalingAction.BUY,
            adjustment_pct=30.0,
            reason="Drop >= 30% → Buy +30%",
        )
    if change <= -20.0:
        return ScalingDecision(
            price_change_pct=change,
            action=ScalingAction.BUY,
            adjustment_pct=15.0,
            reason="Drop >= 20% → Buy +15%",
        )
    if change <= -10.0:
        return ScalingDecision(
            price_change_pct=change,
            action=ScalingAction.HOLD,
            adjustment_pct=0.0,
            reason="Drop >= 10% → Hold (wait for confirmation)",
        )

    # Rise rules (positive price change → scale out)
    if change >= 100.0:
        return ScalingDecision(
            price_change_pct=change,
            action=ScalingAction.SELL,
            adjustment_pct=60.0,
            reason="Rise >= 100% → Sell 60%",
        )
    if change >= 60.0:
        return ScalingDecision(
            price_change_pct=change,
            action=ScalingAction.SELL,
            adjustment_pct=40.0,
            reason="Rise >= 60% → Sell 40%",
        )
    if change >= 50.0:
        return ScalingDecision(
            price_change_pct=change,
            action=ScalingAction.SELL,
            adjustment_pct=30.0,
            reason="Rise >= 50% → Sell 30%",
        )
    if change >= 40.0:
        return ScalingDecision(
            price_change_pct=change,
            action=ScalingAction.SELL,
            adjustment_pct=20.0,
            reason="Rise >= 40% → Sell 20%",
        )
    if change >= 30.0:
        return ScalingDecision(
            price_change_pct=change,
            action=ScalingAction.SELL,
            adjustment_pct=10.0,
            reason="Rise >= 30% → Sell 10%",
        )
    if change >= 20.0:
        return ScalingDecision(
            price_change_pct=change,
            action=ScalingAction.HOLD,
            adjustment_pct=0.0,
            reason="Rise >= 20% → Hold",
        )
    if change >= 10.0:
        return ScalingDecision(
            price_change_pct=change,
            action=ScalingAction.HOLD,
            adjustment_pct=0.0,
            reason="Rise >= 10% → Hold",
        )

    # No significant change
    return ScalingDecision(
        price_change_pct=change,
        action=ScalingAction.HOLD,
        adjustment_pct=0.0,
        reason="No significant change → Hold",
    )


def calculate_position_adjustment(
    original_lot_size: float,
    current_lot_size: float,
    decision: ScalingDecision,
) -> float:
    """Calculate the new target position size after applying a scaling decision.

    For BUY actions: adjustment_pct is a percentage of the original lot size to add.
    For SELL actions: adjustment_pct is a percentage of the current lot size to remove.

    Args:
        original_lot_size: The initial position size when the trade was opened.
        current_lot_size: The current position size (after any prior scaling).
        decision: The ScalingDecision from calculate_scaling_action.

    Returns:
        New target lot size. Never goes below minimum lot (0.01).

    Examples:
        >>> decision = ScalingDecision(price_change_pct=-20.0, action=ScalingAction.BUY, adjustment_pct=15.0, reason="...")
        >>> calculate_position_adjustment(0.10, 0.10, decision)  # Buy +15% of original
        0.115
        >>> decision = ScalingDecision(price_change_pct=30.0, action=ScalingAction.SELL, adjustment_pct=10.0, reason="...")
        >>> calculate_position_adjustment(0.10, 0.10, decision)  # Sell 10% of current
        0.09
    """
    if decision.action == ScalingAction.BUY:
        addition = original_lot_size * (decision.adjustment_pct / 100.0)
        new_size = current_lot_size + addition
        # Round to lot step size (0.01 for XAUUSD)
        return round(new_size * 100) / 100

    if decision.action == ScalingAction.SELL:
        reduction = current_lot_size * (decision.adjustment_pct / 100.0)
        new_size = current_lot_size - reduction
        # Never below minimum lot, round to step size
        return max(round(new_size * 100) / 100, 0.01)

    return current_lot_size  # HOLD


def calculate_entry_and_change(entry_price: float, current_price: float) -> float:
    """Calculate percentage change from entry price.

    Args:
        entry_price: The price at which the position was opened.
        current_price: The current market price.

    Returns:
        Percentage change. Positive means profit, negative means loss.

    Examples:
        >>> calculate_entry_and_change(1900.0, 1950.0)
        2.631578947368421
        >>> calculate_entry_and_change(1900.0, 1330.0)
        -30.0
    """
    if entry_price == 0:
        return 0.0
    return ((current_price - entry_price) / entry_price) * 100


def should_scale_position(price_change_pct: float) -> bool:
    """Check if the price change warrants a scaling action (not just HOLD).

    Args:
        price_change_pct: Percentage change from entry.

    Returns:
        True if the scaling decision is BUY or SELL (actionable).

    Examples:
        >>> should_scale_position(-20.0)
        True
        >>> should_scale_position(30.0)
        True
        >>> should_scale_position(5.0)
        False
        >>> should_scale_position(-8.0)
        False
    """
    decision = calculate_scaling_action(price_change_pct)
    return decision.action != ScalingAction.HOLD