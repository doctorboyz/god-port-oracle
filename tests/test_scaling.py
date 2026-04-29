"""Tests for JPMorgan position scaling strategy — all 10 rules plus edge cases."""

import pytest

from shared.models import ScalingAction, ScalingDecision
from broky.signals.scaling import (
    calculate_scaling_action,
    calculate_position_adjustment,
    calculate_entry_and_change,
    should_scale_position,
)


class TestCalculateScalingAction:
    """Test all 10 JPMorgan rules + boundary conditions."""

    # --- Drop rules ---

    def test_drop_10_percent_hold(self):
        """Drop 10% → Hold."""
        result = calculate_scaling_action(-10.0)
        assert result.action == ScalingAction.HOLD
        assert result.adjustment_pct == 0.0

    def test_drop_12_percent_hold(self):
        """Between -10% and -20% → Hold."""
        result = calculate_scaling_action(-12.0)
        assert result.action == ScalingAction.HOLD
        assert result.adjustment_pct == 0.0

    def test_drop_20_percent_buy_15(self):
        """Drop 20% → Buy +15%."""
        result = calculate_scaling_action(-20.0)
        assert result.action == ScalingAction.BUY
        assert result.adjustment_pct == 15.0

    def test_drop_25_percent_buy_15(self):
        """Between -20% and -30% → Buy +15%."""
        result = calculate_scaling_action(-25.0)
        assert result.action == ScalingAction.BUY
        assert result.adjustment_pct == 15.0

    def test_drop_30_percent_buy_30(self):
        """Drop 30% → Buy +30%."""
        result = calculate_scaling_action(-30.0)
        assert result.action == ScalingAction.BUY
        assert result.adjustment_pct == 30.0

    def test_drop_50_percent_buy_30(self):
        """Beyond -30% → still Buy +30%."""
        result = calculate_scaling_action(-50.0)
        assert result.action == ScalingAction.BUY
        assert result.adjustment_pct == 30.0

    # --- Rise rules ---

    def test_rise_10_percent_hold(self):
        """Rise 10% → Hold."""
        result = calculate_scaling_action(10.0)
        assert result.action == ScalingAction.HOLD
        assert result.adjustment_pct == 0.0

    def test_rise_15_percent_hold(self):
        """Between 10% and 20% → Hold."""
        result = calculate_scaling_action(15.0)
        assert result.action == ScalingAction.HOLD

    def test_rise_20_percent_hold(self):
        """Rise 20% → Hold."""
        result = calculate_scaling_action(20.0)
        assert result.action == ScalingAction.HOLD
        assert result.adjustment_pct == 0.0

    def test_rise_25_percent_hold(self):
        """Between 20% and 30% → Hold."""
        result = calculate_scaling_action(25.0)
        assert result.action == ScalingAction.HOLD

    def test_rise_30_percent_sell_10(self):
        """Rise 30% → Sell 10%."""
        result = calculate_scaling_action(30.0)
        assert result.action == ScalingAction.SELL
        assert result.adjustment_pct == 10.0

    def test_rise_40_percent_sell_20(self):
        """Rise 40% → Sell 20%."""
        result = calculate_scaling_action(40.0)
        assert result.action == ScalingAction.SELL
        assert result.adjustment_pct == 20.0

    def test_rise_50_percent_sell_30(self):
        """Rise 50% → Sell 30%."""
        result = calculate_scaling_action(50.0)
        assert result.action == ScalingAction.SELL
        assert result.adjustment_pct == 30.0

    def test_rise_60_percent_sell_40(self):
        """Rise 60% → Sell 40%."""
        result = calculate_scaling_action(60.0)
        assert result.action == ScalingAction.SELL
        assert result.adjustment_pct == 40.0

    def test_rise_100_percent_sell_60(self):
        """Rise 100% → Sell 60%."""
        result = calculate_scaling_action(100.0)
        assert result.action == ScalingAction.SELL
        assert result.adjustment_pct == 60.0

    def test_rise_150_percent_sell_60(self):
        """Beyond 100% → still Sell 60%."""
        result = calculate_scaling_action(150.0)
        assert result.action == ScalingAction.SELL
        assert result.adjustment_pct == 60.0

    # --- No significant change ---

    def test_small_positive_change(self):
        """Between 0% and 10% → Hold."""
        result = calculate_scaling_action(5.0)
        assert result.action == ScalingAction.HOLD
        assert result.adjustment_pct == 0.0

    def test_small_negative_change(self):
        """Between 0% and -10% → Hold."""
        result = calculate_scaling_action(-5.0)
        assert result.action == ScalingAction.HOLD
        assert result.adjustment_pct == 0.0

    def test_zero_change(self):
        """No change at all → Hold."""
        result = calculate_scaling_action(0.0)
        assert result.action == ScalingAction.HOLD
        assert result.adjustment_pct == 0.0

    # --- Reason strings ---

    def test_reason_strings_are_descriptive(self):
        result = calculate_scaling_action(-20.0)
        assert "20%" in result.reason
        assert "Buy" in result.reason or "BUY" in result.reason

    def test_sell_reason_strings(self):
        result = calculate_scaling_action(50.0)
        assert "50%" in result.reason
        assert "Sell" in result.reason or "SELL" in result.reason


class TestCalculatePositionAdjustment:
    """Test position sizing adjustments based on scaling decisions."""

    def test_buy_scale_in_15_percent(self):
        """Buy +15% of original lot size, rounded to lot step 0.01."""
        decision = ScalingDecision(
            price_change_pct=-20.0,
            action=ScalingAction.BUY,
            adjustment_pct=15.0,
            reason="Drop 20% → Buy +15%",
        )
        new_size = calculate_position_adjustment(0.10, 0.10, decision)
        # 0.10 + (0.10 * 0.15) = 0.10 + 0.015 = 0.115 → rounded to 0.12
        assert new_size == 0.12

    def test_buy_scale_in_30_percent(self):
        """Buy +30% of original lot size."""
        decision = ScalingDecision(
            price_change_pct=-30.0,
            action=ScalingAction.BUY,
            adjustment_pct=30.0,
            reason="Drop 30% → Buy +30%",
        )
        new_size = calculate_position_adjustment(0.10, 0.10, decision)
        # 0.10 + (0.10 * 0.30) = 0.10 + 0.03 = 0.13
        assert new_size == 0.13

    def test_sell_scale_out_10_percent(self):
        """Sell 10% of current position."""
        decision = ScalingDecision(
            price_change_pct=30.0,
            action=ScalingAction.SELL,
            adjustment_pct=10.0,
            reason="Rise 30% → Sell 10%",
        )
        new_size = calculate_position_adjustment(0.10, 0.10, decision)
        # 0.10 - (0.10 * 0.10) = 0.10 - 0.01 = 0.09
        assert new_size == 0.09

    def test_sell_scale_out_60_percent(self):
        """Sell 60% of current position."""
        decision = ScalingDecision(
            price_change_pct=100.0,
            action=ScalingAction.SELL,
            adjustment_pct=60.0,
            reason="Rise 100% → Sell 60%",
        )
        new_size = calculate_position_adjustment(0.10, 0.10, decision)
        # 0.10 - (0.10 * 0.60) = 0.10 - 0.06 = 0.04
        assert new_size == 0.04

    def test_hold_no_change(self):
        """Hold → position unchanged."""
        decision = ScalingDecision(
            price_change_pct=5.0,
            action=ScalingAction.HOLD,
            adjustment_pct=0.0,
            reason="No significant change",
        )
        new_size = calculate_position_adjustment(0.10, 0.10, decision)
        assert new_size == 0.10

    def test_sell_minimum_lot_size(self):
        """Selling can't go below minimum lot 0.01."""
        decision = ScalingDecision(
            price_change_pct=100.0,
            action=ScalingAction.SELL,
            adjustment_pct=60.0,
            reason="Rise 100% → Sell 60%",
        )
        # Even with a tiny position, result must be >= 0.01
        new_size = calculate_position_adjustment(0.02, 0.02, decision)
        # 0.02 - (0.02 * 0.60) = 0.02 - 0.012 = 0.008 → clamped to 0.01
        assert new_size == 0.01

    def test_sequential_scaling_buy_then_sell(self):
        """Simulate: Buy +15% at -20%, then Sell 10% at +30%."""
        # Step 1: Entry 0.10 lot, drops 20% → Buy +15%
        buy_decision = ScalingDecision(
            price_change_pct=-20.0,
            action=ScalingAction.BUY,
            adjustment_pct=15.0,
            reason="Drop 20% → Buy +15%",
        )
        after_buy = calculate_position_adjustment(0.10, 0.10, buy_decision)
        # 0.10 + 0.015 = 0.115 → rounded to 0.12
        assert after_buy == 0.12

        # Step 2: Price recovers +30%, Sell 10% of current position
        sell_decision = ScalingDecision(
            price_change_pct=30.0,
            action=ScalingAction.SELL,
            adjustment_pct=10.0,
            reason="Rise 30% → Sell 10%",
        )
        after_sell = calculate_position_adjustment(0.10, after_buy, sell_decision)
        # 0.12 - (0.12 * 0.10) = 0.12 - 0.012 = 0.108 → rounded to 0.11
        assert after_sell == 0.11

    def test_cumulative_scaling(self):
        """Simulate: Buy +30% at -30%, then Sell 60% at +100%."""
        # Step 1: Entry 0.10, drops 30% → Buy +30%
        buy_decision = ScalingDecision(
            price_change_pct=-30.0,
            action=ScalingAction.BUY,
            adjustment_pct=30.0,
            reason="Drop 30% → Buy +30%",
        )
        after_buy = calculate_position_adjustment(0.10, 0.10, buy_decision)
        assert after_buy == 0.13

        # Step 2: Rises 100%, Sell 60% of current position
        sell_decision = ScalingDecision(
            price_change_pct=100.0,
            action=ScalingAction.SELL,
            adjustment_pct=60.0,
            reason="Rise 100% → Sell 60%",
        )
        after_sell = calculate_position_adjustment(0.10, after_buy, sell_decision)
        # 0.13 - (0.13 * 0.60) = 0.13 - 0.078 = 0.052 → rounded to 0.05
        assert after_sell == 0.05


class TestCalculateEntryAndChange:
    """Test price change percentage calculation."""

    def test_positive_change(self):
        result = calculate_entry_and_change(1900.0, 1950.0)
        assert abs(result - 2.631578947368421) < 0.001

    def test_negative_change(self):
        result = calculate_entry_and_change(1900.0, 1330.0)
        assert abs(result - (-30.0)) < 0.001

    def test_30_percent_rise(self):
        result = calculate_entry_and_change(100.0, 130.0)
        assert abs(result - 30.0) < 0.001

    def test_100_percent_rise(self):
        result = calculate_entry_and_change(100.0, 200.0)
        assert abs(result - 100.0) < 0.001

    def test_zero_change(self):
        result = calculate_entry_and_change(1900.0, 1900.0)
        assert result == 0.0

    def test_zero_entry_returns_zero(self):
        result = calculate_entry_and_change(0, 100.0)
        assert result == 0.0


class TestShouldScalePosition:
    """Test whether a price change warrants action."""

    def test_actionable_drops(self):
        assert should_scale_position(-20.0) is True
        assert should_scale_position(-30.0) is True
        assert should_scale_position(-50.0) is True

    def test_actionable_rises(self):
        assert should_scale_position(30.0) is True
        assert should_scale_position(40.0) is True
        assert should_scale_position(50.0) is True
        assert should_scale_position(60.0) is True
        assert should_scale_position(100.0) is True

    def test_non_actionable_holds(self):
        assert should_scale_position(0.0) is False
        assert should_scale_position(5.0) is False
        assert should_scale_position(-5.0) is False
        assert should_scale_position(-10.0) is False
        assert should_scale_position(10.0) is False
        assert should_scale_position(20.0) is False


class TestEdgeCases:
    """Edge cases and boundary conditions."""

    def test_very_small_negative_change(self):
        result = calculate_scaling_action(-0.01)
        assert result.action == ScalingAction.HOLD

    def test_very_small_positive_change(self):
        result = calculate_scaling_action(0.01)
        assert result.action == ScalingAction.HOLD

    def test_exact_boundary_minus_10(self):
        """Exactly -10% is a boundary — should be Hold."""
        result = calculate_scaling_action(-10.0)
        assert result.action == ScalingAction.HOLD

    def test_just_below_minus_10(self):
        """-10.01% should also be Hold."""
        result = calculate_scaling_action(-10.01)
        assert result.action == ScalingAction.HOLD

    def test_exact_boundary_30_percent(self):
        """Exactly 30% is the boundary for Sell 10%."""
        result = calculate_scaling_action(30.0)
        assert result.action == ScalingAction.SELL
        assert result.adjustment_pct == 10.0

    def test_just_above_30_percent(self):
        """30.01% should still trigger Sell 10%."""
        result = calculate_scaling_action(30.01)
        assert result.action == ScalingAction.SELL

    def test_extreme_drop(self):
        """-90% drop → Buy +30% (max scale-in)."""
        result = calculate_scaling_action(-90.0)
        assert result.action == ScalingAction.BUY
        assert result.adjustment_pct == 30.0

    def test_extreme_rise(self):
        """500% rise → Sell 60% (max scale-out)."""
        result = calculate_scaling_action(500.0)
        assert result.action == ScalingAction.SELL
        assert result.adjustment_pct == 60.0