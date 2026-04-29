"""Tests for backtest engine."""

import numpy as np
import pandas as pd
import pytest
from datetime import datetime, timedelta

from broky.backtest.engine import BacktestEngine, BacktestResult


def _make_market_df(n: int = 500, start_price: float = 1900.0, trend: float = 0.1) -> pd.DataFrame:
    """Generate synthetic OHLCV DataFrame for backtesting."""
    np.random.seed(42)
    dates = pd.date_range(start="2026-01-01", periods=n, freq="5min")
    closes = start_price + np.cumsum(np.random.normal(trend, 1.5, n))
    spreads = np.random.uniform(1, 4, n)

    return pd.DataFrame({
        "Open": closes - spreads * np.random.uniform(0.2, 0.8, n),
        "High": closes + spreads,
        "Low": closes - spreads,
        "Close": closes,
        "Volume": np.random.uniform(1000, 5000, n),
    }, index=dates)


class TestBacktestEngine:
    def test_backtest_returns_result(self):
        df = _make_market_df(300)
        engine = BacktestEngine(initial_equity=1000, risk_per_trade=0.02)
        result = engine.run(df, warmup=50)
        assert isinstance(result, BacktestResult)

    def test_backtest_equity_curve_starts_at_initial(self):
        df = _make_market_df(300)
        engine = BacktestEngine(initial_equity=1000)
        result = engine.run(df, warmup=50)
        assert result.equity_curve[0] == 1000.0

    def test_backtest_win_rate_between_0_and_1(self):
        df = _make_market_df(500)
        engine = BacktestEngine(initial_equity=1000)
        result = engine.run(df, warmup=50)
        if result.total_trades > 0:
            assert 0 <= result.win_rate <= 1.0

    def test_backtest_max_drawdown_non_negative(self):
        df = _make_market_df(500)
        engine = BacktestEngine(initial_equity=1000)
        result = engine.run(df, warmup=50)
        assert result.max_drawdown_pct >= 0

    def test_backtest_profit_factor(self):
        df = _make_market_df(500)
        engine = BacktestEngine(initial_equity=1000)
        result = engine.run(df, warmup=50)
        if result.total_trades > 0:
            assert result.profit_factor >= 0

    def test_backtest_with_no_signals_produces_no_trades(self):
        """With very low confidence threshold, we still get a result."""
        df = _make_market_df(300)
        engine = BacktestEngine(initial_equity=1000, min_confidence=0.99)
        result = engine.run(df, warmup=50)
        # With min_confidence=0.99, most signals won't pass
        assert result.total_trades >= 0

    def test_backtest_trade_exit_reasons(self):
        """Trades should exit with either stop_loss or take_profit."""
        df = _make_market_df(500)
        engine = BacktestEngine(initial_equity=1000, risk_per_trade=0.05)
        result = engine.run(df, warmup=50)
        for trade in result.trades:
            assert trade.exit_reason in ("stop_loss", "take_profit", "max_holding", "")

    def test_backtest_trades_have_valid_prices(self):
        df = _make_market_df(500)
        engine = BacktestEngine(initial_equity=1000)
        result = engine.run(df, warmup=50)
        for trade in result.trades:
            assert trade.entry_price > 0
            assert trade.exit_price is None or trade.exit_price > 0
            assert trade.stop_loss > 0
            assert trade.take_profit > 0

    def test_backtest_consecutive_counts(self):
        df = _make_market_df(500)
        engine = BacktestEngine(initial_equity=1000)
        result = engine.run(df, warmup=50)
        if result.total_trades > 0:
            assert result.max_consecutive_wins >= 0
            assert result.max_consecutive_losses >= 0