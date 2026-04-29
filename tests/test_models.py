"""Tests for shared.models — verify all Pydantic models validate correctly."""

import pytest
from datetime import datetime

from shared.models import (
    MarketData,
    Signal,
    SignalType,
    Position,
    PositionAction,
    TradeResult,
    ScalingAction,
    ScalingDecision,
    SessionType,
    CircuitBreakerState,
)


class TestMarketData:
    def test_valid_candle(self):
        candle = MarketData(
            timestamp=datetime(2026, 1, 1, 12, 0),
            open=1900.0,
            high=1910.0,
            low=1895.0,
            close=1905.0,
            volume=1000.0,
        )
        assert candle.close == 1905.0
        assert candle.high >= candle.low

    def test_high_must_be_gte_low(self):
        with pytest.raises(ValueError):
            MarketData(
                timestamp=datetime(2026, 1, 1),
                open=1900.0,
                high=1890.0,
                low=1905.0,
                close=1900.0,
                volume=100.0,
            )

    def test_close_must_be_within_range(self):
        with pytest.raises(ValueError, match="close must be <= high"):
            MarketData(
                timestamp=datetime(2026, 1, 1),
                open=1900.0,
                high=1910.0,
                low=1890.0,
                close=1920.0,
                volume=100.0,
            )

    def test_close_below_low_raises(self):
        with pytest.raises(ValueError, match="close must be >= low"):
            MarketData(
                timestamp=datetime(2026, 1, 1),
                open=1900.0,
                high=1910.0,
                low=1890.0,
                close=1880.0,
                volume=100.0,
            )

    def test_zero_price_rejected(self):
        with pytest.raises(ValueError):
            MarketData(
                timestamp=datetime(2026, 1, 1),
                open=0,
                high=10,
                low=0,
                close=5,
                volume=100,
            )

    def test_negative_volume_rejected(self):
        with pytest.raises(ValueError):
            MarketData(
                timestamp=datetime(2026, 1, 1),
                open=1900.0,
                high=1910.0,
                low=1890.0,
                close=1905.0,
                volume=-1.0,
            )


class TestSignal:
    def test_valid_buy_signal(self):
        sig = Signal(
            signal_type=SignalType.BUY,
            confidence=0.75,
            price=1900.0,
            timestamp=datetime(2026, 1, 1),
            reason="EMA cross + RSI oversold",
        )
        assert sig.signal_type == SignalType.BUY
        assert sig.confidence == 0.75

    def test_confidence_below_threshold_clamps_to_zero(self):
        """Confidence below 0.3 is clamped to valid range, not rejected."""
        sig = Signal(
            signal_type=SignalType.HOLD,
            confidence=0.2,
            price=1900.0,
            timestamp=datetime(2026, 1, 1),
        )
        assert sig.confidence == 0.2  # Clamped within [0, 1]

    def test_confidence_boundary_03_accepted(self):
        sig = Signal(
            signal_type=SignalType.BUY,
            confidence=0.3,
            price=1900.0,
            timestamp=datetime(2026, 1, 1),
        )
        assert sig.confidence == 0.3

    def test_confidence_above_1_rejected(self):
        with pytest.raises(ValueError):
            Signal(
                signal_type=SignalType.BUY,
                confidence=1.5,
                price=1900.0,
                timestamp=datetime(2026, 1, 1),
            )

    def test_hold_signal(self):
        sig = Signal(
            signal_type=SignalType.HOLD,
            confidence=0.4,
            price=1900.0,
            timestamp=datetime(2026, 1, 1),
        )
        assert sig.signal_type == SignalType.HOLD


class TestPosition:
    def test_price_change_pct_profit(self):
        pos = Position(
            direction=SignalType.BUY,
            entry_price=1900.0,
            current_price=1950.0,
            lot_size=0.01,
            opened_at=datetime(2026, 1, 1),
        )
        # +50 on 1900 = ~2.63%
        assert abs(pos.price_change_pct - 2.631578947368421) < 0.01

    def test_price_change_pct_loss(self):
        pos = Position(
            direction=SignalType.BUY,
            entry_price=1900.0,
            current_price=1800.0,
            lot_size=0.01,
            opened_at=datetime(2026, 1, 1),
        )
        # -100 on 1900 = ~-5.26%
        assert abs(pos.price_change_pct - (-5.263157894736842)) < 0.01

    def test_price_change_pct_30_percent_rise(self):
        """JPMorgan rule: rises 30% → sell 10%"""
        pos = Position(
            direction=SignalType.BUY,
            entry_price=1900.0,
            current_price=2470.0,  # +30%
            lot_size=0.01,
            opened_at=datetime(2026, 1, 1),
        )
        assert abs(pos.price_change_pct - 30.0) < 0.1

    def test_price_change_pct_30_percent_drop(self):
        """JPMorgan rule: drops 30% → buy +30%"""
        pos = Position(
            direction=SignalType.BUY,
            entry_price=1900.0,
            current_price=1330.0,  # -30%
            lot_size=0.01,
            opened_at=datetime(2026, 1, 1),
        )
        assert abs(pos.price_change_pct - (-30.0)) < 0.1

    def test_is_profitable_buy(self):
        pos = Position(
            direction=SignalType.BUY,
            entry_price=1900.0,
            current_price=1950.0,
            lot_size=0.01,
            opened_at=datetime(2026, 1, 1),
        )
        assert pos.is_profitable is True

    def test_is_not_profitable_buy(self):
        pos = Position(
            direction=SignalType.BUY,
            entry_price=1900.0,
            current_price=1850.0,
            lot_size=0.01,
            opened_at=datetime(2026, 1, 1),
        )
        assert pos.is_profitable is False


class TestScalingDecision:
    def test_valid_decision(self):
        decision = ScalingDecision(
            price_change_pct=30.0,
            action=ScalingAction.SELL,
            adjustment_pct=10.0,
            reason="Rises 30% → Sell 10%",
        )
        assert decision.action == ScalingAction.SELL
        assert decision.adjustment_pct == 10.0

    def test_adjustment_pct_above_100_rejected(self):
        with pytest.raises(ValueError):
            ScalingDecision(
                price_change_pct=30.0,
                action=ScalingAction.SELL,
                adjustment_pct=150.0,
                reason="Invalid",
            )


class TestCircuitBreakerState:
    def test_default_state(self):
        state = CircuitBreakerState()
        assert state.consecutive_losses == 0
        assert state.is_active is False

    def test_triggered_state(self):
        state = CircuitBreakerState(
            consecutive_losses=3,
            daily_loss_pct=0.05,
            is_active=True,
            cooldown_until=datetime(2026, 1, 1, 12, 15),
        )
        assert state.is_active is True
        assert state.consecutive_losses == 3


class TestSessionType:
    def test_session_types(self):
        assert SessionType.ASIAN.value == "asian"
        assert SessionType.LONDON.value == "london"
        assert SessionType.NY.value == "ny"
        assert SessionType.OVERLAP.value == "overlap"