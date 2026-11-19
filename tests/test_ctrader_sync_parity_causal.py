"""Causal proof: CTraderBridge sync-call parity surface (engine wiring, 20/11).

LiveTrader's nine self-serve points call SIX sync wrappers on whatever
bridge object they construct (get_spread_sync / get_quote_sync /
fetch_candles_sync / fetch_account_info_sync / position_exists_sync /
fetch_deal_history_sync). The dispatch's hot-path rule: the wrappers
must REUSE one persistent connection — connect is app-auth + account-auth
≈ 2s, and an engine cycle makes several bridge calls, so the MT5Bridge
one-shot connect/disconnect-per-call idiom would burn ~10s/cycle here.

Proven from real artifacts (mock transport, zero network):

1. Signature parity with metty/bridge/client.py::MT5Bridge — parameter
   names, kinds, defaults, and NOT-coroutine (sync), because the engine
   calls them from sync code.
2. One handshake for a WHOLE cycle of calls (APPLICATION_AUTH_REQ count
   stays 1) — including across the async order flows' disconnect() (the
   adapter makes that a no-op; here we prove the wrappers reconnect at
   most once after a real disconnect, never per call).
3. MT5 failure shapes: candles → empty DataFrame, the rest → None; deal
   history distinguishes None (bridge failure — do NOT reconcile) from []
   (healthy window, no deals).
4. get_deal_history maps ProtoOADealListRes to the MT5 deal-dict contract
   the engine's reconciliation reads (ticket/order/position_id/time/
   type/volume/price/profit/symbol/comment).
"""

from __future__ import annotations

import asyncio
import inspect

import pandas as pd
import pytest

from ctrader import CTraderApiError, CTraderBridge
from ctrader import messages as msg
from metty.bridge.client import MT5Bridge
from metty.core.models import AccountInfo

from tests.test_ctrader_bridge import (
    FakeTransport,
    connect_bridge,
    execution_fill_payload,
    gold_symbols_payload,
    make_config,
    open_position_payload,
    spot_event,
    symbol_by_id_payload,
    trader_payload,
)

# The six sync wrappers the engine calls, plus the async get_deal_history
# (shared with MT5Bridge) — the surface the wiring dispatch enumerates.
SYNC_PARITY_METHODS = [
    "fetch_candles_sync",
    "fetch_account_info_sync",
    "get_spread_sync",
    "get_quote_sync",
    "position_exists_sync",
    "fetch_deal_history_sync",
]


# ----------------------------------------------------------------------
# 1. Signature parity (sync — the engine calls them from sync code)
# ----------------------------------------------------------------------

@pytest.mark.parametrize("method_name", SYNC_PARITY_METHODS)
def test_sync_method_exists_on_both_bridges(method_name: str):
    assert hasattr(MT5Bridge, method_name), f"reference drifted: {method_name}"
    assert hasattr(CTraderBridge, method_name), f"missing: {method_name}"


@pytest.mark.parametrize("method_name", SYNC_PARITY_METHODS)
def test_sync_signatures_match_mt5(method_name: str):
    mt5_sig = inspect.signature(getattr(MT5Bridge, method_name))
    ct_sig = inspect.signature(getattr(CTraderBridge, method_name))

    def params_of(sig: inspect.Signature) -> list:
        out = []
        for name, p in sig.parameters.items():
            if name == "self":
                continue
            has_default = p.default is not inspect.Parameter.empty
            out.append((name, p.kind, has_default, p.default if has_default else None))
        return out

    assert params_of(ct_sig) == params_of(mt5_sig), (
        f"{method_name}: MT5 {params_of(mt5_sig)} != cTrader {params_of(ct_sig)}"
    )
    # Both must be PLAIN sync functions — the engine never awaits these.
    assert not asyncio.iscoroutinefunction(getattr(MT5Bridge, method_name))
    assert not asyncio.iscoroutinefunction(getattr(CTraderBridge, method_name))


def test_get_deal_history_async_signature_matches_mt5():
    """get_deal_history is the one ASYNC method shared with MT5Bridge."""
    mt5_sig = inspect.signature(MT5Bridge.get_deal_history)
    ct_sig = inspect.signature(CTraderBridge.get_deal_history)
    assert list(mt5_sig.parameters) == list(ct_sig.parameters)
    assert asyncio.iscoroutinefunction(MT5Bridge.get_deal_history)
    assert asyncio.iscoroutinefunction(CTraderBridge.get_deal_history)


def test_ctrader_only_async_building_blocks_exist():
    """get_spread / position_exists have no MT5 async counterpart — they
    are the cTrader-side building blocks the sync wrappers delegate to
    (the engine touches them only through the *_sync wrappers)."""
    for name in ("get_spread", "position_exists"):
        fn = getattr(CTraderBridge, name, None)
        assert callable(fn), f"missing async building block: {name}"
        assert asyncio.iscoroutinefunction(fn), f"{name} must be a coroutine"


def test_deal_list_payload_types_come_from_proto():
    """Wire contract: the IDs are the canonical proto payload types —
    PROTO_OA_DEAL_LIST_REQ/RES (OpenApiModelMessages.proto, 2133/2134)."""
    assert msg.DEAL_LIST_REQ == 2133
    assert msg.DEAL_LIST_RES == 2134


# ----------------------------------------------------------------------
# 2. Hot path — one handshake for a whole cycle of calls
# ----------------------------------------------------------------------

class TestPersistentConnection:
    """The dispatch's hard rule: NO reconnect per call."""

    def _adapter_cycle(self, transport: FakeTransport, bridge: CTraderBridge) -> None:
        """A representative engine cycle of bridge calls (all six sync
        wrappers, plus the two async order-path entry points)."""
        assert bridge.fetch_account_info_sync() is not None
        assert bridge.get_quote_sync("XAUUSD") is not None
        assert bridge.get_spread_sync("XAUUSD") is not None
        assert isinstance(bridge.fetch_candles_sync("XAUUSD", "M5", 10), pd.DataFrame)
        assert bridge.position_exists_sync(555001) is False
        assert bridge.fetch_deal_history_sync("XAUUSD", days_back=7) == []

    def test_one_handshake_for_whole_cycle_of_sync_calls(self):
        transport = FakeTransport()
        transport.replies[msg.TRADER_REQ] = [trader_payload() for _ in range(8)]
        transport.replies[msg.GET_TRENDBARS_REQ] = [
            {"period": msg.TrendbarPeriod.M5, "trendbar": []},
        ]
        # One live spot before the cycle: the engine's spread gate and
        # quote anchor need a quote or they (correctly) return None.
        asyncio.run(transport.push(spot_event()))
        bridge = CTraderBridge(make_config(), transport=transport)

        self._adapter_cycle(transport, bridge)
        self._adapter_cycle(transport, bridge)

        auth_count = len(transport.sent_of(msg.APPLICATION_AUTH_REQ))
        assert auth_count == 1, (
            f"persistent-loop rule violated: {auth_count} app-auth handshakes for "
            "two full cycles of calls (must be 1 — reconnect per call would cost "
            "~2s of auth per call and sink the engine cycle)"
        )

    def test_reconnect_at_most_once_after_real_disconnect(self):
        """After a REAL disconnect (server hangup), the next call
        reconnects exactly once — and the calls after that reuse it."""
        transport = FakeTransport()
        transport.replies[msg.TRADER_REQ] = [trader_payload() for _ in range(8)]
        bridge = CTraderBridge(make_config(), transport=transport)
        assert bridge.fetch_account_info_sync() is not None

        asyncio.run(bridge.disconnect())
        assert bridge.fetch_account_info_sync() is not None  # reconnect once
        assert bridge.fetch_account_info_sync() is not None  # reuse again

        auth_count = len(transport.sent_of(msg.APPLICATION_AUTH_REQ))
        assert auth_count == 2, (
            f"expected exactly 2 handshakes (initial + one reconnect after a "
            f"real disconnect), saw {auth_count}"
        )

    def test_sync_connect_failure_returns_none_not_raise(self):
        """Unconfigured credentials: connect() False → wrappers return the
        MT5 failure shapes, never raise into the engine cycle."""
        bridge = CTraderBridge(make_config(client_id=""), transport=FakeTransport())
        assert bridge.fetch_account_info_sync() is None
        assert bridge.get_spread_sync("XAUUSD") is None
        assert bridge.get_quote_sync("XAUUSD") is None
        assert bridge.position_exists_sync(123) is None
        assert bridge.fetch_deal_history_sync("XAUUSD") is None
        assert bridge.fetch_candles_sync("XAUUSD").equals(pd.DataFrame())


# ----------------------------------------------------------------------
# 3. Behavior + MT5 failure shapes (mock transport)
# ----------------------------------------------------------------------

class TestSyncWrapperBehavior:

    def test_account_info_is_metty_model(self):
        transport = FakeTransport()
        bridge = CTraderBridge(make_config(), transport=transport)
        transport.replies[msg.TRADER_REQ] = [trader_payload(balance_scaled=220_000)]
        info = bridge.fetch_account_info_sync()
        assert isinstance(info, AccountInfo)
        assert info.balance == 2200.0
        assert info.equity == 2200.0

    def test_spread_and_quote_from_live_spot(self):
        transport = FakeTransport()
        bridge = CTraderBridge(make_config(), transport=transport)
        # No spot yet → None (engine skips the cycle — safe, never a guess)
        assert bridge.get_spread_sync("XAUUSD") is None
        assert bridge.get_quote_sync("XAUUSD") is None
        # First spot: bid 2350.00 / ask 2350.30 → 30 points = $0.30
        asyncio.run(transport.push(spot_event(bid=2350.00, ask=2350.30)))
        bridge.ensure_connected_sync()
        import time as time_mod
        deadline = time_mod.monotonic() + 2.0
        while bridge.get_quote_sync("XAUUSD") is None and time_mod.monotonic() < deadline:
            time_mod.sleep(0.02)
        assert bridge.get_quote_sync("XAUUSD") == (2350.00, 2350.30)
        assert bridge.get_spread_sync("XAUUSD") == 30.0

    def test_position_exists_true_false_none(self):
        transport = FakeTransport()
        bridge = CTraderBridge(make_config(), transport=transport)
        assert bridge.position_exists_sync(555001) is False  # empty reconcile
        transport.replies[msg.RECONCILE_REQ] = [{"position": open_position_payload()}]
        assert bridge.position_exists_sync(555001) is True

    def test_candles_dataframe_shape(self):
        transport = FakeTransport()
        bridge = CTraderBridge(make_config(), transport=transport)
        bars = [{
            "volume": 100, "period": msg.TrendbarPeriod.M5,
            "low": int(2350.0 * 100_000), "deltaOpen": 0, "deltaClose": 0,
            "deltaHigh": 0, "utcTimestampInMinutes": 3_000_000 + i * 5,
        } for i in range(3)]
        transport.replies[msg.GET_TRENDBARS_REQ] = [{
            "period": msg.TrendbarPeriod.M5, "trendbar": bars,
        }]
        df = bridge.fetch_candles_sync("XAUUSD", "M5", 3)
        assert isinstance(df, pd.DataFrame) and not df.empty
        assert df.index.name == "timestamp"
        assert set(df.columns) == {"open", "high", "low", "close", "volume"}


def _closing_deal_payload(position_id: int = 555001) -> dict:
    """A broker-side SL close for a BUY position: closing deal is SELL,
    linked by positionId (the cTrader ticket the engine stores)."""
    return {
        "dealId": 90001,
        "orderId": 70001,
        "positionId": position_id,
        "volume": 1_830,  # cents — 0.183 lots at lotSize 10000
        "filledVolume": 1_830,
        "symbolId": 101,
        "createTimestamp": 1_761_000_360_000,
        "executionTimestamp": 1_761_000_360_000,
        "executionPrice": 2344.20,
        "tradeSide": msg.TradeSide.SELL,  # closing a BUY
        "dealStatus": msg.DealStatus.FILLED,
        "comment": "",
        "closePositionDetail": {
            "entryPrice": 2350.00,
            "grossProfit": -66_060,  # moneyDigits 2 → -660.60 USD
            "swap": 0, "commission": 0, "balance": 100_000_000,
            "moneyDigits": 2,
        },
    }


class TestDealHistory:

    def _connected(self):
        transport = FakeTransport()
        bridge = CTraderBridge(make_config(), transport=transport)
        return transport, bridge

    def test_healthy_window_no_deals_is_empty_list(self):
        transport, bridge = self._connected()
        # FakeTransport default: {"deal": [], "hasMore": False}
        deals = bridge.fetch_deal_history_sync("XAUUSD", days_back=7)
        assert deals == []

    def test_bridge_failure_is_none_not_empty(self):
        """None = "do NOT reconcile" (ISSUE-061): a [] here would make the
        engine false-close open trades at breakeven."""
        transport, bridge = self._connected()
        transport.no_reply.add(msg.DEAL_LIST_REQ)  # server never answers
        assert bridge.fetch_deal_history_sync("XAUUSD", days_back=7) is None

    def test_deal_dict_matches_mt5_contract(self):
        transport, bridge = self._connected()
        transport.replies[msg.DEAL_LIST_REQ] = [
            {"deal": [_closing_deal_payload()], "hasMore": False},
        ]
        deals = bridge.fetch_deal_history_sync("XAUUSD", days_back=7)
        assert len(deals) == 1
        deal = deals[0]
        # The exact key set the engine's reconciliation consumes
        assert set(deal.keys()) == {
            "ticket", "order", "position_id", "time", "type",
            "volume", "price", "profit", "symbol", "comment",
        }
        assert deal["position_id"] == 555001  # Strategy-1 match key
        assert deal["type"] == 1  # SELL deal (closing a BUY) — MT5 type codes
        assert deal["time"] == 1_761_000_360.0  # epoch SECONDS
        assert deal["price"] == 2344.20
        assert abs(deal["profit"] + 660.60) < 1e-6
        assert deal["volume"] == 0.183
        assert deal["symbol"] == "XAUUSD"

    def test_deal_pages_backward_on_has_more(self):
        import time as time_mod
        now_ms = int(time_mod.time() * 1000)
        transport, bridge = self._connected()
        first = dict(_closing_deal_payload())
        first["dealId"] = 90001
        first["createTimestamp"] = now_ms - 60_000
        second = dict(_closing_deal_payload())
        second["dealId"] = 90002
        second["createTimestamp"] = now_ms - 3600_000
        transport.replies[msg.DEAL_LIST_REQ] = [
            {"deal": [first], "hasMore": True},
            {"deal": [second], "hasMore": False},
        ]
        deals = bridge.fetch_deal_history_sync("XAUUSD", days_back=7)
        assert [d["ticket"] for d in deals] == [90001, 90002]
        # The second request must walk the cursor BACKWARD from the oldest
        # deal of the first page (server caps rows per response).
        second_req = transport.sent_of(msg.DEAL_LIST_REQ)[1]
        assert second_req.payload["toTimestamp"] == (now_ms - 60_000) - 1

    def test_get_deal_history_raises_on_wire_error(self):
        """Async error parity with MT5Bridge.get_deal_history: failures
        raise; only the sync wrapper converts them to None."""
        transport, bridge = self._connected()
        bridge.ensure_connected_sync()
        transport.no_reply.add(msg.DEAL_LIST_REQ)  # server goes silent
        with pytest.raises(asyncio.TimeoutError):
            asyncio.run(asyncio.wait_for(
                bridge.get_deal_history("XAUUSD", days_back=7), timeout=5.0,
            ))

    def test_unknown_symbol_sync_returns_none_async_raises(self):
        """Unknown symbol: the sync wrapper gives the engine None (skip
        reconciliation — safe); the async building block raises so direct
        callers cannot mistake a config bug for an empty window."""
        transport, bridge = self._connected()
        assert bridge.fetch_deal_history_sync("NOTASYMBOL", days_back=7) is None
        with pytest.raises(CTraderApiError):
            asyncio.run(bridge.get_deal_history("NOTASYMBOL", days_back=7))


# ----------------------------------------------------------------------
# 4. Persistent-loop facility guards
# ----------------------------------------------------------------------

class TestSyncLoopGuards:

    def test_run_coroutine_sync_from_own_loop_raises(self):
        """Deadlock guard: submitting to the persistent loop FROM the
        persistent loop must fail loud, not hang the engine forever."""
        transport = FakeTransport()
        bridge = CTraderBridge(make_config(), transport=transport)
        bridge.ensure_connected_sync()

        async def _offender():
            # The guard raises on the loop check BEFORE the coroutine arg
            # is touched — no coroutine needed here.
            return bridge.run_coroutine_sync(None)

        coro = _offender()
        with pytest.raises(RuntimeError):
            bridge.run_coroutine_sync(coro)
        coro.close()

    def test_wrapper_error_never_raises_into_engine(self):
        """A bridge method that explodes must surface as the wrapper's
        MT5 failure shape (None), not crash the trading cycle."""
        transport = FakeTransport()
        bridge = CTraderBridge(make_config(), transport=transport)
        transport.no_reply.add(msg.TRADER_REQ)  # every account-info call times out
        assert bridge.fetch_account_info_sync() is None
