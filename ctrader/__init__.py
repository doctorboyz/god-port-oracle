"""cTrader Open API adapter — MT5Bridge-compatible broker surface.

Public API: CTraderBridge, CTraderConfig, EndPoints, CTraderApiError,
plus transport/protocol pieces for connection-day wiring and tests.

This package is additive: no engine, farm or MT5 file imports or depends on
it yet. When the demo account passes, the engine can swap its bridge class
without touching core code (contract: tests/test_ctrader_contract.py).
"""

from .bridge import CTraderApiError, CTraderBridge
from .config import CTraderConfig
from .endpoints import EndPoints

__all__ = [
    "CTraderApiError",
    "CTraderBridge",
    "CTraderConfig",
    "EndPoints",
]
