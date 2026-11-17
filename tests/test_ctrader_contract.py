"""Contract test: CTraderBridge surface parity with MT5Bridge.

God Port's engine trades through MT5Bridge today. The day it swaps to
cTrader, this contract must hold so no engine code changes:

1. same method names
2. same parameter names (positional order) and kinds
3. same return shapes: get_account_info -> metty AccountInfo,
   send_order -> metty OrderResult, get_positions -> list of dicts with the
   exact MT5 keys, get_candles -> DataFrame (timestamp index, OHLCV),
   close_position -> bool, close_position_with_fill -> (bool, float|None),
   connect -> bool, disconnect -> None, health_check -> bool

This test reflects on the REAL classes (metty/bridge/client.py is imported
read-only — it is never modified) and runs behavioral checks against the
mock-driven CTraderBridge (helpers shared with test_ctrader_bridge.py).
"""

import asyncio
import inspect
import time as time_mod

import pandas as pd
import pytest

from ctrader import CTraderBridge
from ctrader import messages as msg
from metty.bridge.client import MT5Bridge
from metty.core.models import AccountInfo, OrderResult

from tests.test_ctrader_bridge import (
    FakeTransport,
    connect_bridge,
    execution_fill_payload,
    open_position_payload,
    push_execution_reply,
    spot_event,
    trader_payload,
)

# The parity surface the engine actually calls (per the task spec, plus
# close_position_with_fill which the trader/kill paths use on MT5).
PARITY_METHODS = [
    "connect",
    "disconnect",
    "get_account_info",
    "get_positions",
    "get_candles",
    "send_order",
    "close_position",
    "health_check",
    "close_position_with_fill",
]


# ----------------------------------------------------------------------
# 1. Method-name parity
# ----------------------------------------------------------------------

def test_ctader_bridge_exposes_every_parity_method_name():
    for name in PARITY_METHODS:
        assert hasattr(CTraderBridge, name), f"missing MT5Bridge method: {name}"
        assert callable(getattr(CTraderBridge, name))


def test_parity_methods_exist_on_mt5_bridge_reference():
    for name in PARITY_METHODS:
        assert hasattr(MT5Bridge, name), f"reference drifted: {name} gone from MT5Bridge"


# ----------------------------------------------------------------------
# 2. Signature parity (parameter names, kinds, defaults, async-ness)
# ----------------------------------------------------------------------

@pytest.mark.parametrize("method_name", PARITY_METHODS)
def test_method_signatures_match_mt5(method_name: str):
    mt5_sig = inspect.signature(getattr(MT5Bridge, method_name))
    ct_sig = inspect.signature(getattr(CTraderBridge, method_name))

    def params_of(sig: inspect.Signature) -> list:
        out = []
        for name, p in sig.parameters.items():
            if name == "self":
                continue
            has_default = p.default is not inspect.Parameter.empty
            # Keep "no default" and "default=None" distinguishable so drift
            # like count: int = 500 -> count: int = None cannot pass silently.
            out.append((name, p.kind, has_default, p.default if has_default else None))
        return out

    assert params_of(ct_sig) == params_of(mt5_sig), (
        f"{method_name}: MT5 params {params_of(mt5_sig)} != cTrader params {params_of(ct_sig)}"
    )
    # Both must be coroutines (the engine awaits them).
    assert asyncio.iscoroutinefunction(getattr(MT5Bridge, method_name))
    assert asyncio.iscoroutinefunction(getattr(CTraderBridge, method_name))


# ----------------------------------------------------------------------
# 3. Return-shape parity, proven with the mock transport
# ----------------------------------------------------------------------

async def test_get_account_info_returns_metty_account_info_model():
    transport = FakeTransport()
    bridge = await connect_bridge(transport)
    transport.replies[msg.TRADER_REQ] = [trader_payload()]

    info = await bridge.get_account_info()
    assert isinstance(info, AccountInfo)
    assert set(type(info).model_fields.keys()) == set(AccountInfo.model_fields.keys())
    await bridge.disconnect()


async def test_send_order_returns_metty_order_result_model():
    transport = FakeTransport()
    bridge = await connect_bridge(transport)
    await transport.push(spot_event())
    await asyncio.sleep(0.01)

    await push_execution_reply(transport, msg.NEW_ORDER_REQ, execution_fill_payload())
    result = await bridge.send_order("XAUUSD", "BUY", 1.0)
    assert isinstance(result, OrderResult)
    assert set(type(result).model_fields.keys()) == set(OrderResult.model_fields.keys())
    assert result.success is True
    await bridge.disconnect()


async def test_get_positions_dict_keys_match_mt5_contract():
    transport = FakeTransport()
    bridge = await connect_bridge(transport)
    transport.replies[msg.RECONCILE_REQ] = [
        {"position": open_position_payload()},
    ]
    await transport.push(spot_event())
    await asyncio.sleep(0.01)

    positions = await bridge.get_positions("XAUUSD")
    assert positions, "mock server must expose one gold position"
    expected_keys = {
        "ticket", "symbol", "type", "volume", "price_open", "price_current",
        "sl", "tp", "profit", "time",
    }
    assert set(positions[0].keys()) == expected_keys

    pos = positions[0]
    assert isinstance(pos["ticket"], int)
    assert isinstance(pos["symbol"], str)
    assert pos["type"] in ("BUY", "SELL")
    assert isinstance(pos["volume"], float)
    assert isinstance(pos["profit"], float)
    assert hasattr(pos["time"], "year")  # datetime-like, like MT5
    await bridge.disconnect()


async def test_get_candles_dataframe_shape_matches_mt5_contract():
    transport = FakeTransport()
    bridge = await connect_bridge(transport)

    bars = [{
        "volume": 100, "period": msg.TrendbarPeriod.M5,
        "low": int(2350.0 * 100_000), "deltaOpen": 0, "deltaClose": 0,
        "deltaHigh": 0, "utcTimestampInMinutes": 3_000_000 + i * 5,
    } for i in range(3)]
    transport.replies[msg.GET_TRENDBARS_REQ] = [{
        "period": msg.TrendbarPeriod.M5,
        "timestamp": int(time_mod.time() * 1000),
        "trendbar": bars,
    }]
    df = await bridge.get_candles("XAUUSD", "M5", 3)
    assert isinstance(df, pd.DataFrame)
    assert not df.empty
    assert df.index.name == "timestamp"
    assert set(df.columns) == {"open", "high", "low", "close", "volume"}
    await bridge.disconnect()


async def test_connect_disconnect_health_check_return_types():
    transport = FakeTransport()
    bridge = await connect_bridge(transport)
    assert isinstance(await bridge.health_check(), bool)
    assert await bridge.disconnect() is None
    assert isinstance(await bridge.connect(), bool)


async def test_close_position_and_with_fill_return_types():
    transport = FakeTransport()
    bridge = await connect_bridge(transport)
    transport.replies[msg.RECONCILE_REQ] = [
        {"position": open_position_payload(position_id=555001)},
    ]
    await push_execution_reply(
        transport, msg.CLOSE_POSITION_REQ,
        execution_fill_payload(position_id=555001, fill_price=2351.0),
    )
    closed = await bridge.close_position(555001)
    assert isinstance(closed, bool)
    assert closed is True
    await bridge.disconnect()


async def test_close_position_with_fill_tuple_shape():
    transport = FakeTransport()
    bridge = await connect_bridge(transport)
    transport.replies[msg.RECONCILE_REQ] = [
        {"position": open_position_payload(position_id=555001)},
    ]
    await push_execution_reply(
        transport, msg.CLOSE_POSITION_REQ,
        execution_fill_payload(position_id=555001, fill_price=2351.5),
    )
    ok, fill = await bridge.close_position_with_fill(555001)
    assert ok is True
    assert fill == 2351.5
    await bridge.disconnect()


# ----------------------------------------------------------------------
# 4. Interface-level parity extras
# ----------------------------------------------------------------------

def test_symbol_property_exists_like_mt5():
    """MT5Bridge exposes `symbol` (resolved broker name); so must we."""
    assert isinstance(getattr(MT5Bridge, "symbol", None), property)
    assert isinstance(getattr(CTraderBridge, "symbol", None), property)


def test_constructor_takes_config_first_like_mt5():
    """Both take a config object as the first parameter."""
    mt5_params = list(inspect.signature(MT5Bridge.__init__).parameters)[1:]
    ct_params = list(inspect.signature(CTraderBridge.__init__).parameters)[1:]
    assert mt5_params[0] == "config"
    assert ct_params[0] == "config"


def test_no_mt5_bridge_coupling_inside_ctrader_package():
    """IRON guard: the adapter must not couple to the MT5 implementation.

    Only the shared result models (metty.core.models) may be imported — that
    is the engine contract, not MT5-specific code. Any rpyc/bridge import
    would leak the old broker into the new adapter. (Docstrings may mention
    MT5Bridge for parity context — we assert on actual import statements.)
    """
    import ctrader.bridge as bridge_module
    import ctrader.config as config_module

    for module in (bridge_module, config_module):
        source = inspect.getsource(module)
        import_lines = [
            line.strip() for line in source.splitlines()
            if line.strip().startswith(("from metty", "import metty"))
        ]
        for line in import_lines:
            assert "metty.core.models" in line, f"unexpected metty import: {line}"
        assert "rpyc" not in source, f"{module.__name__} must not touch rpyc"
    # The bridge itself must consume the shared result models (the contract).
    assert any(
        "metty.core.models" in line
        for line in inspect.getsource(bridge_module).splitlines()
        if line.strip().startswith("from metty")
    )
