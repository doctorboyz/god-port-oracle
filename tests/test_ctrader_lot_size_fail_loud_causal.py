"""Causal tests — lotSize fallback must fail loud, never guess (P22 wiring 20/11).

Job point 8 (Hermes flag from 2026-11-11, iron-locked then, carved out now):
the three lotSize fallbacks in ctrader/bridge.py (`or 100`, `= 100.0`,
`.get(symbol_id, 100.0)`) were "correct by accident" under the removed
x100 conversion (b8378ce). With volume_cents = lots x lotSize, a missing
lotSize for XAUUSD (real wire value 10000) silently ships orders 100x
mis-scaled on the ORDER path — the worst place for a silent fallback.

Hypothesis: the fallback paths, not the happy path, are the defect —
when SymbolByIdRes carries no usable lotSize, every consumer
(connect-time load, order-time lookup) must raise CTraderApiError
instead of inventing a number.

RED proof: every fail-loud test below fails against the fallback code
(silent 100.0 continues) and the wire-truth control stays green —
flip the fallback back in and exactly these go red again.
"""

import asyncio
import inspect

import pytest

from ctrader import CTraderApiError, CTraderBridge
from ctrader import messages as msg

from tests.test_ctrader_bridge import (
    FakeTransport,
    assets_payload,
    connect_bridge,
    gold_symbols_payload,
    make_config,
    spot_event,
)


def _reply(transport: FakeTransport, payload_type: int, payload: dict) -> None:
    transport.replies[payload_type] = [payload]


async def test_connect_fails_loud_when_lot_size_field_missing():
    """SymbolByIdRes entry exists but carries no lotSize — the old code
    wrote a silent 100.0 and every order afterwards was 100x off."""
    transport = FakeTransport()
    _reply(transport, msg.SYMBOLS_LIST_REQ, gold_symbols_payload())
    _reply(transport, msg.SYMBOL_BY_ID_REQ, {"symbol": [{
        "symbolId": 101, "symbolName": "XAUUSD", "minVolume": 1,
    }]})
    _reply(transport, msg.ASSET_LIST_REQ, assets_payload())
    bridge = CTraderBridge(make_config(), transport=transport)

    assert await bridge.connect() is False  # fail at boot, not mid-order
    assert isinstance(bridge.last_connect_error, CTraderApiError)
    assert bridge.last_connect_error.code == "LOT_SIZE_MISSING"


async def test_connect_fails_loud_when_symbol_entry_missing():
    """SymbolByIdRes answered but not for our symbolId — refuse, don't guess."""
    transport = FakeTransport()
    _reply(transport, msg.SYMBOLS_LIST_REQ, gold_symbols_payload())
    _reply(transport, msg.SYMBOL_BY_ID_REQ, {"symbol": [{
        "symbolId": 555, "symbolName": "OTHERUSD", "lotSize": 10_000,
    }]})
    _reply(transport, msg.ASSET_LIST_REQ, assets_payload())
    bridge = CTraderBridge(make_config(), transport=transport)

    assert await bridge.connect() is False
    assert isinstance(bridge.last_connect_error, CTraderApiError)
    assert bridge.last_connect_error.code == "LOT_SIZE_NOT_FOUND"


async def test_lot_size_lookup_raises_when_never_loaded():
    """Order-time lookup for a symbol whose lotSize was never fetched:
    the old `.get(symbol_id, 100.0)` silently mis-scaled the volume."""
    transport = FakeTransport()
    bridge = await connect_bridge(transport)

    # Control — the real wire value must survive the fail-loud change:
    assert bridge._lot_size(101) == 10_000.0
    with pytest.raises(CTraderApiError) as exc:
        bridge._lot_size(999)
    assert exc.value.code == "LOT_SIZE_NOT_LOADED"
    await bridge.disconnect()


async def test_send_order_refuses_when_lot_size_never_loaded():
    """The order path must refuse (raise) rather than send a 100x-wrong
    volume. EURUSD resolves (symbolId 999) but its lotSize was never
    loaded — connect only loads the default symbol."""
    transport = FakeTransport()
    bridge = await connect_bridge(transport)
    await transport.push(spot_event(symbol_id=999))
    await asyncio.sleep(0.01)  # let the receive loop cache the quote first

    with pytest.raises(CTraderApiError) as exc:
        await bridge.send_order("EURUSD", "BUY", 1.0)
    assert exc.value.code == "LOT_SIZE_NOT_LOADED"
    # No order left the building.
    assert transport.sent_of(msg.NEW_ORDER_REQ) == []
    await bridge.disconnect()


def test_no_silent_lot_size_fallback_left_in_source():
    """Regression guard: the fallback literals must not come back. The
    fail-loud methods must raise CTraderApiError and contain no numeric
    fallback at all (any '100' in these two methods IS the bug)."""
    for method in (CTraderBridge._load_lot_size, CTraderBridge._lot_size):
        source = inspect.getsource(method)
        assert "CTraderApiError" in source, (
            f"{method.__name__} lost its fail-loud raise"
        )
        assert "100" not in source, (
            f"{method.__name__} reintroduced a numeric lotSize fallback — "
            "volume_cents = lots x lotSize means a guessed lotSize ships "
            "silently mis-scaled orders (XAUUSD wire truth: 10000)"
        )
