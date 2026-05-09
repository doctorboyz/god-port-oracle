"""Strategy registry — @strategy decorator and StrategyRegistry for discoverable signal generators.

Usage:
    from broky.signals.registry import strategy, StrategyRegistry

    @strategy(name="swing", timeframe="H1", trading_mode=TradingMode.SWING)
    def generate_signal(close, high, low, volume, ...):
        ...

    # Later:
    fn, config = StrategyRegistry.get("swing")
    signal = fn(close=df['close'], ...)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Optional

from shared.models import TradingMode


@dataclass
class StrategyConfig:
    """Metadata for a registered strategy."""

    name: str
    timeframe: str
    trading_mode: TradingMode
    description: str = ""
    risk_defaults: dict = field(default_factory=dict)
    indicator_config: Optional[dict] = None
    requires_spread: bool = False
    requires_d1_trend: bool = False
    requires_h4_trend: bool = False
    min_bars: int = 50


class StrategyRegistry:
    """Global registry for signal generation strategies."""

    _strategies: dict[str, tuple[Callable, StrategyConfig]] = {}

    @classmethod
    def register(cls, fn: Callable, config: StrategyConfig) -> None:
        if config.name in cls._strategies:
            raise ValueError(f"Strategy '{config.name}' already registered")
        cls._strategies[config.name] = (fn, config)

    @classmethod
    def get(cls, name: str) -> tuple[Callable, StrategyConfig]:
        if name not in cls._strategies:
            available = ", ".join(sorted(cls._strategies.keys()))
            raise KeyError(f"Strategy '{name}' not found. Available: {available}")
        return cls._strategies[name]

    @classmethod
    def all(cls) -> dict[str, tuple[Callable, StrategyConfig]]:
        return dict(cls._strategies)

    @classmethod
    def names(cls) -> list[str]:
        return sorted(cls._strategies.keys())


def strategy(
    name: str,
    timeframe: str,
    trading_mode: TradingMode,
    description: str = "",
    risk_defaults: Optional[dict] = None,
    indicator_config: Optional[dict] = None,
    requires_spread: bool = False,
    requires_d1_trend: bool = False,
    requires_h4_trend: bool = False,
    min_bars: int = 50,
):
    """Decorator that registers a signal generator function as a named strategy.

    The decorator:
    1. Attaches a StrategyConfig to the function via fn._strategy_config
    2. Registers (fn, config) in StrategyRegistry
    3. Returns the function unchanged (still callable with same args)
    """

    def decorator(fn: Callable) -> Callable:
        config = StrategyConfig(
            name=name,
            timeframe=timeframe,
            trading_mode=trading_mode,
            description=description,
            risk_defaults=risk_defaults or {},
            indicator_config=indicator_config,
            requires_spread=requires_spread,
            requires_d1_trend=requires_d1_trend,
            requires_h4_trend=requires_h4_trend,
            min_bars=min_bars,
        )
        fn._strategy_config = config
        StrategyRegistry.register(fn, config)
        return fn

    return decorator