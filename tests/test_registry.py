"""Tests for StrategyRegistry and @strategy decorator."""

from __future__ import annotations

import pytest

from shared.models import TradingMode, Signal, SignalType
from broky.signals.registry import StrategyRegistry, StrategyConfig, strategy


@pytest.fixture(autouse=True)
def clean_registry():
    """Clear registry before each test."""
    StrategyRegistry._strategies.clear()
    yield
    StrategyRegistry._strategies.clear()


class TestStrategyDecorator:
    """Test @strategy decorator registration."""

    def test_registers_function(self):
        @strategy(
            name="test_strat",
            timeframe="H1",
            trading_mode=TradingMode.SWING,
        )
        def my_generator(close, high, low, volume):
            return Signal(signal_type=SignalType.HOLD, confidence=0.0, price=0.0)

        assert "test_strat" in StrategyRegistry.names()

    def test_attaches_config(self):
        @strategy(
            name="test_strat2",
            timeframe="M5",
            trading_mode=TradingMode.M5_SCALP,
            description="Test strategy",
            risk_defaults={"risk_per_trade": 0.02},
            requires_spread=True,
        )
        def my_generator2(close, high, low, volume):
            return Signal(signal_type=SignalType.HOLD, confidence=0.0, price=0.0)

        assert hasattr(my_generator2, "_strategy_config")
        config = my_generator2._strategy_config
        assert config.name == "test_strat2"
        assert config.timeframe == "M5"
        assert config.trading_mode == TradingMode.M5_SCALP
        assert config.requires_spread is True
        assert config.risk_defaults == {"risk_per_trade": 0.02}

    def test_function_still_callable(self):
        @strategy(
            name="test_callable",
            timeframe="H1",
            trading_mode=TradingMode.SWING,
        )
        def my_generator(close, high, low, volume):
            return 42

        assert my_generator(close=None, high=None, low=None, volume=None) == 42

    def test_duplicate_name_raises_error(self):
        @strategy(name="dup_name", timeframe="H1", trading_mode=TradingMode.SWING)
        def gen_a():
            pass

        with pytest.raises(ValueError, match="already registered"):
            @strategy(name="dup_name", timeframe="H1", trading_mode=TradingMode.SWING)
            def gen_b():
                pass


class TestStrategyRegistry:
    """Test StrategyRegistry get/all/names."""

    def test_get_returns_function_and_config(self):
        @strategy(name="get_test", timeframe="H1", trading_mode=TradingMode.SWING)
        def my_gen():
            return Signal(signal_type=SignalType.HOLD, confidence=0.0, price=0.0)

        fn, config = StrategyRegistry.get("get_test")
        assert fn is my_gen
        assert config.name == "get_test"

    def test_get_unknown_raises_key_error(self):
        with pytest.raises(KeyError, match="not found"):
            StrategyRegistry.get("nonexistent")

    def test_all_returns_all_strategies(self):
        @strategy(name="all_a", timeframe="H1", trading_mode=TradingMode.SWING)
        def gen_a():
            pass

        @strategy(name="all_b", timeframe="M5", trading_mode=TradingMode.M5_SCALP)
        def gen_b():
            pass

        all_strats = StrategyRegistry.all()
        assert len(all_strats) == 2
        assert "all_a" in all_strats
        assert "all_b" in all_strats

    def test_names_returns_sorted(self):
        @strategy(name="z_last", timeframe="H1", trading_mode=TradingMode.SWING)
        def gen_z():
            pass

        @strategy(name="a_first", timeframe="H1", trading_mode=TradingMode.SWING)
        def gen_a():
            pass

        assert StrategyRegistry.names() == ["a_first", "z_last"]


class TestRealGenerators:
    """Test that actual generators register correctly."""

    def test_all_three_generators_registered(self):
        # Re-register since autouse fixture clears the registry
        from broky.signals.generator import generate_signal
        from broky.signals.m5_scalp_generator import generate_m5_scalp_signal
        from broky.signals.scalp_generator import generate_scalp_signal

        for fn in [generate_signal, generate_m5_scalp_signal, generate_scalp_signal]:
            if hasattr(fn, "_strategy_config"):
                cfg = fn._strategy_config
                StrategyRegistry.register(fn, cfg)

        names = StrategyRegistry.names()
        assert "swing" in names
        assert "m5_scalp" in names
        assert "m1_scalp" in names

    def test_swing_config(self):
        from broky.signals.generator import generate_signal
        if hasattr(generate_signal, "_strategy_config"):
            StrategyRegistry.register(generate_signal, generate_signal._strategy_config)

        _, config = StrategyRegistry.get("swing")
        assert config.timeframe == "H1"
        assert config.trading_mode == TradingMode.SWING
        assert config.requires_d1_trend is True
        assert config.requires_spread is False

    def test_m5_scalp_config(self):
        from broky.signals.m5_scalp_generator import generate_m5_scalp_signal
        if hasattr(generate_m5_scalp_signal, "_strategy_config"):
            StrategyRegistry.register(generate_m5_scalp_signal, generate_m5_scalp_signal._strategy_config)

        _, config = StrategyRegistry.get("m5_scalp")
        assert config.timeframe == "M5"
        assert config.trading_mode == TradingMode.M5_SCALP
        assert config.requires_spread is True
        assert config.requires_d1_trend is True
        assert config.requires_h4_trend is True
        assert config.min_bars == 200

    def test_m1_scalp_config(self):
        from broky.signals.scalp_generator import generate_scalp_signal
        if hasattr(generate_scalp_signal, "_strategy_config"):
            StrategyRegistry.register(generate_scalp_signal, generate_scalp_signal._strategy_config)

        _, config = StrategyRegistry.get("m1_scalp")
        assert config.timeframe == "M1"
        assert config.trading_mode == TradingMode.SCALP
        assert config.requires_spread is True


class TestBacktestEngineStrategy:
    """Test BacktestEngine with strategy parameter."""

    def test_default_strategy_is_swing(self):
        # Register swing strategy first
        from broky.signals.generator import generate_signal
        if hasattr(generate_signal, "_strategy_config"):
            StrategyRegistry.register(generate_signal, generate_signal._strategy_config)

        from broky.backtest.engine import BacktestEngine
        engine = BacktestEngine()
        assert engine.strategy == "swing"

    def test_can_select_m5_scalp(self):
        # Register m5_scalp strategy
        from broky.signals.m5_scalp_generator import generate_m5_scalp_signal
        if hasattr(generate_m5_scalp_signal, "_strategy_config"):
            StrategyRegistry.register(generate_m5_scalp_signal, generate_m5_scalp_signal._strategy_config)

        from broky.backtest.engine import BacktestEngine
        engine = BacktestEngine(strategy="m5_scalp")
        assert engine.strategy == "m5_scalp"
        assert engine._strategy_config.trading_mode == TradingMode.M5_SCALP

    def test_unknown_strategy_raises(self):
        from broky.backtest.engine import BacktestEngine
        with pytest.raises(KeyError, match="not found"):
            BacktestEngine(strategy="nonexistent")