"""CTraderBridge — cTrader Open API adapter with MT5Bridge surface parity.

Same public surface as metty/bridge/client.py::MT5Bridge (connect /
disconnect / get_account_info / get_positions / get_candles / send_order /
close_position / health_check, plus close_position_with_fill) so the engine
can swap brokers without touching core code. Contract enforced by
tests/test_ctrader_contract.py.

Wire protocol (JSON mode, port 5036 — see protocol.py) verified against the
canonical .proto (ctrader/proto_reference/) and help.ctrader.com/open-api.
Auth is two-layer: ProtoOAApplicationAuthReq (app credentials) then
ProtoOAAccountAuthReq (per-account OAuth2 access token). Absolute SL/TP is
NOT supported on MARKET orders, so send_order converts SL/TP to
relativeStopLoss/relativeTakeProfit distances anchored on the current quote.
Order and close requests are answered by push ProtoOAExecutionEvent.

This module never talks to the network by itself: it drives a
CTraderTransport (transport.py), which unit tests replace with a fake.
"""

import asyncio
import logging
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional

import pandas as pd

from metty.core.models import AccountInfo, OrderResult  # read-only import

from . import messages as msg
from .config import CTraderConfig
from .endpoints import EndPoints
from .protocol import (
    ApiMessage,
    encode_frame,
    leverage_from_cents,
    lots_to_volume_cents,
    money_to_real,
    price_to_proto,
    price_to_real,
    volume_cents_to_lots,
)
from .transport import CTraderTransport, TcpNewlineTransport

logger = logging.getLogger(__name__)

# Execution types that END an order-ish request with a usable outcome.
# ORDER_ACCEPTED (2) and ORDER_PARTIAL_FILL (11) do NOT end the wait — the
# proto says ExecutionEvent follows "acceptance OR execution", so a MARKET
# order can legitimately see ACCEPTED first and the fill after (review
# finding CRITICAL 17/11: resolving on the first event reported filled
# orders as failures while the position was open).
_TERMINAL_ORDER_EVENTS = {
    msg.ExecutionType.ORDER_FILLED,
    msg.ExecutionType.ORDER_REJECTED,
    msg.ExecutionType.ORDER_EXPIRED,
    msg.ExecutionType.ORDER_CANCEL_REJECTED,
}
# Event types that never answer a trading request (account lifecycle, not
# order outcomes) — must not touch pending waits.
_UNRELATED_EVENTS = {
    msg.ExecutionType.SWAP,
    msg.ExecutionType.DEPOSIT_WITHDRAW,
    msg.ExecutionType.BONUS_DEPOSIT_WITHDRAW,
}

SYMBOL_ALIASES = {
    # IC Markets cTrader serves gold as "XAUUSD"; keep aliases for safety —
    # resolution is case-insensitive over the live symbols list anyway.
    "XAUUSD": ["XAUUSD", "XAUUSD.a", "GOLD"],
}


@dataclass
class _OrderWait:
    """One pending order/close/amend request awaiting its execution event."""

    future: asyncio.Future
    client_msg_id: str
    # positionId the request targets (close/amend); None = new order.
    target_position_id: Optional[int] = None


class CTraderApiError(Exception):
    """API-level error (ProtoOAErrorRes / ProtoOAOrderErrorEvent)."""

    def __init__(self, code: str, description: str = "") -> None:
        self.code = code
        self.description = description
        super().__init__(f"{code}: {description}" if description else code)


class _SyncLoopThread:
    """Persistent background event loop for the sync-call parity wrappers.

    One daemon thread per CTraderBridge instance; the loop outlives any
    single call, so the connection built on it (transport, receive task,
    heartbeat task) is established ONCE and reused by every subsequent
    call. This is the hot-path rule from the engine-wiring dispatch
    (20/11): connect() is app-auth + account-auth ≈ 2s per attempt, and
    an engine cycle makes several bridge calls — reconnecting per call
    (the MT5Bridge one-shot wrapper idiom, whose MT5 terminal is
    stateless and cheap to re-open) would spend ~10s per cycle just
    authenticating.

    Deliberately NOT started in __init__: a bridge used purely from async
    code (paper feed fetcher) never pays for a thread it does not use.
    """

    def __init__(self) -> None:
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()

    def ensure_started(self) -> None:
        """Idempotent start of the loop thread."""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._loop = asyncio.new_event_loop()

            def _run_loop() -> None:
                asyncio.set_event_loop(self._loop)
                self._loop.run_forever()

            self._thread = threading.Thread(
                target=_run_loop, name="ctrader-sync-loop", daemon=True,
            )
            self._thread.start()

    @property
    def loop(self) -> Optional[asyncio.AbstractEventLoop]:
        return self._loop

    def is_current(self) -> bool:
        """True when called FROM the persistent loop (deadlock hazard)."""
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            return False
        return self._loop is not None and running is self._loop

    def submit(self, coro: Any, timeout: Optional[float]) -> Any:
        """Run one coroutine on the persistent loop and wait for it.

        Exceptions propagate to the caller exactly as a direct await would;
        the caller decides the failure shape (None / empty DataFrame).
        """
        assert self._loop is not None, "submit before ensure_started()"
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        try:
            return future.result(timeout)
        except BaseException:
            future.cancel()  # best effort — the coroutine may already be done
            raise


class CTraderBridge:
    """One cTrader account connection via the Open API (JSON mode)."""

    def __init__(
        self,
        config: CTraderConfig,
        transport: Optional[CTraderTransport] = None,
    ) -> None:
        self.config = config
        self.host = EndPoints.host_for(config.host_type)
        self.port = EndPoints.port_for("json")
        self._transport = transport if transport is not None else TcpNewlineTransport(
            self.host, self.port,
        )

        self._connected = False
        self._receive_task: Optional[asyncio.Task] = None
        self._heartbeat_task: Optional[asyncio.Task] = None
        self._last_sent: float = 0.0
        # Last error that made connect() return False (auth failures carry
        # the API errorCode, e.g. CH_ACCESS_TOKEN_EXPIRED) — read-only
        # metadata so callers can fail LOUD with the real reason instead of
        # retrying a dead token blind. Additive: never set on success.
        self.last_connect_error: Optional[BaseException] = None

        # clientMsgId -> Future[ApiMessage] for req/res messages
        self._pending: dict[str, asyncio.Future] = {}
        # Order-ish requests (new/close/amend): the server answers with a
        # push ProtoOAExecutionEvent which may NOT echo clientMsgId, so we
        # track waits both by id and in a FIFO — but claiming is gated on
        # event type / server flag / target position (see _resolve_execution).
        self._order_waits: dict[str, _OrderWait] = {}
        self._order_queue: list[_OrderWait] = []

        # Cache from connect(): symbol name/id, lot sizes, assets, spots
        self._symbol_ids: dict[str, int] = {}
        self._symbol_names: dict[int, str] = {}
        self._lot_sizes: dict[int, float] = {}
        self._assets: dict[int, str] = {}
        self._spot: dict[int, dict[str, float]] = {}
        self._subscribed: set[int] = set()
        self._symbol: Optional[str] = None  # resolved default symbol name
        # Persistent-loop facility for the sync parity wrappers (20/11
        # engine wiring): started lazily by ensure_connected_sync().
        self._sync_loop = _SyncLoopThread()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def connect(self) -> bool:
        """Connect, authorize (app then account), resolve symbols, subscribe."""
        if self._connected:
            return True
        self.last_connect_error = None  # fresh attempt — stale errors must not linger
        if not self.config.is_configured:
            logger.warning(
                "CTrader bridge disabled for %s — missing credentials (env-only pattern)",
                self.config.name,
            )
            return False
        try:
            await self._transport.connect()
        except Exception as exc:  # noqa: BLE001 — connect must return False, never raise
            logger.error("cTrader connect to %s:%s failed: %s", self.host, self.port, exc)
            return False

        self._receive_task = asyncio.create_task(self._receive_loop())
        try:
            await self._authorize_application()
            await self._authorize_account()
            await self._load_symbols()
            await self._load_assets()
            await self.subscribe_spots(self.config.symbol)
        except Exception as exc:  # noqa: BLE001 — one failed step fails the connect
            logger.error("cTrader connect aborted for %s: %s", self.config.name, exc)
            self.last_connect_error = exc
            await self.disconnect()
            return False

        self._connected = True
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
        logger.info(
            "Connected to cTrader %s:%s (%s, account %d, symbol %s)",
            self.host, self.port, self.config.host_type,
            self.config.account_id, self.symbol,
        )
        return True

    async def disconnect(self) -> None:
        """Tear down tasks, transport and pending futures (idempotent)."""
        self._connected = False
        for task in (self._heartbeat_task, self._receive_task):
            if task is not None and not task.done():
                task.cancel()
        self._heartbeat_task = None
        self._receive_task = None
        self._fail_pending(ConnectionError("disconnecting"))
        try:
            await self._transport.close()
        except Exception:  # noqa: BLE001 — best-effort teardown
            pass
        logger.info("Disconnected from cTrader %s:%s", self.host, self.port)

    async def _authorize_application(self) -> None:
        """Layer 1: ProtoOAApplicationAuthReq with app credentials."""
        request = self._build_request(msg.APPLICATION_AUTH_REQ, {
            "clientId": self.config.client_id,
            "clientSecret": self.config.client_secret,
        })
        await self._await_response(request, msg.APPLICATION_AUTH_RES)
        logger.debug("cTrader application authorized for %s", self.config.name)

    async def _authorize_account(self) -> None:
        """Layer 2: ProtoOAAccountAuthReq for this ctidTraderAccountId."""
        request = self._build_request(msg.ACCOUNT_AUTH_REQ, {
            "ctidTraderAccountId": self.config.account_id,
            "accessToken": self.config.access_token,
        })
        await self._await_response(request, msg.ACCOUNT_AUTH_RES)
        logger.debug("cTrader account %d authorized", self.config.account_id)

    async def _load_symbols(self) -> None:
        """ProtoOASymbolsListReq — build name<->symbolId map, resolve default."""
        request = self._build_request(msg.SYMBOLS_LIST_REQ, {
            "ctidTraderAccountId": self.config.account_id,
            "includeArchivedSymbols": False,
        })
        response = await self._await_response(request, msg.SYMBOLS_LIST_RES)
        self._symbol_ids.clear()
        self._symbol_names.clear()
        for entry in response.payload.get("symbol", []):
            symbol_id = int(entry.get("symbolId", 0))
            name = entry.get("symbolName") or ""
            if symbol_id and name:
                self._symbol_ids[name.lower()] = symbol_id
                self._symbol_names[symbol_id] = name
        self._symbol = self._resolve_symbol_name(self.config.symbol)
        if self._symbol is None:
            raise CTraderApiError("SYMBOL_NOT_FOUND", f"symbol {self.config.symbol} not listed")
        await self._load_lot_size(self._symbol_ids[self._symbol.lower()])

    async def _load_lot_size(self, symbol_id: int) -> None:
        """ProtoOASymbolByIdReq — lotSize drives the volume<->lots mapping.

        Fail-loud (P22 wiring 20/11): volume_cents = lots x lotSize, so a
        guessed lotSize ships silently mis-scaled orders — refuse instead.
        connect() aborts with this as last_connect_error.
        """
        request = self._build_request(msg.SYMBOL_BY_ID_REQ, {
            "ctidTraderAccountId": self.config.account_id,
            "symbolId": [symbol_id],
        })
        response = await self._await_response(request, msg.SYMBOL_BY_ID_RES)
        for entry in response.payload.get("symbol", []):
            if int(entry.get("symbolId", 0)) == symbol_id:
                lot_size = entry.get("lotSize")
                if not lot_size:
                    raise CTraderApiError(
                        "LOT_SIZE_MISSING",
                        f"SymbolByIdRes for symbolId {symbol_id} carried no lotSize",
                    )
                self._lot_sizes[symbol_id] = float(lot_size)
                return
        raise CTraderApiError(
            "LOT_SIZE_NOT_FOUND",
            f"SymbolByIdRes did not include symbolId {symbol_id}",
        )

    async def _load_assets(self) -> None:
        """ProtoOAAssetListReq — depositAssetId -> currency name map."""
        request = self._build_request(msg.ASSET_LIST_REQ, {
            "ctidTraderAccountId": self.config.account_id,
        })
        response = await self._await_response(request, msg.ASSET_LIST_RES)
        self._assets = {
            int(entry.get("assetId", 0)): entry.get("name") or ""
            for entry in response.payload.get("asset", [])
            if entry.get("assetId")
        }

    # ------------------------------------------------------------------
    # Wire plumbing
    # ------------------------------------------------------------------

    def _build_request(self, payload_type: int, payload: dict[str, Any]) -> ApiMessage:
        return ApiMessage(payload_type=payload_type, payload=payload, client_msg_id=uuid.uuid4().hex)

    async def _send(self, message: ApiMessage) -> None:
        """Serialize and send one message, refreshing the heartbeat clock."""
        await self._transport.send(encode_frame(message))
        self._last_sent = time.monotonic()

    async def _await_response(
        self, request: ApiMessage, expected_type: int | None = None,
    ) -> ApiMessage:
        """Send a req/res message and await the matching response envelope."""
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        assert request.client_msg_id is not None
        self._pending[request.client_msg_id] = future
        try:
            await self._send(request)
            response = await asyncio.wait_for(future, timeout=self.config.request_timeout)
        finally:
            self._pending.pop(request.client_msg_id, None)
        if expected_type is not None and response.payload_type not in (
            expected_type, msg.OA_ERROR_RES, msg.COMMON_ERROR_RES,
        ):
            raise CTraderApiError(
                "UNEXPECTED_RESPONSE", f"expected {expected_type}, got {response.payload_type}",
            )
        return response

    async def _await_execution(
        self, request: ApiMessage, target_position_id: Optional[int] = None,
    ) -> ApiMessage:
        """Send an order-ish request; the answer is a push ProtoOAExecutionEvent.

        Only a TERMINAL event that matches this request resolves the wait
        (see _resolve_execution): ORDER_ACCEPTED / ORDER_PARTIAL_FILL keep
        the wait alive until the fill, and server-side events (SWAP, stop-out
        of another position) never claim a waiter.
        """
        assert request.client_msg_id is not None
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        wait = _OrderWait(
            future=future,
            client_msg_id=request.client_msg_id,
            target_position_id=target_position_id,
        )
        self._order_waits[request.client_msg_id] = wait
        self._order_queue.append(wait)
        try:
            await self._send(request)
            return await asyncio.wait_for(future, timeout=self.config.request_timeout)
        finally:
            self._order_waits.pop(request.client_msg_id, None)
            if wait in self._order_queue:
                self._order_queue.remove(wait)
            if not future.done():
                future.cancel()

    def _claim_wait(
        self, message: ApiMessage, predicate: Any = None,
    ) -> Optional[_OrderWait]:
        """Claim a pending wait for an incoming event.

        clientMsgId match wins (the server echoed our request id). Without
        one, the FIFO is scanned for the FIRST waiter satisfying predicate —
        waiters that don't match are left pending for their own event.

        An event echoing a clientMsgId with NO live waiter belongs to a dead
        (timed-out/finished) request: dropping it can never starve a live
        waiter, while falling through to the FIFO would let a stale fill or
        ghost error claim the retry's waiter.
        """
        if message.client_msg_id:
            wait = self._order_waits.get(message.client_msg_id)
            if wait is not None and not wait.future.done():
                self._order_waits.pop(wait.client_msg_id, None)
                self._order_queue.remove(wait)
                return wait
            return None  # echoed id of a dead request — drop, never re-route
        for wait in self._order_queue:
            if not wait.future.done() and (predicate is None or predicate(wait)):
                self._order_waits.pop(wait.client_msg_id, None)
                self._order_queue.remove(wait)
                return wait
        return None

    def _fail_pending(self, reason: Exception) -> None:
        """Fail every in-flight future (called on connection loss/disconnect)."""
        futures = list(self._pending.values()) + [
            wait.future for wait in self._order_queue
        ]
        for future in futures:
            if not future.done():
                future.set_exception(reason)
        self._pending.clear()
        self._order_waits.clear()
        self._order_queue.clear()

    async def _receive_loop(self) -> None:
        """Dispatch incoming frames: responses, events, heartbeats."""
        try:
            async for frame in self._transport.messages():
                try:
                    await self._handle_frame(frame)
                except Exception as exc:  # noqa: BLE001 — one bad frame must not kill the loop
                    logger.error("cTrader frame handling error: %s", exc)
        except asyncio.CancelledError:
            pass
        except Exception as exc:  # noqa: BLE001 — connection lost
            logger.warning("cTrader receive loop ended: %s", exc)
        finally:
            if self._connected:
                logger.error("cTrader connection lost for %s", self.config.name)
                self._connected = False
                self._fail_pending(ConnectionError("connection lost"))

    async def _handle_frame(self, frame: bytes) -> None:
        """Route one decoded envelope to its handler."""
        from .protocol import decode_message  # local import keeps module import light
        message = decode_message(frame)

        if message.payload_type == msg.HEARTBEAT_EVENT:
            # Server heartbeat -> echo immediately (SDK TcpProtocol behavior)
            await self._send(ApiMessage(payload_type=msg.HEARTBEAT_EVENT))
            return
        if message.payload_type == msg.EXECUTION_EVENT:
            self._resolve_execution(message)
            return
        if message.payload_type == msg.ORDER_ERROR_EVENT:
            self._resolve_order_error(message)
            return
        if message.payload_type == msg.SPOT_EVENT:
            self._cache_spot(message.payload)
            return
        if message.payload_type == msg.CLIENT_DISCONNECT_EVENT:
            logger.warning("cTrader server is disconnecting %s", self.config.name)
            return
        if message.client_msg_id is not None and message.client_msg_id in self._pending:
            future = self._pending.pop(message.client_msg_id)
            if message.payload_type in (msg.OA_ERROR_RES, msg.COMMON_ERROR_RES):
                future.set_exception(CTraderApiError(
                    str(message.payload.get("errorCode", "UNKNOWN_ERROR")),
                    str(message.payload.get("description", "")),
                ))
            else:
                future.set_result(message)
            return
        logger.debug("cTrader unhandled message payloadType=%s", message.payload_type)

    def _resolve_execution(self, message: ApiMessage) -> None:
        """Match a push ProtoOAExecutionEvent to its awaiting request.

        Gate order matters (review findings CRITICAL/HIGH 17/11):
          1. server-generated events (stop-out etc.) never answer requests;
          2. unrelated lifecycle types (SWAP, deposit/withdraw) never do;
          3. non-terminal types (ACCEPTED, PARTIAL_FILL) do NOT end the wait
             — the fill event still follows;
          4. anonymous events (no clientMsgId) may only claim a waiter whose
             target position matches (or any waiter for new orders).
        """
        payload = message.payload
        if payload.get("isServerEvent"):
            logger.debug("cTrader server-side execution event ignored: %s", payload)
            return
        execution_type = payload.get("executionType")
        if execution_type in _UNRELATED_EVENTS:
            logger.debug("cTrader unrelated execution event ignored: %s", execution_type)
            return
        if execution_type not in _TERMINAL_ORDER_EVENTS:
            # ACCEPTED / PARTIAL_FILL / REPLACED — wait for the terminal one
            return

        event_position = (payload.get("deal") or {}).get("positionId") \
            or (payload.get("position") or {}).get("positionId")

        def _matches(wait: _OrderWait) -> bool:
            if wait.target_position_id is None:
                return True  # new order: terminal non-server event is ours
            if event_position is None:
                return True  # cannot disambiguate — documented acceptance
            return int(event_position) == int(wait.target_position_id)

        wait = self._claim_wait(message, predicate=_matches)
        if wait is not None and not wait.future.done():
            wait.future.set_result(message)
        elif execution_type == msg.ExecutionType.ORDER_FILLED:
            logger.debug("cTrader fill event with no pending order wait")

    def _resolve_order_error(self, message: ApiMessage) -> None:
        """ProtoOAOrderErrorEvent fails the matching pending order request."""
        wait = self._claim_wait(message)  # any pending waiter may receive it
        if wait is not None and not wait.future.done():
            wait.future.set_exception(CTraderApiError(
                str(message.payload.get("errorCode", "ORDER_ERROR")),
                str(message.payload.get("description", "")),
            ))

    def _cache_spot(self, payload: dict[str, Any]) -> None:
        symbol_id = int(payload.get("symbolId", 0))
        self._spot[symbol_id] = {
            "bid": price_to_real(payload.get("bid")),
            "ask": price_to_real(payload.get("ask")),
            "timestamp": float(payload.get("timestamp") or 0.0),
        }

    async def _heartbeat_loop(self) -> None:
        """Send ProtoHeartbeatEvent whenever the connection has been idle.

        Docs: a client may be disconnected if it sends nothing for longer
        than 30 seconds; the official SDK heartbeats after 20s idle, which
        we mirror (config.heartbeat_interval). If sending fails the
        connection is dead — stop the loop loudly, not silently.
        """
        try:
            while True:
                await asyncio.sleep(self.config.heartbeat_interval)
                idle = time.monotonic() - self._last_sent
                if idle >= self.config.heartbeat_interval:
                    await self._send(ApiMessage(payload_type=msg.HEARTBEAT_EVENT))
        except asyncio.CancelledError:
            pass
        except Exception as exc:  # noqa: BLE001 — a dead heartbeat = dead connection
            logger.error("cTrader heartbeat loop stopped: %s (connection lost)", exc)
            if self._connected:
                self._connected = False
                self._fail_pending(ConnectionError("heartbeat send failed"))

    # ------------------------------------------------------------------
    # Symbol plumbing
    # ------------------------------------------------------------------

    @property
    def symbol(self) -> str:
        """Resolved default symbol name (e.g. 'XAUUSD' on IC Markets)."""
        return self._symbol or self.config.symbol

    def _resolve_symbol_name(self, symbol: str) -> Optional[str]:
        """Match a requested symbol against the broker's symbol list."""
        candidates = SYMBOL_ALIASES.get(symbol, [symbol]) + [symbol]
        for candidate in candidates:
            if candidate.lower() in self._symbol_ids:
                return candidate
        return None

    def _symbol_id(self, symbol: str) -> int:
        """symbolId for a requested symbol name; raises if unknown."""
        resolved = self._resolve_symbol_name(symbol)
        if resolved is None:
            raise CTraderApiError("SYMBOL_NOT_FOUND", f"symbol {symbol} not listed")
        return self._symbol_ids[resolved.lower()]

    def _lot_size(self, symbol_id: int) -> float:
        """lotSize for a symbol; raises when it was never loaded — a guess
        here mis-scales the order volume silently (order-path rule 20/11)."""
        lot_size = self._lot_sizes.get(symbol_id)
        if lot_size is None:
            raise CTraderApiError(
                "LOT_SIZE_NOT_LOADED",
                f"lotSize for symbolId {symbol_id} was never loaded — refusing to guess",
            )
        return lot_size

    async def subscribe_spots(self, symbol: str) -> bool:
        """ProtoOASubscribeSpotsReq — keep the spot cache fresh for quotes."""
        try:
            symbol_id = self._symbol_id(symbol)
        except CTraderApiError as exc:
            logger.error("subscribe_spots: %s", exc)
            return False
        if symbol_id in self._subscribed:
            return True
        request = self._build_request(msg.SUBSCRIBE_SPOTS_REQ, {
            "ctidTraderAccountId": self.config.account_id,
            "symbolId": [symbol_id],
            "subscribeToSpotTimestamp": False,
        })
        try:
            await self._await_response(request, msg.SUBSCRIBE_SPOTS_RES)
        except Exception as exc:  # noqa: BLE001 — subscribe failure is non-fatal
            logger.warning("spot subscribe failed for %s: %s", symbol, exc)
            return False
        self._subscribed.add(symbol_id)
        return True

    # ------------------------------------------------------------------
    # MT5Bridge-parity surface
    # ------------------------------------------------------------------

    async def get_candles(
        self,
        symbol: str = "XAUUSD",
        timeframe: str = "M5",
        count: int = 500,
    ) -> pd.DataFrame:
        """Fetch OHLCV trendbars for a symbol/timeframe.

        Returns a DataFrame with a timestamp index and open/high/low/close/
        volume columns (same shape as MT5Bridge.get_candles).
        """
        try:
            symbol_id = self._symbol_id(symbol)
        except CTraderApiError:
            logger.error("get_candles: symbol %s not found", symbol)
            return pd.DataFrame()
        period = msg.TIMEFRAME_TO_TRENDBAR.get(timeframe.upper())
        if period is None:
            logger.error("get_candles: unsupported timeframe %s", timeframe)
            return pd.DataFrame()

        bars = await self._fetch_trendbars(symbol_id, period, count)
        if not bars:
            return pd.DataFrame()
        rows = []
        for bar in bars:
            low_scaled = bar.get("low") or 0
            open_price = price_to_real(low_scaled + (bar.get("deltaOpen") or 0))
            high_price = price_to_real(low_scaled + (bar.get("deltaHigh") or 0))
            close_price = price_to_real(low_scaled + (bar.get("deltaClose") or 0))
            rows.append({
                "timestamp": pd.to_datetime(int(bar.get("utcTimestampInMinutes", 0)) * 60, unit="s"),
                "open": open_price,
                "high": high_price,
                "low": price_to_real(low_scaled),
                "close": close_price,
                "volume": float(bar.get("volume") or 0),
            })
        frame = pd.DataFrame(rows).sort_values("timestamp").drop_duplicates("timestamp")
        return frame.set_index("timestamp").tail(count)

    async def _fetch_trendbars(
        self, symbol_id: int, period: int, count: int,
    ) -> list[dict[str, Any]]:
        """Chunked ProtoOAGetTrendbarsReq — window capped at 1 week/request.

        The server may also cap the number of bars per response (hasMore) —
        when that happens we page BACKWARD inside the same window from the
        oldest bar received, instead of skipping the rest of the window.
        """
        period_seconds = msg.TRENDBAR_PERIOD_SECONDS[period]
        to_ms = int(time.time() * 1000)
        window_start = to_ms - count * period_seconds * 1000
        bars: list[dict[str, Any]] = []
        cursor_to = to_ms
        while len(bars) < count and cursor_to > window_start:
            window_begin = max(window_start, cursor_to - msg.TRENDBAR_MAX_WINDOW_MS)
            request = self._build_request(msg.GET_TRENDBARS_REQ, {
                "ctidTraderAccountId": self.config.account_id,
                "fromTimestamp": window_begin,
                "toTimestamp": cursor_to,
                "period": period,
                "symbolId": symbol_id,
            })
            try:
                response = await self._await_response(request, msg.GET_TRENDBARS_RES)
            except Exception as exc:  # noqa: BLE001 — partial data beats a crash
                logger.error("trendbar fetch failed: %s", exc)
                break
            chunk = response.payload.get("trendbar", [])
            bars.extend(chunk)
            previous_to = cursor_to
            if response.payload.get("hasMore") and chunk:
                # Server capped this window's bars — continue from the oldest
                # bar received, still inside the same window (bar order in
                # the response is not guaranteed, so min() over timestamps).
                cursor_to = min(
                    int(bar.get("utcTimestampInMinutes", 0)) for bar in chunk
                ) * 60_000
            else:
                cursor_to = window_begin
            if cursor_to >= previous_to:
                break  # no backward progress — keep what we have, don't spin
            if cursor_to > window_start:
                # Historical data rate limit: 5 req/s per connection.
                await asyncio.sleep(getattr(self.config, "trendbar_min_interval", 0.2))
        return bars

    async def get_quote(self, symbol: str = "XAUUSD") -> Optional[tuple[float, float]]:
        """Latest (bid, ask) from the spot cache — None if no spot yet."""
        try:
            symbol_id = self._symbol_id(symbol)
        except CTraderApiError:
            return None
        spot = self._spot.get(symbol_id)
        if not spot or spot["bid"] <= 0 or spot["ask"] <= 0:
            return None
        return spot["bid"], spot["ask"]

    # XAUUSD point = 0.01 — the same convention MT5Bridge.get_spread_sync
    # falls back to when symbol info is unavailable. Spread POINTS is the
    # unit the engine's SWING_MAX_SPREAD gate compares against (30 = $0.30).
    SPREAD_POINT = 0.01

    async def get_spread(self, symbol: str = "XAUUSD") -> Optional[float]:
        """Spread in POINTS from the live spot quote (MT5 shape: float).

        None when no spot yet (engine skips the cycle — safe) or when the
        quote is malformed. No MT5Bridge async counterpart exists; the
        sync wrapper get_spread_sync delegates here and adds the
        persistent-loop connection policy.
        """
        quote = await self.get_quote(symbol)
        if quote is None:
            return None
        bid, ask = quote
        if bid <= 0 or ask <= 0 or ask < bid:
            return None
        return round((ask - bid) / self.SPREAD_POINT, 0)

    async def position_exists(self, ticket: int) -> Optional[bool]:
        """Is the position still open? (MT5 position_exists_sync shape).

        True  — still open
        False — gone (broker closed it via SL/TP/manual/stopout)
        None  — cannot answer (symbol/bridge trouble) — callers must NOT
                treat None as gone
        """
        try:
            for pos in await self._reconcile():
                if int(pos.get("positionId", 0)) == int(ticket):
                    return True
            return False
        except Exception as exc:  # noqa: BLE001 — parity: errors return None
            logger.warning("position_exists(%s) failed: %s", ticket, exc)
            return None

    async def get_deal_history(
        self, symbol: str = "XAUUSD", days_back: int = 7,
    ) -> list[dict]:
        """ProtoOADealListReq — deals in a window, MT5-shaped dicts.

        Same dict contract as MT5Bridge.get_deal_history (the engine's
        reconciliation reads these keys — see _reconcile_external_close
        and metty.core.db._match_closing_deal): ticket, order, position_id,
        time (epoch SECONDS), type (0=BUY / 1=SELL deal), volume (lots),
        price, profit, symbol, comment.

        cTrader mapping notes:
        - positionId IS the engine's ticket (Strategy-1 match in
          reconciliation is the reliable one on cTrader; MT5's
          deal.order==ticket heuristic does not hold here because orderId
          and positionId are distinct id spaces).
        - dealType "reason" has no cTrader equivalent — exit_reason falls
          back to the comment pass-through the engine already handles.
        - profit comes from closePositionDetail.grossProfit (money-scaled)
          on closing deals, 0.0 otherwise.
        - Paged by hasMore (server caps rows per response); the cursor
          walks the window backward from the oldest deal received.

        Error parity with MT5Bridge.get_deal_history: failures RAISE (the
        sync wrapper converts to None so the engine skips reconciliation
        instead of false-closing on an empty window).
        """
        symbol_id = self._symbol_id(symbol)
        lot_size = self._lot_size(symbol_id)

        now_ms = int(time.time() * 1000)
        from_ms = now_ms - days_back * 86_400_000
        deals: list[dict[str, Any]] = []
        cursor_to = now_ms
        max_rows = 200
        # Iteration bound: a runaway hasMore loop must not spin forever.
        for _ in range(64):
            request = self._build_request(msg.DEAL_LIST_REQ, {
                "ctidTraderAccountId": self.config.account_id,
                "fromTimestamp": max(from_ms, cursor_to - 30 * 86_400_000),
                "toTimestamp": cursor_to,
                "maxRows": max_rows,
            })
            response = await self._await_response(request, msg.DEAL_LIST_RES)
            chunk = response.payload.get("deal", [])
            if not chunk:
                break
            for deal in chunk:
                deals.append(self._deal_to_mt5_shape(deal, symbol_id, lot_size))
            if not response.payload.get("hasMore"):
                break
            oldest = min(int(d.get("createTimestamp") or 0) for d in chunk)
            cursor_to = oldest - 1
            if cursor_to <= from_ms:
                break
        return deals

    def _deal_to_mt5_shape(
        self, deal: dict[str, Any], symbol_id: int, lot_size: float,
    ) -> dict[str, Any]:
        """Map one ProtoOADeal to the MT5 deal dict the engine expects."""
        close_detail = deal.get("closePositionDetail") or {}
        money_digits = int(
            close_detail.get("moneyDigits") or deal.get("moneyDigits") or 2,
        )
        gross_profit = close_detail.get("grossProfit")
        profit = (
            money_to_real(gross_profit, money_digits)
            if gross_profit is not None else 0.0
        )
        filled = deal.get("filledVolume") or deal.get("volume")
        return {
            "ticket": int(deal.get("dealId", 0)),
            "order": int(deal.get("orderId", 0)),
            "position_id": int(deal.get("positionId", 0)),
            "time": float(deal.get("executionTimestamp") or 0) / 1000.0,
            "type": 0 if deal.get("tradeSide") == msg.TradeSide.BUY else 1,
            "volume": volume_cents_to_lots(int(filled or 0), lot_size),
            "price": float(deal.get("executionPrice") or 0.0),
            "profit": profit,
            "symbol": self._symbol_names.get(symbol_id, self.config.symbol),
            "comment": deal.get("comment") or "",
        }

    async def send_order(
        self,
        symbol: str,
        direction: str,
        lots: float,
        stop_loss: Optional[float] = None,
        take_profit: Optional[float] = None,
    ) -> OrderResult:
        """Send a MARKET order with SL/TP as relative distances.

        Absolute stopLoss/takeProfit are not supported for MARKET orders in
        the Open API — we convert them to relativeStopLoss/relativeTakeProfit
        (1/100000) anchored on the current quote. Fill info comes back as a
        push ProtoOAExecutionEvent (ORDER_FILLED).
        """
        try:
            symbol_id = self._symbol_id(symbol)
        except CTraderApiError as exc:
            return self._order_error(f"Symbol {symbol} not found: {exc}")

        quote = await self.get_quote(symbol)
        if quote is None:
            return self._order_error(f"Symbol {symbol} not found (no spot quote)")
        bid, ask = quote

        side = direction.upper()
        if side not in ("BUY", "SELL"):
            return self._order_error(f"Invalid direction: {direction}")
        lot_size = self._lot_size(symbol_id)
        volume_cents = lots_to_volume_cents(lots, lot_size)
        if volume_cents <= 0:
            return self._order_error(f"Invalid volume: {lots} lots")

        payload: dict[str, Any] = {
            "ctidTraderAccountId": self.config.account_id,
            "symbolId": symbol_id,
            "orderType": msg.OrderType.MARKET,
            "tradeSide": msg.TradeSide.BUY if side == "BUY" else msg.TradeSide.SELL,
            "volume": volume_cents,
            "comment": f"god-port-{self.config.name}"[:512],
        }
        base = ask if side == "BUY" else bid
        if stop_loss:
            distance = (base - stop_loss) if side == "BUY" else (stop_loss - base)
            payload["relativeStopLoss"] = price_to_proto(distance)
        if take_profit:
            distance = (take_profit - base) if side == "BUY" else (base - take_profit)
            payload["relativeTakeProfit"] = price_to_proto(distance)

        request = self._build_request(msg.NEW_ORDER_REQ, payload)
        try:
            event = await self._await_execution(request)
        except asyncio.TimeoutError:
            return self._order_error("Order timeout: no execution event")
        except CTraderApiError as exc:
            return self._order_error(f"Order rejected: {exc.code} {exc.description}")
        except ConnectionError as exc:
            return self._order_error(f"Error sending order: {exc}")

        return self._order_result_from_event(event, lot_size)

    def _order_result_from_event(self, event: ApiMessage, lot_size: float) -> OrderResult:
        """Map a terminal ProtoOAExecutionEvent to the engine's OrderResult.

        Only terminal events reach here (see _resolve_execution) — ACCEPTED
        and PARTIAL_FILL were already filtered out upstream.
        """
        payload = event.payload
        execution_type = payload.get("executionType")
        if execution_type != msg.ExecutionType.ORDER_FILLED or payload.get("errorCode"):
            return self._order_error(
                f"Order rejected: {payload.get('errorCode') or execution_type}",
            )
        deal = payload.get("deal") or {}
        position = payload.get("position") or {}
        ticket = deal.get("positionId") or position.get("positionId")
        fill_price = deal.get("executionPrice")
        filled = deal.get("filledVolume") or deal.get("volume")
        return OrderResult(
            success=True,
            ticket=int(ticket) if ticket else None,
            price=float(fill_price) if fill_price else None,
            volume=volume_cents_to_lots(int(filled), lot_size) if filled else None,
            timestamp=datetime.now(),
        )

    @staticmethod
    def _order_error(message: str) -> OrderResult:
        logger.error(message)
        return OrderResult(success=False, error=message, timestamp=datetime.now())

    async def get_positions(self, symbol: str = "XAUUSD") -> list[dict]:
        """Open positions for a symbol — same dict shape as MT5Bridge."""
        try:
            positions = await self._reconcile()
        except Exception as exc:  # noqa: BLE001 — parity: errors return []
            logger.error("Error getting positions: %s", exc)
            return []
        try:
            wanted_id = self._symbol_id(symbol)
        except CTraderApiError:
            return []

        result: list[dict] = []
        for pos in positions:
            trade_data = pos.get("tradeData") or {}
            if int(trade_data.get("symbolId", 0)) != wanted_id:
                continue
            symbol_id = wanted_id
            lot_size = self._lot_size(symbol_id)
            spot = self._spot.get(symbol_id) or {"bid": 0.0, "ask": 0.0}
            entry = float(pos.get("price") or 0.0)
            side = "BUY" if trade_data.get("tradeSide") == msg.TradeSide.BUY else "SELL"
            current = (spot["bid"] if side == "BUY" else spot["ask"]) or entry
            units = int(trade_data.get("volume") or 0) / 100.0
            if side == "BUY":
                profit = (current - entry) * units
            else:
                profit = (entry - current) * units
            opened_ms = trade_data.get("openTimestamp") or 0
            result.append({
                "ticket": int(pos.get("positionId", 0)),
                "symbol": self._symbol_names.get(symbol_id, symbol),
                "type": side,
                "volume": volume_cents_to_lots(int(trade_data.get("volume") or 0), lot_size),
                "price_open": entry,
                "price_current": current,
                "sl": float(pos.get("stopLoss") or 0.0),
                "tp": float(pos.get("takeProfit") or 0.0),
                "profit": profit,
                "time": datetime.fromtimestamp(opened_ms / 1000.0),
            })
        return result

    async def _reconcile(self) -> list[dict[str, Any]]:
        """ProtoOAReconcileReq — raw open positions for this account."""
        request = self._build_request(msg.RECONCILE_REQ, {
            "ctidTraderAccountId": self.config.account_id,
        })
        response = await self._await_response(request, msg.RECONCILE_RES)
        return list(response.payload.get("position", []))

    async def get_account_info(self) -> Optional[AccountInfo]:
        """Account snapshot — same AccountInfo model as the MT5 path."""
        request = self._build_request(msg.TRADER_REQ, {
            "ctidTraderAccountId": self.config.account_id,
        })
        try:
            response = await self._await_response(request, msg.TRADER_RES)
        except Exception as exc:  # noqa: BLE001 — parity: errors return None
            logger.error("Error getting account info: %s", exc)
            return None
        trader = response.payload.get("trader") or {}
        money_digits = int(trader.get("moneyDigits") or 2)
        balance = money_to_real(trader.get("balance"), money_digits)

        equity_extra = 0.0
        margin = 0.0
        try:
            for pos in await self._reconcile():
                trade_data = pos.get("tradeData") or {}
                spot = self._spot.get(int(trade_data.get("symbolId", 0))) or {
                    "bid": 0.0, "ask": 0.0,
                }
                entry = float(pos.get("price") or 0.0)
                side = trade_data.get("tradeSide")
                units = int(trade_data.get("volume") or 0) / 100.0
                # No live spot yet -> PnL contribution 0 (never a huge fake loss)
                close_price = spot["bid"] if side == msg.TradeSide.BUY else spot["ask"]
                if close_price > 0:
                    if side == msg.TradeSide.BUY:
                        equity_extra += (close_price - entry) * units
                    else:
                        equity_extra += (entry - close_price) * units
                margin += money_to_real(pos.get("usedMargin"), pos.get("moneyDigits") or 2)
        except Exception as exc:  # noqa: BLE001 — equity estimate must not kill info
            logger.warning("equity estimate skipped: %s", exc)

        currency = self._assets.get(int(trader.get("depositAssetId") or 0), "USD") or "USD"
        return AccountInfo(
            balance=balance,
            equity=balance + equity_extra,
            margin=margin,
            free_margin=balance + equity_extra - margin,
            leverage=leverage_from_cents(
                trader.get("leverageInCents"), trader.get("maxLeverage"),
            ),
            currency=currency,
            name=self.config.name,
        )

    async def close_position(self, ticket: int) -> bool:
        """Close a position by ticket (positionId)."""
        ok, _fill = await self.close_position_with_fill(ticket)
        return ok

    async def close_position_with_fill(self, ticket: int) -> tuple[bool, Optional[float]]:
        """Close a position, returning (success, fill_price).

        Needs the position's current volume: reconcile first, then
        ProtoOAClosePositionReq; the answer is a push ProtoOAExecutionEvent.
        """
        try:
            position = await self._position_by_id(ticket)
        except Exception as exc:  # noqa: BLE001 — parity: missing position -> False
            logger.warning("Position %s not found: %s", ticket, exc)
            return False, None
        trade_data = position.get("tradeData") or {}
        volume_cents = int(trade_data.get("volume") or 0)
        if volume_cents <= 0:
            logger.warning("Position %s has no volume", ticket)
            return False, None

        request = self._build_request(msg.CLOSE_POSITION_REQ, {
            "ctidTraderAccountId": self.config.account_id,
            "positionId": int(ticket),
            "volume": volume_cents,
        })
        try:
            event = await self._await_execution(request, target_position_id=int(ticket))
        except asyncio.TimeoutError:
            logger.error("Close position %s timeout", ticket)
            return False, None
        except CTraderApiError as exc:
            logger.error("Close position %s rejected: %s %s", ticket, exc.code, exc.description)
            return False, None

        payload = event.payload
        if payload.get("errorCode") or payload.get("executionType") != msg.ExecutionType.ORDER_FILLED:
            logger.error("Close position %s rejected: %s", ticket, payload.get("errorCode"))
            return False, None
        deal = payload.get("deal") or {}
        fill = deal.get("executionPrice")
        return True, float(fill) if fill else None

    async def _position_by_id(self, ticket: int) -> dict[str, Any]:
        """Reconcile and pick the position with this positionId."""
        for pos in await self._reconcile():
            if int(pos.get("positionId", 0)) == int(ticket):
                return pos
        raise CTraderApiError("POSITION_NOT_FOUND", f"position {ticket} not open")

    async def amend_position_sltp(
        self,
        ticket: int,
        stop_loss: Optional[float] = None,
        take_profit: Optional[float] = None,
    ) -> bool:
        """ProtoOAAmendPositionSLTPReq — absolute SL/TP on an open position."""
        payload: dict[str, Any] = {
            "ctidTraderAccountId": self.config.account_id,
            "positionId": int(ticket),
        }
        if stop_loss:
            payload["stopLoss"] = float(stop_loss)
        if take_profit:
            payload["takeProfit"] = float(take_profit)
        request = self._build_request(msg.AMEND_POSITION_SLTP_REQ, payload)
        try:
            event = await self._await_execution(request, target_position_id=int(ticket))
        except Exception as exc:  # noqa: BLE001 — parity: amend errors return False
            logger.error("Amend SL/TP for %s failed: %s", ticket, exc)
            return False
        return not event.payload.get("errorCode")

    async def health_check(self) -> bool:
        """ProtoOAVersionReq round-trip — True only on a live response."""
        try:
            request = self._build_request(msg.VERSION_REQ, {})
            await self._await_response(request, msg.VERSION_RES)
            return True
        except Exception as exc:  # noqa: BLE001 — any failure is unhealthy
            logger.warning("cTrader health check failed: %s", exc)
            self._connected = False
            return False

    # ------------------------------------------------------------------
    # Discovery helper (connection day)
    # ------------------------------------------------------------------

    async def list_accounts_by_token(self) -> list[dict[str, Any]]:
        """ProtoOAGetAccountListByAccessTokenReq — accounts on this token.

        Requires application auth (layer 1) only — the bridge reaches that
        state via connect(); on failure returns [].
        """
        request = self._build_request(msg.GET_ACCOUNTS_BY_ACCESS_TOKEN_REQ, {
            "accessToken": self.config.access_token,
        })
        try:
            response = await self._await_response(request, msg.GET_ACCOUNTS_BY_ACCESS_TOKEN_RES)
        except Exception as exc:  # noqa: BLE001 — discovery is best-effort
            logger.error("account discovery failed: %s", exc)
            return []
        return [
            {
                "ctidTraderAccountId": int(entry.get("ctidTraderAccountId", 0)),
                "isLive": bool(entry.get("isLive")),
                "traderLogin": entry.get("traderLogin"),
                "broker": entry.get("brokerTitleShort") or "",
            }
            for entry in response.payload.get("ctidTraderAccount", [])
        ]

    # ------------------------------------------------------------------
    # Sync-call parity surface (engine wiring stage 2, 20/11)
    # ------------------------------------------------------------------
    # LiveTrader's nine self-serve points call the bridge from SYNCHRONOUS
    # code (get_spread_sync / fetch_account_info_sync / ...) and from
    # short-lived asyncio.run flows (connect / send_order / close). The
    # wrappers below keep ONE persistent connection on ONE persistent
    # loop (hot-path rule: connect ≈ 2s of app+account auth, several
    # calls per engine cycle — reconnecting per call would sink it).

    def ensure_connected_sync(self, timeout: Optional[float] = None) -> bool:
        """Start the persistent loop (if needed) and connect once.

        Idempotent: an already-connected bridge returns True without
        touching the wire. The FIRST caller pays the ~2s handshake; every
        later caller in this process reuses the same connection until the
        server drops it (the next call then reconnects exactly once).
        """
        if self._connected:
            return True
        self._sync_loop.ensure_started()
        if self._sync_loop.is_current():
            raise RuntimeError(
                "ensure_connected_sync called from the persistent loop — "
                "deadlock by construction (report this: engine wiring bug)",
            )
        connect_timeout = timeout or max(
            30.0, self.config.request_timeout * 6.0,
        )
        try:
            return bool(self._sync_loop.submit(self.connect(), connect_timeout))
        except Exception as exc:  # noqa: BLE001 — connect() itself returns False
            logger.error("cTrader sync connect failed: %s", exc)
            return False

    def run_coroutine_sync(self, coro: Any, timeout: Optional[float] = None) -> Any:
        """Run one bridge coroutine on the persistent loop and wait.

        Public for CTraderEngineAdapter: the engine's asyncio.run flows
        await adapter methods from a THROWAWAY loop, but this bridge's
        connection (receive task, pending futures, heartbeat) lives on
        the persistent loop — a future created on any other loop would
        never resolve (responses arrive on the loop that owns the
        transport). Exceptions propagate exactly as a direct await would.
        """
        if self._sync_loop.is_current():
            raise RuntimeError(
                "run_coroutine_sync called from the persistent loop — "
                "deadlock by construction (report this: engine wiring bug)",
            )
        self._sync_loop.ensure_started()
        return self._sync_loop.submit(
            coro, timeout or self.config.request_timeout * 2.0,
        )

    def _sync_run(self, coro: Any) -> Any:
        """Ensure-connected then run — the body every sync wrapper shares.

        Returns None on ANY failure so each wrapper can apply its own
        MT5Bridge failure shape (None / empty DataFrame). Never raises
        into the engine cycle.
        """
        try:
            if not self.ensure_connected_sync():
                return None
            return self.run_coroutine_sync(coro)
        except Exception as exc:  # noqa: BLE001 — parity: MT5 wrappers return None
            logger.error("cTrader sync call failed: %s", exc)
            return None

    def fetch_candles_sync(
        self,
        symbol: str = "XAUUSD",
        timeframe: str = "M5",
        count: int = 500,
    ) -> pd.DataFrame:
        """Candles on the persistent connection (MT5Bridge shape).

        MT5 failure parity: empty DataFrame (never None, never raises).
        """
        frame = self._sync_run(self.get_candles(symbol, timeframe, count))
        return frame if isinstance(frame, pd.DataFrame) else pd.DataFrame()

    def fetch_account_info_sync(self) -> Optional[AccountInfo]:
        """Account snapshot on the persistent connection (MT5Bridge shape)."""
        return self._sync_run(self.get_account_info())

    def get_spread_sync(self, symbol: str = "XAUUSD") -> Optional[float]:
        """Spread points on the persistent connection (MT5Bridge shape)."""
        return self._sync_run(self.get_spread(symbol))

    def get_quote_sync(self, symbol: str = "XAUUSD") -> Optional[tuple[float, float]]:
        """(bid, ask) on the persistent connection (MT5Bridge shape)."""
        return self._sync_run(self.get_quote(symbol))

    def position_exists_sync(self, ticket: int) -> Optional[bool]:
        """Position-exists check on the persistent connection.

        None = UNKNOWN (bridge trouble) — callers must NOT treat as gone.
        """
        return self._sync_run(self.position_exists(ticket))

    def fetch_deal_history_sync(
        self, symbol: str = "XAUUSD", days_back: int = 7,
    ) -> Optional[list[dict]]:
        """Deal history on the persistent connection (MT5Bridge shape).

        None = bridge failure ("do NOT reconcile"); [] = healthy window
        with no deals. Callers rely on that distinction — see
        LiveTrader._get_deal_history.
        """
        return self._sync_run(self.get_deal_history(symbol, days_back=days_back))
