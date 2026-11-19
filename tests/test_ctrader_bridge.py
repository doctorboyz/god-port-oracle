"""Unit tests for the cTrader adapter — mock-only, zero network.

The FakeTransport below is a scripted in-process server speaking the same
newline-JSON frames the real ctraderapi.com:5036 endpoint uses. Every
payloadType ID and field name in the scripted replies comes from the
canonical .proto (ctrader/proto_reference/). Nothing here connects to
cTrader, opens accounts, or stores credentials.

Run: .venv/bin/python -m pytest tests/test_ctrader_bridge.py -q
"""

import asyncio
import json
import time
from typing import Any, Optional

import pytest

from ctrader import CTraderApiError, CTraderBridge, CTraderConfig
from ctrader import messages as msg
from ctrader.protocol import ApiMessage, decode_message, encode_frame, price_to_real


def make_config(**overrides: Any) -> CTraderConfig:
    """Configured-for-demo config with fast timings (env-only pattern)."""
    base = dict(
        name="ctrader-demo-test",
        client_id="test-client-id",
        client_secret="test-client-secret",
        access_token="test-access-token",
        account_id=6194964,
        host_type="demo",
        symbol="XAUUSD",
        request_timeout=1.0,
        heartbeat_interval=0.05,
        trendbar_min_interval=0.0,
    )
    base.update(overrides)
    return CTraderConfig(**base)


# ----------------------------------------------------------------------
# Server payload builders (field names from ctrader/proto_reference/)
# ----------------------------------------------------------------------

def gold_symbols_payload(symbol_id: int = 101, name: str = "XAUUSD") -> dict[str, Any]:
    return {"symbol": [
        {"symbolId": symbol_id, "symbolName": name, "enabled": True},
        {"symbolId": 999, "symbolName": "EURUSD", "enabled": True},
    ]}


def symbol_by_id_payload(symbol_id: int = 101, lot_size: int = 10_000) -> dict[str, Any]:
    # lotSize=10000 is the real SymbolByIdRes value for XAUUSD (19/11) and
    # is already in cents — volume_cents = lots x lotSize (no extra x100)
    return {"symbol": [{
        "symbolId": symbol_id, "symbolName": "XAUUSD", "lotSize": lot_size,
        "minVolume": 1, "maxVolume": 5_000_000,
    }]}


def assets_payload(usd_asset: int = 1) -> dict[str, Any]:
    return {"asset": [
        {"assetId": usd_asset, "name": "USD"},
        {"assetId": usd_asset + 1, "name": "EUR"},
    ]}


def trader_payload(balance_scaled: int = 10_000_000, money_digits: int = 2) -> dict[str, Any]:
    return {"trader": {
        "ctidTraderAccountId": 6194964,
        "balance": balance_scaled,
        "moneyDigits": money_digits,
        "depositAssetId": 1,
        "leverageInCents": 100_000,  # 1:1000
        "maxLeverage": 1000,
        "traderLogin": 6194964,
    }}


def spot_event(symbol_id: int = 101, bid: float = 2350.00, ask: float = 2350.30) -> ApiMessage:
    return ApiMessage(payload_type=msg.SPOT_EVENT, payload={
        "ctidTraderAccountId": 6194964,
        "symbolId": symbol_id,
        "bid": int(bid * 100_000),
        "ask": int(ask * 100_000),
        "timestamp": int(time.time()),
    })


def open_position_payload(
    position_id: int = 555001,
    symbol_id: int = 101,
    volume_cents: int = 10_000,  # 1.0 lot of gold
    side: int = msg.TradeSide.BUY,
    entry: float = 2345.00,
    sl: float = 2340.00,
    tp: float = 2360.00,
) -> list[dict[str, Any]]:
    return [{
        "positionId": position_id,
        "tradeData": {
            "symbolId": symbol_id,
            "volume": volume_cents,
            "tradeSide": side,
            "openTimestamp": 1_761_000_000_000,
            "label": "god-port-ctrader-demo-test",
        },
        "positionStatus": msg.PositionStatus.POSITION_STATUS_OPEN,
        "price": entry,
        "stopLoss": sl,
        "takeProfit": tp,
        "usedMargin": 2_350,  # moneyDigits=2 -> 23.50
        "moneyDigits": 2,
    }]


def execution_fill_payload(
    position_id: int = 555002,
    fill_price: float = 2350.20,
    filled_cents: int = 10_000,
    execution_type: int = msg.ExecutionType.ORDER_FILLED,
    error_code: Optional[str] = None,
) -> dict[str, Any]:
    return {
        "ctidTraderAccountId": 6194964,
        "executionType": execution_type,
        "position": {"positionId": position_id, "price": fill_price},
        "order": {"orderId": 70001, "executionPrice": fill_price},
        "deal": {
            "dealId": 80001, "orderId": 70001, "positionId": position_id,
            "volume": filled_cents, "filledVolume": filled_cents,
            "executionPrice": fill_price,
            "tradeSide": msg.TradeSide.BUY,
            "dealStatus": msg.DealStatus.FILLED,
        },
        "errorCode": error_code,
    }


def error_res(error_code: str, description: str = "") -> ApiMessage:
    """Scripted ProtoOAErrorRes reply (payload type 2142)."""
    return ApiMessage(
        payload_type=msg.OA_ERROR_RES,
        payload={"errorCode": error_code, "description": description},
    )


class FakeTransport:
    """In-process cTrader server: scripted replies + event pushes.

    Plain-dict replies are auto-wrapped as the matching *Res envelope
    (every Req in this API answers with payloadType + 1). To script an
    error, pass an ApiMessage (e.g. error_res(...)) — it is pushed as-is.
    `no_reply` holds payload types the server deliberately never answers.
    Everything the bridge sends is recorded in `sent_messages`.
    """

    DEFAULT_REPLIES: dict[int, dict[str, Any]] = {
        msg.APPLICATION_AUTH_REQ: {},
        msg.ACCOUNT_AUTH_REQ: {},
        msg.VERSION_REQ: {},
        msg.SUBSCRIBE_SPOTS_REQ: {},
        msg.RECONCILE_REQ: {"position": []},
        msg.GET_TRENDBARS_REQ: {"trendbar": []},
        # Engine wiring (20/11): reconciliation deals — empty window by
        # default so a stray deal-list request never hangs a test for a
        # full request_timeout.
        msg.DEAL_LIST_REQ: {"deal": [], "hasMore": False},
        # Enough of a broker for a bare connect() to succeed:
        msg.SYMBOLS_LIST_REQ: lambda: gold_symbols_payload(),
        msg.SYMBOL_BY_ID_REQ: lambda: symbol_by_id_payload(),
        msg.ASSET_LIST_REQ: lambda: assets_payload(),
    }

    def __init__(self, replies: Optional[dict[int, list[Any]]] = None) -> None:
        self.replies: dict[int, list[Any]] = replies or {}
        self.no_reply: set[int] = set()
        self.sent_messages: list[ApiMessage] = []
        self._queue: asyncio.Queue[bytes | None] = asyncio.Queue()
        self.connected = False

    # --- transport contract ---

    async def connect(self) -> None:
        self.connected = True

    async def close(self) -> None:
        self.connected = False

    async def send(self, data: bytes) -> None:
        message = decode_message(data.rstrip(b"\n"))
        self.sent_messages.append(message)
        reply = self._next_reply(message)
        if reply is not None:
            if isinstance(reply, ApiMessage):
                await self.push(ApiMessage(
                    payload_type=reply.payload_type,
                    payload=reply.payload,
                    client_msg_id=message.client_msg_id,
                ))
            else:
                # Req (even/odd alike) -> Res is payloadType + 1 in this API.
                await self.push(ApiMessage(
                    payload_type=message.payload_type + 1,
                    payload=reply,
                    client_msg_id=message.client_msg_id,
                ))

    def messages(self):
        return self._iterate()

    async def _iterate(self):
        while True:
            item = await self._queue.get()
            if item is None:
                return
            yield item

    # --- scripting helpers ---

    def _next_reply(self, request: ApiMessage) -> Optional[Any]:
        if request.payload_type in self.no_reply:
            return None
        queue = self.replies.get(request.payload_type)
        if queue:
            return queue.pop(0)
        default = self.DEFAULT_REPLIES.get(request.payload_type)
        # Fresh data each time (defaults may be builders — e.g. symbols list).
        return default() if callable(default) else default

    async def push(self, message: ApiMessage) -> None:
        await self._queue.put(encode_frame(message))

    async def drop_connection(self) -> None:
        """Simulate the server hanging up: frames iterator ends."""
        await self._queue.put(None)

    def sent_of(self, payload_type: int) -> list[ApiMessage]:
        return [m for m in self.sent_messages if m.payload_type == payload_type]



async def connect_bridge(
    transport: FakeTransport, config: Optional[CTraderConfig] = None,
) -> CTraderBridge:
    """Connect with standard server scripting (gold symbol + USD assets)."""
    transport.replies[msg.SYMBOLS_LIST_REQ] = [gold_symbols_payload()]
    transport.replies[msg.SYMBOL_BY_ID_REQ] = [symbol_by_id_payload()]
    transport.replies[msg.ASSET_LIST_REQ] = [assets_payload()]
    bridge = CTraderBridge(config or make_config(), transport=transport)
    assert await bridge.connect() is True
    return bridge


async def push_execution_reply(
    transport: FakeTransport, request_type: int, event_payload: dict[str, Any],
) -> None:
    """Wrap transport.send so the given request type gets an execution event."""
    original_send = transport.send

    async def wrapped(data: bytes) -> None:
        await original_send(data)
        last = transport.sent_messages[-1]
        if last.payload_type == request_type:
            await transport.push(ApiMessage(
                payload_type=msg.EXECUTION_EVENT,
                payload=event_payload,
                client_msg_id=last.client_msg_id,
            ))

    transport.send = wrapped  # type: ignore[method-assign]


# ----------------------------------------------------------------------
# Auth flow — two layers
# ----------------------------------------------------------------------

async def test_connect_sends_application_auth_then_account_auth():
    transport = FakeTransport()
    bridge = CTraderBridge(make_config(), transport=transport)
    assert await bridge.connect() is True

    first, second = transport.sent_messages[0], transport.sent_messages[1]
    assert first.payload_type == msg.APPLICATION_AUTH_REQ
    assert first.payload == {
        "clientId": "test-client-id",
        "clientSecret": "test-client-secret",
    }
    assert second.payload_type == msg.ACCOUNT_AUTH_REQ
    assert second.payload == {
        "ctidTraderAccountId": 6194964,
        "accessToken": "test-access-token",
    }
    await bridge.disconnect()


async def test_connect_returns_false_when_credentials_missing():
    bridge = CTraderBridge(
        make_config(client_id="", access_token=""), transport=FakeTransport(),
    )
    assert await bridge.connect() is False


async def test_connect_returns_false_on_application_auth_error():
    transport = FakeTransport()
    transport.replies[msg.APPLICATION_AUTH_REQ] = [
        error_res("CH_ACCESS_DENIED", "bad credentials"),
    ]
    bridge = CTraderBridge(make_config(), transport=transport)
    assert await bridge.connect() is False


async def test_connect_returns_false_on_account_auth_error():
    transport = FakeTransport()
    transport.replies[msg.ACCOUNT_AUTH_REQ] = [
        error_res("CH_ACCESS_TOKEN_EXPIRED", "refresh the token"),
    ]
    bridge = CTraderBridge(make_config(), transport=transport)
    assert await bridge.connect() is False


async def test_connect_returns_false_when_symbol_not_listed():
    transport = FakeTransport()
    transport.replies[msg.SYMBOLS_LIST_REQ] = [gold_symbols_payload(name="EURUSD")]
    transport.replies[msg.SYMBOL_BY_ID_REQ] = [symbol_by_id_payload()]
    transport.replies[msg.ASSET_LIST_REQ] = [assets_payload()]
    bridge = CTraderBridge(make_config(), transport=transport)
    assert await bridge.connect() is False


async def test_connect_subscribes_spots_for_gold():
    transport = FakeTransport()
    bridge = await connect_bridge(transport)
    subscribe_calls = transport.sent_of(msg.SUBSCRIBE_SPOTS_REQ)
    assert len(subscribe_calls) == 1
    assert subscribe_calls[0].payload["symbolId"] == [101]
    assert subscribe_calls[0].payload["ctidTraderAccountId"] == 6194964
    await bridge.disconnect()


async def test_connect_resolves_host_from_config():
    from ctrader.endpoints import EndPoints

    transport = FakeTransport()
    bridge = CTraderBridge(make_config(host_type="live"), transport=transport)
    assert bridge.host == EndPoints.JSON_LIVE_HOST
    assert bridge.port == 5036
    bridge_demo = CTraderBridge(make_config(), transport=FakeTransport())
    assert bridge_demo.host == EndPoints.JSON_DEMO_HOST
    assert bridge_demo.port == 5036


# ----------------------------------------------------------------------
# Symbol mapping
# ----------------------------------------------------------------------

async def test_symbol_resolution_exact_and_case_insensitive():
    transport = FakeTransport()
    bridge = await connect_bridge(transport)
    assert bridge.symbol == "XAUUSD"
    assert bridge._symbol_id("xauusd") == 101
    assert bridge._symbol_id("XAUUSD") == 101
    await bridge.disconnect()


async def test_unknown_symbol_raises_api_error():
    transport = FakeTransport()
    bridge = await connect_bridge(transport)
    with pytest.raises(CTraderApiError, match="SYMBOL_NOT_FOUND"):
        bridge._symbol_id("USDCAD")
    await bridge.disconnect()


# ----------------------------------------------------------------------
# Spots and quotes
# ----------------------------------------------------------------------

async def test_spot_event_updates_quote_cache():
    transport = FakeTransport()
    bridge = await connect_bridge(transport)
    await transport.push(spot_event(bid=2350.0, ask=2350.5))
    await asyncio.sleep(0.01)
    assert await bridge.get_quote("XAUUSD") == pytest.approx((2350.0, 2350.5))
    await bridge.disconnect()


async def test_get_quote_returns_none_before_any_spot():
    transport = FakeTransport()
    bridge = await connect_bridge(transport)
    assert await bridge.get_quote("XAUUSD") is None
    await bridge.disconnect()


# ----------------------------------------------------------------------
# Account info
# ----------------------------------------------------------------------

async def test_get_account_info_maps_balance_leverage_currency():
    transport = FakeTransport()
    bridge = await connect_bridge(transport)
    transport.replies[msg.TRADER_REQ] = [trader_payload(balance_scaled=10_000_000)]

    info = await bridge.get_account_info()
    assert info is not None
    assert info.balance == pytest.approx(100_000.0)  # 10_000_000 / 10^2
    assert info.leverage == 1000  # leverageInCents 100000 -> 1:1000
    assert info.currency == "USD"  # depositAssetId 1 -> assets map
    assert info.name == "ctrader-demo-test"
    # No open positions -> equity == balance, margin 0
    assert info.equity == pytest.approx(100_000.0)
    assert info.margin == pytest.approx(0.0)
    await bridge.disconnect()


async def test_get_account_info_adds_unrealized_pnl_and_margin():
    transport = FakeTransport()
    bridge = await connect_bridge(transport)
    transport.replies[msg.TRADER_REQ] = [trader_payload(balance_scaled=10_000_000)]
    transport.replies[msg.RECONCILE_REQ] = [
        {"position": open_position_payload(entry=2345.0, volume_cents=10_000)},
    ]
    await transport.push(spot_event(bid=2355.0, ask=2355.3))
    await asyncio.sleep(0.01)

    info = await bridge.get_account_info()
    assert info is not None
    # BUY 100 oz @ 2345, bid now 2355 => +1000.0 on top of balance
    assert info.equity == pytest.approx(101_000.0)
    # usedMargin 2350 / 10^2 = 23.50
    assert info.margin == pytest.approx(23.50)
    assert info.free_margin == pytest.approx(101_000.0 - 23.50)
    await bridge.disconnect()


async def test_get_account_info_returns_none_on_api_error():
    transport = FakeTransport()
    bridge = await connect_bridge(transport)
    transport.replies[msg.TRADER_REQ] = [error_res("TIMEOUT_ERROR", "slow")]
    assert await bridge.get_account_info() is None
    await bridge.disconnect()


# ----------------------------------------------------------------------
# Positions
# ----------------------------------------------------------------------

async def test_get_positions_maps_position_fields_to_mt5_shape():
    transport = FakeTransport()
    bridge = await connect_bridge(transport)
    transport.replies[msg.RECONCILE_REQ] = [
        {"position": open_position_payload(position_id=555001)},
    ]
    await transport.push(spot_event(bid=2350.0, ask=2350.3))
    await asyncio.sleep(0.01)

    positions = await bridge.get_positions("XAUUSD")
    assert len(positions) == 1
    pos = positions[0]
    assert set(pos.keys()) == {
        "ticket", "symbol", "type", "volume", "price_open", "price_current",
        "sl", "tp", "profit", "time",
    }
    assert pos["ticket"] == 555001
    assert pos["symbol"] == "XAUUSD"
    assert pos["type"] == "BUY"
    assert pos["volume"] == pytest.approx(1.0)  # 10000 cents / lotSize 10000
    assert pos["price_open"] == pytest.approx(2345.0)
    assert pos["price_current"] == pytest.approx(2350.0)
    assert pos["sl"] == pytest.approx(2340.0)
    assert pos["tp"] == pytest.approx(2360.0)
    # BUY 100 oz: (2350 - 2345) * 100 = 500.0
    assert pos["profit"] == pytest.approx(500.0)
    assert pos["time"].year >= 2025
    await bridge.disconnect()


async def test_get_positions_filters_other_symbols():
    transport = FakeTransport()
    bridge = await connect_bridge(transport)
    transport.replies[msg.RECONCILE_REQ] = [
        {"position": open_position_payload(symbol_id=999)},
    ]
    assert await bridge.get_positions("XAUUSD") == []
    await bridge.disconnect()


async def test_get_positions_sell_side_pnl_uses_ask():
    transport = FakeTransport()
    bridge = await connect_bridge(transport)
    transport.replies[msg.RECONCILE_REQ] = [
        {"position": open_position_payload(
            side=msg.TradeSide.SELL, entry=2360.0, volume_cents=10_000,
        )},
    ]
    await transport.push(spot_event(bid=2350.0, ask=2350.3))
    await asyncio.sleep(0.01)

    positions = await bridge.get_positions("XAUUSD")
    # SELL 100 oz: (2360 - 2350.3 ask) * 100 = 970.0
    assert positions[0]["profit"] == pytest.approx(970.0)
    assert positions[0]["type"] == "SELL"
    await bridge.disconnect()


async def test_get_positions_returns_empty_on_reconcile_error():
    transport = FakeTransport()
    bridge = await connect_bridge(transport)
    transport.replies[msg.RECONCILE_REQ] = [error_res("CONCURRENT_MODIFICATION")]
    assert await bridge.get_positions("XAUUSD") == []
    await bridge.disconnect()


# ----------------------------------------------------------------------
# Candles
# ----------------------------------------------------------------------

def trendbar_payload(bars: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "period": msg.TrendbarPeriod.M5,
        "timestamp": int(time.time() * 1000),
        "count": len(bars),
        "trendbar": bars,
    }


def make_bars(n: int, start_minutes: int, low: float = 2350.0) -> list[dict[str, Any]]:
    """n M5 bars with a rising close, per the trendbar delta scheme."""
    bars = []
    for i in range(n):
        low_scaled = int((low + i) * 100_000)
        bars.append({
            "volume": 100 + i,
            "period": msg.TrendbarPeriod.M5,
            "low": low_scaled,
            "deltaOpen": 10_000,
            "deltaClose": 20_000 + i * 1_000,
            "deltaHigh": 30_000,
            "utcTimestampInMinutes": start_minutes + i * 5,
        })
    return bars


async def test_get_candles_decodes_trendbar_deltas():
    transport = FakeTransport()
    bridge = await connect_bridge(transport)
    start_minutes = 3_000_000
    transport.replies[msg.GET_TRENDBARS_REQ] = [
        trendbar_payload(make_bars(3, start_minutes)),
    ]

    df = await bridge.get_candles("XAUUSD", "M5", 3)
    assert len(df) == 3
    assert list(df.columns) == ["open", "high", "low", "close", "volume"]
    assert df.index.name == "timestamp"
    first = df.iloc[0]
    # Bar 0: low 2350.0, open = low+0.1, close = low+0.2, high = low+0.3
    assert first["low"] == pytest.approx(2350.0)
    assert first["open"] == pytest.approx(2350.1)
    assert first["close"] == pytest.approx(2350.2)
    assert first["high"] == pytest.approx(2350.3)
    assert first["volume"] == pytest.approx(100)
    spacing = (df.index[1] - df.index[0]).total_seconds()
    assert spacing == pytest.approx(300)
    await bridge.disconnect()


async def test_get_candles_unknown_timeframe_returns_empty():
    transport = FakeTransport()
    bridge = await connect_bridge(transport)
    assert (await bridge.get_candles("XAUUSD", "M7", 10)).empty
    await bridge.disconnect()


async def test_get_candles_tail_keeps_requested_count():
    transport = FakeTransport()
    bridge = await connect_bridge(transport)
    transport.replies[msg.GET_TRENDBARS_REQ] = [
        trendbar_payload(make_bars(10, 3_000_000)),
    ]
    df = await bridge.get_candles("XAUUSD", "M5", 5)
    assert len(df) == 5
    # Keeps the LAST 5 bars (most recent)
    assert df.iloc[0]["low"] == pytest.approx(2355.0)
    await bridge.disconnect()


async def test_get_candles_chunks_window_over_one_week():
    """H1 x 200 bars = 30 days > 1-week cap -> two chunked requests."""
    transport = FakeTransport()
    bridge = await connect_bridge(transport)

    def h1_bars(start_minutes: int, n: int) -> list[dict[str, Any]]:
        return [{
            "volume": 50, "period": msg.TrendbarPeriod.H1,
            "low": int(2350.0 * 100_000), "deltaOpen": 0, "deltaClose": 0,
            "deltaHigh": 0, "utcTimestampInMinutes": start_minutes + i * 60,
        } for i in range(n)]

    transport.replies[msg.GET_TRENDBARS_REQ] = [
        {"period": msg.TrendbarPeriod.H1, "trendbar": h1_bars(3_000_000, 100)},
        {"period": msg.TrendbarPeriod.H1, "trendbar": h1_bars(3_000_000 - 100 * 60, 100)},
    ]
    df = await bridge.get_candles("XAUUSD", "H1", 200)
    assert len(df) == 200
    assert len(transport.sent_of(msg.GET_TRENDBARS_REQ)) == 2
    # The second chunk's window is contiguous with the first one
    first_req, second_req = transport.sent_of(msg.GET_TRENDBARS_REQ)
    assert second_req.payload["toTimestamp"] == first_req.payload["fromTimestamp"]
    assert second_req.payload["fromTimestamp"] < first_req.payload["fromTimestamp"]
    await bridge.disconnect()


# ----------------------------------------------------------------------
# Order lifecycle
# ----------------------------------------------------------------------

async def test_send_order_market_buy_with_relative_sltp():
    transport = FakeTransport()
    bridge = await connect_bridge(transport)
    await transport.push(spot_event(bid=2350.00, ask=2350.30))
    await asyncio.sleep(0.01)

    await push_execution_reply(transport, msg.NEW_ORDER_REQ, execution_fill_payload())
    result = await bridge.send_order(
        "XAUUSD", "BUY", 1.0, stop_loss=2345.0, take_profit=2360.0,
    )
    assert result.success is True
    assert result.ticket == 555002
    assert result.price == pytest.approx(2350.20)
    assert result.volume == pytest.approx(1.0)

    order = transport.sent_of(msg.NEW_ORDER_REQ)[-1]
    assert order.payload["orderType"] == msg.OrderType.MARKET
    assert order.payload["tradeSide"] == msg.TradeSide.BUY
    assert order.payload["volume"] == 10_000  # 1.0 lot x lotSize 10000 cents
    assert order.payload["ctidTraderAccountId"] == 6194964
    # BUY anchors on ask 2350.30: relSL = 2350.30-2345.0 = 5.30, relTP = 2360-2350.30 = 9.70
    assert order.payload["relativeStopLoss"] == 530_000
    assert order.payload["relativeTakeProfit"] == 970_000
    # Absolute SL/TP must NOT be sent on a MARKET order (proto rule)
    assert "stopLoss" not in order.payload
    assert "takeProfit" not in order.payload
    await bridge.disconnect()


async def test_send_order_market_sell_anchors_on_bid():
    transport = FakeTransport()
    bridge = await connect_bridge(transport)
    await transport.push(spot_event(bid=2350.00, ask=2350.30))
    await asyncio.sleep(0.01)

    await push_execution_reply(
        transport, msg.NEW_ORDER_REQ,
        execution_fill_payload(filled_cents=5_000),
    )
    result = await bridge.send_order("XAUUSD", "SELL", 0.5, stop_loss=2355.0)
    assert result.success is True
    assert result.volume == pytest.approx(0.5)

    order = transport.sent_of(msg.NEW_ORDER_REQ)[-1]
    assert order.payload["tradeSide"] == msg.TradeSide.SELL
    assert order.payload["volume"] == 5_000
    # SELL anchors on bid 2350.0: relSL = 2355 - 2350 = 5.0
    assert order.payload["relativeStopLoss"] == 500_000
    await bridge.disconnect()


async def test_send_order_without_sltp_sends_no_relative_fields():
    transport = FakeTransport()
    bridge = await connect_bridge(transport)
    await transport.push(spot_event())
    await asyncio.sleep(0.01)
    await push_execution_reply(transport, msg.NEW_ORDER_REQ, execution_fill_payload())

    result = await bridge.send_order("XAUUSD", "BUY", 0.01)
    assert result.success is True
    order = transport.sent_of(msg.NEW_ORDER_REQ)[-1]
    assert order.payload["volume"] == 100  # 0.01 lot gold
    assert "relativeStopLoss" not in order.payload
    assert "relativeTakeProfit" not in order.payload
    await bridge.disconnect()


async def test_send_order_rejected_by_order_error_event():
    transport = FakeTransport()
    bridge = await connect_bridge(transport)
    await transport.push(spot_event())
    await asyncio.sleep(0.01)

    original_send = transport.send

    async def push_reject(data: bytes) -> None:
        await original_send(data)
        last = transport.sent_messages[-1]
        if last.payload_type == msg.NEW_ORDER_REQ:
            await transport.push(ApiMessage(
                payload_type=msg.ORDER_ERROR_EVENT,
                payload={
                    "ctidTraderAccountId": 6194964,
                    "errorCode": "TRADE_BAD_VOLUME",
                    "description": "volume below minimum",
                },
                client_msg_id=last.client_msg_id,
            ))

    transport.send = push_reject  # type: ignore[method-assign]
    result = await bridge.send_order("XAUUSD", "BUY", 0.001)
    assert result.success is False
    assert "TRADE_BAD_VOLUME" in (result.error or "")
    await bridge.disconnect()


async def test_send_order_rejected_by_execution_event():
    transport = FakeTransport()
    bridge = await connect_bridge(transport)
    await transport.push(spot_event())
    await asyncio.sleep(0.01)

    await push_execution_reply(
        transport, msg.NEW_ORDER_REQ,
        execution_fill_payload(execution_type=msg.ExecutionType.ORDER_REJECTED),
    )
    result = await bridge.send_order("XAUUSD", "BUY", 1.0)
    assert result.success is False
    await bridge.disconnect()


async def test_send_order_fails_without_spot_quote():
    transport = FakeTransport()
    bridge = await connect_bridge(transport)
    result = await bridge.send_order("XAUUSD", "BUY", 1.0)
    assert result.success is False
    assert "no spot quote" in (result.error or "")
    await bridge.disconnect()


async def test_send_order_fails_on_unknown_symbol():
    transport = FakeTransport()
    bridge = await connect_bridge(transport)
    result = await bridge.send_order("BTCUSD", "BUY", 1.0)
    assert result.success is False
    assert "not found" in (result.error or "")
    await bridge.disconnect()


async def test_send_order_timeout_returns_failure():
    transport = FakeTransport()
    transport.no_reply.add(msg.NEW_ORDER_REQ)
    bridge = await connect_bridge(transport, make_config(request_timeout=0.05))
    await transport.push(spot_event())
    await asyncio.sleep(0.01)
    result = await bridge.send_order("XAUUSD", "BUY", 1.0)  # server never answers
    assert result.success is False
    assert "timeout" in (result.error or "").lower()
    await bridge.disconnect()


async def test_send_order_invalid_direction():
    transport = FakeTransport()
    bridge = await connect_bridge(transport)
    await transport.push(spot_event())
    await asyncio.sleep(0.01)
    result = await bridge.send_order("XAUUSD", "HOLD", 1.0)
    assert result.success is False
    assert "direction" in (result.error or "").lower()
    await bridge.disconnect()


async def test_send_order_invalid_volume():
    transport = FakeTransport()
    bridge = await connect_bridge(transport)
    await transport.push(spot_event())
    await asyncio.sleep(0.01)
    result = await bridge.send_order("XAUUSD", "BUY", 0.0)
    assert result.success is False
    assert "volume" in (result.error or "").lower()
    await bridge.disconnect()


async def test_execution_event_without_client_msg_id_resolves_fifo():
    """Server pushes the fill without echoing clientMsgId (event semantics)."""
    transport = FakeTransport()
    bridge = await connect_bridge(transport)
    await transport.push(spot_event())
    await asyncio.sleep(0.01)

    original_send = transport.send

    async def push_anonymous_fill(data: bytes) -> None:
        await original_send(data)
        last = transport.sent_messages[-1]
        if last.payload_type == msg.NEW_ORDER_REQ:
            await transport.push(ApiMessage(
                payload_type=msg.EXECUTION_EVENT,
                payload=execution_fill_payload(),  # no clientMsgId echoed
            ))

    transport.send = push_anonymous_fill  # type: ignore[method-assign]
    result = await bridge.send_order("XAUUSD", "BUY", 1.0)
    assert result.success is True
    assert result.ticket == 555002
    await bridge.disconnect()


# ----------------------------------------------------------------------
# Close position
# ----------------------------------------------------------------------

async def test_close_position_sends_close_req_and_fills():
    transport = FakeTransport()
    bridge = await connect_bridge(transport)
    transport.replies[msg.RECONCILE_REQ] = [
        {"position": open_position_payload(position_id=555001, volume_cents=10_000)},
    ]
    await push_execution_reply(transport, msg.CLOSE_POSITION_REQ, execution_fill_payload(position_id=555001))
    assert await bridge.close_position(555001) is True

    close_reqs = transport.sent_of(msg.CLOSE_POSITION_REQ)
    assert len(close_reqs) == 1
    assert close_reqs[0].payload == {
        "ctidTraderAccountId": 6194964, "positionId": 555001, "volume": 10_000,
    }
    await bridge.disconnect()


async def test_close_position_with_fill_returns_execution_price():
    transport = FakeTransport()
    bridge = await connect_bridge(transport)
    transport.replies[msg.RECONCILE_REQ] = [
        {"position": open_position_payload(position_id=555001)},
    ]
    await push_execution_reply(
        transport, msg.CLOSE_POSITION_REQ,
        execution_fill_payload(position_id=555001, fill_price=2351.77),
    )
    ok, fill = await bridge.close_position_with_fill(555001)
    assert ok is True
    assert fill == pytest.approx(2351.77)
    await bridge.disconnect()


async def test_close_position_missing_position_returns_false():
    transport = FakeTransport()
    bridge = await connect_bridge(transport)
    transport.replies[msg.RECONCILE_REQ] = [{"position": []}]
    assert await bridge.close_position(424242) is False
    await bridge.disconnect()


async def test_close_position_rejected_returns_false():
    transport = FakeTransport()
    bridge = await connect_bridge(transport)
    transport.replies[msg.RECONCILE_REQ] = [
        {"position": open_position_payload(position_id=555001)},
    ]
    await push_execution_reply(
        transport, msg.CLOSE_POSITION_REQ,
        execution_fill_payload(execution_type=msg.ExecutionType.ORDER_REJECTED),
    )
    assert await bridge.close_position(555001) is False
    await bridge.disconnect()


async def test_close_position_timeout_returns_false():
    transport = FakeTransport()
    transport.no_reply.add(msg.CLOSE_POSITION_REQ)
    bridge = await connect_bridge(transport, make_config(request_timeout=0.05))
    transport.replies[msg.RECONCILE_REQ] = [
        {"position": open_position_payload(position_id=555001)},
    ]
    assert await bridge.close_position(555001) is False
    await bridge.disconnect()


# ----------------------------------------------------------------------
# Amend SL/TP (cTrader-native extra)
# ----------------------------------------------------------------------

async def test_amend_position_sltp_sends_absolute_prices():
    transport = FakeTransport()
    bridge = await connect_bridge(transport)
    await push_execution_reply(
        transport, msg.AMEND_POSITION_SLTP_REQ,
        execution_fill_payload(execution_type=msg.ExecutionType.ORDER_FILLED),
    )
    assert await bridge.amend_position_sltp(555001, stop_loss=2348.0, take_profit=2362.0) is True
    amend = transport.sent_of(msg.AMEND_POSITION_SLTP_REQ)[-1]
    assert amend.payload["positionId"] == 555001
    assert amend.payload["stopLoss"] == pytest.approx(2348.0)
    assert amend.payload["takeProfit"] == pytest.approx(2362.0)
    await bridge.disconnect()


async def test_amend_position_sltp_error_returns_false():
    transport = FakeTransport()
    bridge = await connect_bridge(transport)
    original_send = transport.send

    async def push_error(data: bytes) -> None:
        await original_send(data)
        last = transport.sent_messages[-1]
        if last.payload_type == msg.AMEND_POSITION_SLTP_REQ:
            await transport.push(ApiMessage(
                payload_type=msg.ORDER_ERROR_EVENT,
                payload={"errorCode": "TRADE_ENTITY_NOT_FOUND", "description": "gone"},
                client_msg_id=last.client_msg_id,
            ))

    transport.send = push_error  # type: ignore[method-assign]
    assert await bridge.amend_position_sltp(555001, stop_loss=2348.0) is False
    await bridge.disconnect()


# ----------------------------------------------------------------------
# Heartbeat
# ----------------------------------------------------------------------

async def test_heartbeat_sent_when_idle():
    transport = FakeTransport()
    bridge = await connect_bridge(transport)  # heartbeat_interval=0.05
    await asyncio.sleep(0.2)
    heartbeats = transport.sent_of(msg.HEARTBEAT_EVENT)
    assert len(heartbeats) >= 1
    assert heartbeats[0].payload == {}
    await bridge.disconnect()


async def test_server_heartbeat_is_echoed_immediately():
    transport = FakeTransport()
    bridge = await connect_bridge(transport)
    before = len(transport.sent_of(msg.HEARTBEAT_EVENT))
    await transport.push(ApiMessage(payload_type=msg.HEARTBEAT_EVENT, payload={}))
    await asyncio.sleep(0.01)
    after = len(transport.sent_of(msg.HEARTBEAT_EVENT))
    assert after == before + 1  # echo, not waiting for the idle timer
    await bridge.disconnect()


async def test_heartbeat_not_sent_while_traffic_flows():
    """Continuous traffic suppresses the idle-timer heartbeat entirely."""
    transport = FakeTransport()
    config = make_config(heartbeat_interval=0.1)
    bridge = await connect_bridge(transport, config)
    for _ in range(6):  # ~0.18s of requests — idle never reaches 0.1s
        assert await bridge.health_check() is True
        await asyncio.sleep(0.03)
    assert transport.sent_of(msg.HEARTBEAT_EVENT) == []
    await bridge.disconnect()


# ----------------------------------------------------------------------
# Health check, reconnect, connection loss
# ----------------------------------------------------------------------

async def test_health_check_round_trips_version_req():
    transport = FakeTransport()
    bridge = await connect_bridge(transport)
    assert await bridge.health_check() is True
    assert len(transport.sent_of(msg.VERSION_REQ)) == 1
    await bridge.disconnect()


async def test_health_check_false_on_error_response():
    transport = FakeTransport()
    bridge = await connect_bridge(transport)
    transport.replies[msg.VERSION_REQ] = [error_res("TIMEOUT_ERROR")]
    assert await bridge.health_check() is False
    await bridge.disconnect()


async def test_connect_after_disconnect_reconnects_with_full_auth():
    transport = FakeTransport()
    bridge = await connect_bridge(transport)
    await bridge.disconnect()
    assert transport.connected is False
    assert await bridge.connect() is True  # full auth sequence again
    assert len(transport.sent_of(msg.APPLICATION_AUTH_REQ)) >= 2
    assert len(transport.sent_of(msg.ACCOUNT_AUTH_REQ)) >= 2
    await bridge.disconnect()


async def test_connection_lost_fails_pending_and_marks_disconnected():
    transport = FakeTransport()
    bridge = await connect_bridge(transport)
    assert bridge._connected is True

    # A request in flight with no scripted answer
    transport.no_reply.add(msg.VERSION_REQ)
    request = bridge._build_request(msg.VERSION_REQ, {})
    future = asyncio.get_running_loop().create_future()
    bridge._pending[request.client_msg_id] = future
    await bridge._send(request)

    await transport.drop_connection()
    await asyncio.sleep(0.05)
    assert bridge._connected is False
    assert future.done()
    assert isinstance(future.exception(), ConnectionError)
    await bridge.disconnect()


async def test_disconnect_cancels_tasks_and_clears_pending():
    transport = FakeTransport()
    bridge = await connect_bridge(transport)
    assert bridge._heartbeat_task is not None
    assert bridge._receive_task is not None
    await bridge.disconnect()
    assert bridge._heartbeat_task is None
    assert bridge._receive_task is None
    assert bridge._pending == {}
    assert bridge._order_waits == {}
    assert bridge._order_queue == []


async def test_pending_request_fails_on_error_response_without_raising():
    transport = FakeTransport()
    bridge = await connect_bridge(transport)
    transport.replies[msg.TRADER_REQ] = [
        error_res("CH_ACCOUNT_NOT_AUTHORIZED", "re-auth"),
    ]
    info = await bridge.get_account_info()  # parity: error -> None, never raise
    assert info is None
    await bridge.disconnect()


# ----------------------------------------------------------------------
# Account discovery (connection-day helper)
# ----------------------------------------------------------------------

async def test_list_accounts_by_token():
    transport = FakeTransport()
    bridge = await connect_bridge(transport)
    transport.replies[msg.GET_ACCOUNTS_BY_ACCESS_TOKEN_REQ] = [{
        "accessToken": "test-access-token",
        "ctidTraderAccount": [
            {"ctidTraderAccountId": 6194964, "isLive": True,
             "traderLogin": 6194964, "brokerTitleShort": "IC Markets"},
            {"ctidTraderAccountId": 123, "isLive": False,
             "traderLogin": 12345, "brokerTitleShort": "IC Markets"},
        ],
    }]
    accounts = await bridge.list_accounts_by_token()
    assert accounts[0]["ctidTraderAccountId"] == 6194964
    assert accounts[0]["isLive"] is True
    assert accounts[1]["isLive"] is False
    await bridge.disconnect()


# ----------------------------------------------------------------------
# Wire format: frames the bridge produces are valid JSON envelopes
# ----------------------------------------------------------------------

async def test_frames_are_newline_terminated_json_envelopes():
    transport = FakeTransport()
    bridge = await connect_bridge(transport)

    raw: list[bytes] = []
    original_send = transport.send

    async def capture(data: bytes) -> None:
        raw.append(data)
        await original_send(data)

    transport.send = capture  # type: ignore[method-assign]
    assert await bridge.health_check() is True

    assert raw, "health_check must have sent at least one frame"
    frame = raw[-1]
    assert frame.endswith(b"\n")
    envelope = json.loads(frame)
    assert set(envelope.keys()) == {"clientMsgId", "payloadType", "payload"}
    assert envelope["payloadType"] == msg.VERSION_REQ
    assert isinstance(envelope["clientMsgId"], str) and envelope["clientMsgId"]
    await bridge.disconnect()


def test_price_scaling_documented_semantics():
    # 1/100000 unit per the proto comment: 123000 -> 1.23
    assert price_to_real(123_000) == pytest.approx(1.23)
    assert price_to_real(53_423_782) == pytest.approx(534.23782)


# ----------------------------------------------------------------------
# Causal tests — cross-context review findings 17/11 (reproduced, then fixed)
# ----------------------------------------------------------------------

async def test_send_order_accepted_then_filled_is_success():
    """REVIEW-CRITICAL: ACCEPTED must not end the wait — only FILLED does.

    Proto: ExecutionEvent follows "acceptance OR execution"; ORDER_ACCEPTED=2
    legitimately precedes ORDER_FILLED=3 for MARKET orders. Resolving on the
    first event reported filled orders as failures with the position open.
    """
    transport = FakeTransport()
    bridge = await connect_bridge(transport)
    await transport.push(spot_event())
    await asyncio.sleep(0.01)

    original_send = transport.send

    async def push_accepted_then_filled(data: bytes) -> None:
        await original_send(data)
        last = transport.sent_messages[-1]
        if last.payload_type == msg.NEW_ORDER_REQ:
            await transport.push(ApiMessage(
                payload_type=msg.EXECUTION_EVENT,
                payload=execution_fill_payload(
                    execution_type=msg.ExecutionType.ORDER_ACCEPTED,
                ),
                client_msg_id=last.client_msg_id,
            ))
            await transport.push(ApiMessage(
                payload_type=msg.EXECUTION_EVENT,
                payload=execution_fill_payload(),
                client_msg_id=last.client_msg_id,
            ))

    transport.send = push_accepted_then_filled  # type: ignore[method-assign]
    result = await bridge.send_order("XAUUSD", "BUY", 1.0)
    assert result.success is True, f"order was accepted+filled: {result.error}"
    assert result.ticket == 555002
    await bridge.disconnect()


async def test_close_position_not_stolen_by_swap_server_event():
    """REVIEW-HIGH: SWAP/server events must not resolve pending requests.

    ProtoOAExecutionEvent also carries SWAP(9)/DEPOSIT_WITHDRAW(10) and
    isServerEvent marks server-side logic (stop-outs). The FIFO fallback used
    to pop the pending close for any event without clientMsgId.
    """
    transport = FakeTransport()
    bridge = await connect_bridge(transport)
    transport.replies[msg.RECONCILE_REQ] = [
        {"position": open_position_payload(position_id=555001)},
    ]
    await transport.push(spot_event())
    await asyncio.sleep(0.01)

    original_send = transport.send

    async def push_swap_then_fill(data: bytes) -> None:
        await original_send(data)
        last = transport.sent_messages[-1]
        if last.payload_type == msg.CLOSE_POSITION_REQ:
            # Daily rollover SWAP push arrives mid-close, no clientMsgId
            await transport.push(ApiMessage(
                payload_type=msg.EXECUTION_EVENT,
                payload={
                    "ctidTraderAccountId": 6194964,
                    "executionType": msg.ExecutionType.SWAP,
                    "isServerEvent": True,
                },
            ))
            # Then the real fill for OUR close
            await transport.push(ApiMessage(
                payload_type=msg.EXECUTION_EVENT,
                payload=execution_fill_payload(position_id=555001),
                client_msg_id=last.client_msg_id,
            ))

    transport.send = push_swap_then_fill  # type: ignore[method-assign]
    ok, fill = await bridge.close_position_with_fill(555001)
    assert ok is True, "the close actually filled after the SWAP event"
    assert fill == pytest.approx(2350.20)
    await bridge.disconnect()


async def test_close_position_rejects_other_position_server_stopout():
    """REVIEW-HIGH variant: an anonymous terminal event for a DIFFERENT
    position must not resolve our close (target mismatch check)."""
    transport = FakeTransport()
    bridge = await connect_bridge(transport)
    transport.replies[msg.RECONCILE_REQ] = [
        {"position": open_position_payload(position_id=555001)},
    ]
    await transport.push(spot_event())
    await asyncio.sleep(0.01)

    original_send = transport.send

    async def push_foreign_stopout_then_fill(data: bytes) -> None:
        await original_send(data)
        last = transport.sent_messages[-1]
        if last.payload_type == msg.CLOSE_POSITION_REQ:
            # Server stop-out of ANOTHER position, terminal type, no msg id —
            # a DIFFERENT fill price so a stolen future is observable
            await transport.push(ApiMessage(
                payload_type=msg.EXECUTION_EVENT,
                payload=execution_fill_payload(
                    position_id=999888,
                    execution_type=msg.ExecutionType.ORDER_FILLED,
                    fill_price=9999.99,
                ),
            ))
            await transport.push(ApiMessage(
                payload_type=msg.EXECUTION_EVENT,
                payload=execution_fill_payload(position_id=555001),
                client_msg_id=last.client_msg_id,
            ))

    transport.send = push_foreign_stopout_then_fill  # type: ignore[method-assign]
    ok, fill = await bridge.close_position_with_fill(555001)
    assert ok is True
    assert fill == pytest.approx(2350.20), "fill must come from OUR position's event"
    await bridge.disconnect()


async def test_stale_client_msg_id_cannot_claim_retry_waiter():
    """REVIEW-MEDIUM (round 2): an event echoing a clientMsgId whose request
    already timed out belongs to a DEAD request — it must be dropped, not
    fall through to the anonymous FIFO and steal a live waiter.

    Scenario: order #1 times out (slow server), the engine retries as
    order #2, then order #1's late fill arrives echoing order #1's id.
    Pre-fix the stale fill claimed the retry's waiter, so the retry
    reported success with ORDER #1's ticket and its own fill was dropped.
    """
    transport = FakeTransport()
    bridge = await connect_bridge(
        transport, config=make_config(request_timeout=0.15),
    )
    await transport.push(spot_event())
    await asyncio.sleep(0.01)

    original_send = transport.send
    order_ids: list[str] = []

    async def capture_order_ids(data: bytes) -> None:
        await original_send(data)
        last = transport.sent_messages[-1]
        if last.payload_type == msg.NEW_ORDER_REQ and last.client_msg_id:
            order_ids.append(last.client_msg_id)

    transport.send = capture_order_ids  # type: ignore[method-assign]
    first = await bridge.send_order("XAUUSD", "BUY", 1.0)  # no reply scripted
    assert first.success is False, "order #1 must time out first"
    assert len(order_ids) == 1

    async def push_stale_fill_then_own_fill(data: bytes) -> None:
        await original_send(data)
        last = transport.sent_messages[-1]
        if last.payload_type == msg.NEW_ORDER_REQ:
            # Order #1's late fill arrives echoing the DEAD request id
            await transport.push(ApiMessage(
                payload_type=msg.EXECUTION_EVENT,
                payload=execution_fill_payload(position_id=111111),
                client_msg_id=order_ids[0],
            ))
            # Then order #2's own fill, echoing its live id
            await transport.push(ApiMessage(
                payload_type=msg.EXECUTION_EVENT,
                payload=execution_fill_payload(position_id=222333),
                client_msg_id=last.client_msg_id,
            ))

    transport.send = push_stale_fill_then_own_fill  # type: ignore[method-assign]
    retry = await bridge.send_order("XAUUSD", "BUY", 1.0)
    assert retry.success is True
    assert retry.ticket == 222333, (
        "retry must report ITS OWN fill (222333), not the dead request's (111111)"
    )
    await bridge.disconnect()


async def test_order_error_with_unknown_msg_id_does_not_fail_live_close():
    """REVIEW-MEDIUM (round 2): an ORDER_ERROR_EVENT echoing a clientMsgId
    with no live waiter is an error for a DEAD request — it must not claim
    any pending waiter.

    Pre-fix _resolve_order_error claimed the FIFO blindly, so a live close
    reported 'rejected' even though its own fill followed right after.
    """
    transport = FakeTransport()
    bridge = await connect_bridge(transport)
    transport.replies[msg.RECONCILE_REQ] = [
        {"position": open_position_payload(position_id=555001)},
    ]
    await transport.push(spot_event())
    await asyncio.sleep(0.01)

    original_send = transport.send

    async def push_ghost_error_then_fill(data: bytes) -> None:
        await original_send(data)
        last = transport.sent_messages[-1]
        if last.payload_type == msg.CLOSE_POSITION_REQ:
            # Error event for some dead/unknown request id
            await transport.push(ApiMessage(
                payload_type=msg.ORDER_ERROR_EVENT,
                payload={"errorCode": "TRADE_BAD_VOLUME", "description": "dead request"},
                client_msg_id="unknown-dead-request-id",
            ))
            # Our close actually fills right after
            await transport.push(ApiMessage(
                payload_type=msg.EXECUTION_EVENT,
                payload=execution_fill_payload(position_id=555001),
                client_msg_id=last.client_msg_id,
            ))

    transport.send = push_ghost_error_then_fill  # type: ignore[method-assign]
    ok, fill = await bridge.close_position_with_fill(555001)
    assert ok is True, "ghost error for a dead request must not fail the live close"
    assert fill == pytest.approx(2350.20)
    await bridge.disconnect()


async def test_get_positions_pnl_zero_before_first_spot():
    """Self-review: no spot yet -> PnL must be 0, not -entry*units."""
    transport = FakeTransport()
    bridge = await connect_bridge(transport)
    transport.replies[msg.RECONCILE_REQ] = [
        {"position": open_position_payload(entry=2345.0, volume_cents=10_000)},
    ]
    positions = await bridge.get_positions("XAUUSD")  # no spot pushed
    assert positions[0]["profit"] == pytest.approx(0.0)
    assert positions[0]["price_current"] == pytest.approx(2345.0)
    await bridge.disconnect()


async def test_get_candles_pages_hasmore_inside_window():
    """REVIEW-MEDIUM: hasMore must page backward within the window, not skip."""
    transport = FakeTransport()
    bridge = await connect_bridge(transport)

    # Bars must sit INSIDE the requested window (count=4 x M5 = last 20 min)
    now_min = int(time.time() // 60)
    def bar(minutes: int) -> dict[str, Any]:
        return {"volume": 1, "period": msg.TrendbarPeriod.M5, "low": 1, "deltaOpen": 0,
                "deltaClose": 0, "deltaHigh": 0, "utcTimestampInMinutes": minutes}

    # 2 requests for the SAME window: newest pair first (hasMore, newest-first
    # order to prove min() robustness), then the older pair
    transport.replies[msg.GET_TRENDBARS_REQ] = [
        {"period": msg.TrendbarPeriod.M5, "hasMore": True,
         "trendbar": [bar(now_min), bar(now_min - 5)]},
        {"period": msg.TrendbarPeriod.M5, "hasMore": False,
         "trendbar": [bar(now_min - 10), bar(now_min - 15)]},
    ]
    df = await bridge.get_candles("XAUUSD", "M5", 4)
    assert len(df) == 4  # all four bars, no silent gap
    requests = transport.sent_of(msg.GET_TRENDBARS_REQ)
    assert len(requests) == 2
    # Second page pages backward from the oldest received bar, same window
    assert requests[1].payload["toTimestamp"] == (now_min - 5) * 60_000
    assert requests[1].payload["fromTimestamp"] == requests[0].payload["fromTimestamp"]
    await bridge.disconnect()


async def test_tcp_transport_reconnect_resets_stale_state():
    """REVIEW-MEDIUM: reconnect must not inherit the dead session's state.

    Stale partial frame, stale end-of-stream sentinel and a still-running old
    read pump would corrupt or kill the new session.
    """
    from ctrader.transport import TcpNewlineTransport

    class SeamTransport(TcpNewlineTransport):
        """Test seam: no network — returns a live (never-EOF) reader."""

        async def _open_streams(self):
            return asyncio.StreamReader(), None  # no feed_eof -> pump stays alive

    transport = SeamTransport("host", 5036)
    # Dead session leftovers
    transport._decoder.feed(b'{"payloadType": 2101')  # torn partial frame
    await transport._queue.put(b'{"payloadType": 2101, "payload": {}}\n')  # stale frame
    await transport._queue.put(None)  # stale end-of-stream sentinel
    old_pump = asyncio.create_task(asyncio.sleep(100))
    transport._pump_task = old_pump

    await transport.connect()

    # Let the cancelled sleep task settle, then verify
    try:
        await old_pump
    except asyncio.CancelledError:
        pass
    assert old_pump.cancelled(), "old read pump must be cancelled"
    assert transport._decoder.pending() == b"", "decoder must be reset"
    # The stale sentinel must not kill the fresh messages() iterator
    collected: list[bytes] = []

    async def collect() -> None:
        async for frame in transport.messages():
            collected.append(frame)

    collector = asyncio.create_task(collect())
    await transport._queue.put(b'{"payloadType": 2101, "payload": {}}\n')
    await asyncio.sleep(0.05)
    assert len(collected) == 1, "fresh frame must arrive — stale sentinel was drained"
    collector.cancel()
    await transport.close()
