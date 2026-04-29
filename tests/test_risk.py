"""Tests for risk management — circuit breaker and position sizing."""

import pytest
from datetime import datetime, timezone, timedelta
from unittest.mock import patch

from shared.models import CircuitBreakerState
from broky.risk.circuit_breaker import CircuitBreaker
from broky.risk.position_sizing import calculate_position_size, calculate_stop_loss, calculate_take_profit


class TestCircuitBreaker:
    def test_initial_state_is_inactive(self):
        cb = CircuitBreaker()
        assert cb.is_active is False
        assert cb.state.consecutive_losses == 0

    def test_record_one_loss_does_not_trigger(self):
        cb = CircuitBreaker()
        cb.record_loss()
        assert cb.is_active is False

    def test_three_consecutive_losses_triggers(self):
        cb = CircuitBreaker(consecutive_loss_limit=3)
        cb.record_loss()
        cb.record_loss()
        assert cb.is_active is False
        cb.record_loss()
        assert cb.is_active is True

    def test_record_win_resets_consecutive_losses(self):
        cb = CircuitBreaker(consecutive_loss_limit=3)
        cb.record_loss()
        cb.record_loss()
        cb.record_win()
        cb.record_loss()
        # Only 1 consecutive loss after the win
        assert cb.is_active is False

    def test_daily_loss_limit_triggers(self):
        cb = CircuitBreaker(daily_loss_limit_pct=0.05)
        # $100 equity, 5% limit = $5 loss triggers
        cb._daily_start_equity = 100.0
        cb.record_loss(pnl=-5.0, equity=100.0)
        assert cb.is_active is True

    def test_can_open_trade_when_inactive(self):
        cb = CircuitBreaker()
        can_trade, reason = cb.can_open_trade()
        assert can_trade is True
        assert reason == "OK"

    def test_cannot_open_trade_when_active(self):
        cb = CircuitBreaker(consecutive_loss_limit=3)
        cb.record_loss()
        cb.record_loss()
        cb.record_loss()
        can_trade, reason = cb.can_open_trade()
        assert can_trade is False
        assert "active" in reason.lower() or "breaker" in reason.lower()

    def test_cooldown_expiry(self):
        cb = CircuitBreaker(consecutive_loss_limit=3, cooldown_minutes=15)
        cb.record_loss()
        cb.record_loss()
        cb.record_loss()
        assert cb.state.is_active is True
        # Manually set cooldown to past to simulate expiry
        cb._state.cooldown_until = datetime.now(timezone.utc) - timedelta(minutes=1)
        # Now checking is_active should auto-reset
        assert cb.is_active is False

    def test_flash_crash_detection(self):
        cb = CircuitBreaker()
        triggered = cb.check_flash_crash(-10.0)  # 10% drop
        assert triggered is True
        assert cb.state.flash_crash_detected is True

    def test_flash_crash_not_triggered_under_threshold(self):
        cb = CircuitBreaker()
        triggered = cb.check_flash_crash(-5.0)  # 5% drop (below 10%)
        assert triggered is False

    def test_reset_daily(self):
        cb = CircuitBreaker()
        cb._daily_start_equity = 1000.0
        cb._daily_pnl = -50.0
        cb.reset_daily()
        assert cb._daily_pnl == 0.0
        assert cb._daily_start_equity is None


class TestPositionSizing:
    def test_calculate_position_size_basic(self):
        # $1000 equity, 2% risk = $20 risk, SL distance = $15
        # lots = 20 / (15 * 100) = 0.013 → 0.01 (minimum)
        size = calculate_position_size(
            equity=1000,
            risk_per_trade_pct=0.02,
            entry_price=1900.0,
            stop_loss_price=1885.0,
            contract_size=100.0,
        )
        assert size >= 0.01
        assert isinstance(size, float)

    def test_position_size_minimum_lot(self):
        """Position size should never be below minimum lot (0.01)."""
        size = calculate_position_size(
            equity=10,
            risk_per_trade_pct=0.01,
            entry_price=1900.0,
            stop_loss_price=1899.0,
            contract_size=100.0,
        )
        assert size >= 0.01

    def test_position_size_zero_entry_returns_minimum(self):
        size = calculate_position_size(0, 0, 0, 0)
        assert size == 0.01

    def test_position_size_increases_with_equity(self):
        size_small = calculate_position_size(100, 0.02, 1900.0, 1885.0, 100.0)
        size_large = calculate_position_size(10000, 0.02, 1900.0, 1885.0, 100.0)
        assert size_large > size_small

    def test_position_size_with_large_equity(self):
        """$10000 equity, 2% risk, $15 SL distance → lots > 0.01"""
        # risk = $200, SL distance = $15, lots = 200 / (15*100) = 0.133 → 0.13
        size = calculate_position_size(10000, 0.02, 1900.0, 1885.0, 100.0)
        assert size == 0.13

    def test_stop_loss_buy(self):
        sl = calculate_stop_loss(1900.0, 15.0, "BUY", atr_multiplier=1.5, spread_buffer=2.0)
        # SL = 1900 - (15 * 1.5 + 2) = 1900 - 24.5 = 1875.5
        assert sl == 1875.5

    def test_stop_loss_sell(self):
        sl = calculate_stop_loss(1900.0, 15.0, "SELL", atr_multiplier=1.5, spread_buffer=2.0)
        # SL = 1900 + (15 * 1.5 + 2) = 1900 + 24.5 = 1924.5
        assert sl == 1924.5

    def test_stop_loss_custom_multiplier(self):
        sl = calculate_stop_loss(1900.0, 10.0, "BUY", atr_multiplier=2.0, spread_buffer=1.0)
        # SL = 1900 - (10 * 2.0 + 1) = 1900 - 21 = 1879.0
        assert sl == 1879.0

    def test_take_profit_buy(self):
        tp = calculate_take_profit(1900.0, 1875.0, "BUY", risk_reward_ratio=2.0)
        # SL distance = 25, TP distance = 25 * 2 = 50
        # TP = 1900 + 50 = 1950.0
        assert tp == 1950.0

    def test_take_profit_sell(self):
        tp = calculate_take_profit(1900.0, 1925.0, "SELL", risk_reward_ratio=2.0)
        # SL distance = 25, TP distance = 25 * 2 = 50
        # TP = 1900 - 50 = 1850.0
        assert tp == 1850.0

    def test_take_profit_3_to_1_rr(self):
        tp = calculate_take_profit(1900.0, 1875.0, "BUY", risk_reward_ratio=3.0)
        # SL distance = 25, TP distance = 25 * 3 = 75
        # TP = 1900 + 75 = 1975.0
        assert tp == 1975.0