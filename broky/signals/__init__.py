"""Signal generation — strategies, groups, and registry."""

from broky.signals.registry import StrategyRegistry, strategy, StrategyConfig
from broky.signals.generator import generate_signal
from broky.signals.m5_scalp_generator import generate_m5_scalp_signal

__all__ = [
    "StrategyRegistry", "strategy", "StrategyConfig",
    "generate_signal", "generate_m5_scalp_signal",
]