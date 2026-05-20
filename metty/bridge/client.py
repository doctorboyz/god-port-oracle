"""MT5 Bridge Client — connects to custom RPyC bridge server.

Each MT5Bridge instance connects to one MT5 terminal via the custom
RPyC bridge server (mt5_bridge_server.py) running in Wine Python
inside a Docker container on the VPS.

The bridge server exposes MetaTrader5 functions as RPyC service methods
and converts namedtuples/numpy arrays to plain dicts/lists for serialization.

IMPORTANT: RPyC returns netref proxies for dicts, not local dicts.
Use bracket notation (info["key"]) not .get("key") to access netref dicts.
"""

import asyncio
import logging
from datetime import datetime
from typing import Optional

import pandas as pd
import rpyc

from metty.core.models import AccountConfig, AccountInfo, OrderResult

logger = logging.getLogger(__name__)

MAX_RETRIES = 3
RETRY_DELAY_SECONDS = 5

# MT5 timeframe constants (must match MetaTrader5 Python package)
MT5_TIMEFRAMES = {
    "M1": 1,
    "M5": 5,
    "M15": 15,
    "M30": 30,
    "H1": 16385,
    "H4": 16388,
    "D1": 16408,
    "W1": 32769,
    "MN": 44641,
}

# MT5 order type constants
ORDER_TYPE_BUY = 0
ORDER_TYPE_SELL = 1
TRADE_ACTION_DEAL = 1
TRADE_RETCODE_DONE = 10009
ORDER_TIME_GTC = 0
ORDER_FILLING_FOK = 0
ORDER_FILLING_IOC = 2

# Broker-specific symbol names
# Exness uses XAUUSDm (micro lot), other brokers may use XAUUSD
SYMBOL_ALIASES = {
    "XAUUSD": ["XAUUSDm", "XAUUSD", "XAUUSD.i", "XAUUSDb"],
}


# Known column names for MT5 data (RPyC blocks .keys() on netref dicts)
CANDLE_COLUMNS = ["time", "open", "high", "low", "close", "tick_volume", "spread", "real_volume"]
TICK_COLUMNS = ["time", "bid", "ask", "last", "volume", "time_msc", "flags", "volume_real"]
POSITION_COLUMNS = ["ticket", "time", "time_update", "type", "magic", "identifier", "reason",
                     "volume", "price_open", "sl", "tp", "price_current", "swap", "profit",
                     "symbol", "comment", "identifier"]
ACCOUNT_COLUMNS = ["login", "balance", "credit", "profit", "equity", "margin", "margin_free",
                    "margin_level", "trade_mode", "name", "server", "currency", "leverage",
                    "trade_allowed", "trade_expert", "margin_mode"]
SYMBOL_COLUMNS = ["name", "digits", "trade_mode", "point", "spread", "trade_contract_size",
                  "trade_stop_level", "volume_min", "volume_max", "volume_step"]


def _netref_to_dict(netref_dict, columns: list[str] | None = None) -> dict:
    """Convert an RPyC netref dict to a local Python dict.

    RPyC blocks .keys() and .get() on netref dicts. Only bracket access works.
    If columns are known, extract them directly. Otherwise try common access patterns.
    Falls back to attribute access for namedtuple-like netrefs that don't support __getitem__.
    """
    if netref_dict is None:
        return {}
    if columns:
        result = {}
        for k in columns:
            try:
                result[k] = netref_dict[k]
            except (KeyError, Exception):
                # Bracket access failed — try attribute access (netref may wrap a namedtuple)
                try:
                    result[k] = getattr(netref_dict, k, None)
                except Exception:
                    pass
        # If all extractions failed, log the netref type for debugging
        if not result:
            logger.warning(
                "_netref_to_dict: empty result — netref type=%s, dir=%s",
                type(netref_dict).__name__,
                [a for a in dir(netref_dict) if not a.startswith("_")][:10],
            )
        return result
    # No known columns — try dict() conversion
    try:
        return dict(netref_dict)
    except Exception:
        try:
            return {a: getattr(netref_dict, a) for a in dir(netref_dict) if not a.startswith("_")}
        except Exception:
            return {}


def _netref_to_list(netref_list, columns: list[str] | None = None) -> list:
    """Convert an RPyC netref list of dicts to a local Python list."""
    if netref_list is None:
        return []
    result = []
    for item in netref_list:
        if isinstance(item, dict) or (hasattr(item, '__getitem__') and not isinstance(item, (str, bytes))):
            result.append(_netref_to_dict(item, columns))
        else:
            result.append(item)
    return result


class MT5Bridge:
    """Single MT5 connection via custom RPyC bridge server.

    Connects to one MT5 terminal instance (running in Docker with Wine)
    and provides methods for fetching data and executing orders.
    """

    def __init__(self, config: AccountConfig):
        self.config = config
        self.host = config.bridge_host
        self.port = config.bridge_port
        self._conn: Optional[rpyc.Connection] = None
        self._connected = False
        self._symbol: Optional[str] = None  # Resolved symbol name

    async def connect(self) -> bool:
        """Connect to the RPyC bridge server and initialize MT5."""
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                self._conn = await asyncio.to_thread(
                    rpyc.connect, self.host, self.port,
                    config={"sync_request_timeout": 30},
                )
                ok = await asyncio.to_thread(self._conn.root.initialize)
                if ok:
                    self._connected = True
                    # Resolve the correct symbol name
                    await self._resolve_symbol()
                    logger.info(
                        "Connected to MT5 bridge at %s:%s (attempt %d, symbol=%s)",
                        self.host, self.port, attempt, self._symbol,
                    )
                    return True
                else:
                    err = await asyncio.to_thread(self._conn.root.last_error)
                    logger.warning(
                        "MT5 initialize failed on attempt %d: %s", attempt, err,
                    )
                    self._conn.close()
                    self._conn = None
            except Exception as e:
                logger.warning(
                    "Connection attempt %d/%d failed for %s:%s: %s",
                    attempt, MAX_RETRIES, self.host, self.port, e,
                )
                if self._conn:
                    self._conn.close()
                    self._conn = None

            if attempt < MAX_RETRIES:
                await asyncio.sleep(RETRY_DELAY_SECONDS)

        logger.error(
            "Failed to connect to MT5 bridge at %s:%s after %d attempts",
            self.host, self.port, MAX_RETRIES,
        )
        self._connected = False
        return False

    async def _resolve_symbol(self) -> None:
        """Find the correct XAUUSD symbol name for this broker.

        Exness uses 'XAUUSDm' (micro lot), other brokers may use 'XAUUSD'.
        """
        conn = self._ensure_connected()
        for symbol in SYMBOL_ALIASES.get("XAUUSD", ["XAUUSD"]):
            try:
                info = await asyncio.to_thread(conn.root.symbol_info, symbol)
                if info is not None:
                    # Symbol exists — select it in Market Watch
                    await asyncio.to_thread(conn.root.symbol_select, symbol, True)
                    self._symbol = symbol
                    logger.info("Resolved symbol: %s", symbol)
                    return
            except Exception:
                continue

        # Fallback to XAUUSD even if not found (broker may add it on demand)
        self._symbol = "XAUUSD"
        logger.warning("Could not find XAUUSD symbol variant, using XAUUSD")

    async def connect_with_login(
        self, login: int, password: str, server: str,
    ) -> bool:
        """Connect and login with credentials."""
        if not await self.connect():
            return False
        try:
            ok = await asyncio.to_thread(
                self._conn.root.login, login, password, server,
            )
            if ok:
                logger.info("Logged in to %s as %d", server, login)
                return True
            err = await asyncio.to_thread(self._conn.root.last_error)
            logger.error("Login failed: %s", err)
            return False
        except Exception as e:
            logger.error("Login error: %s", e)
            return False

    async def disconnect(self) -> None:
        """Disconnect from the MT5 bridge."""
        if self._conn:
            try:
                await asyncio.to_thread(self._conn.root.shutdown)
            except Exception:
                pass
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None
            self._connected = False
            logger.info("Disconnected from MT5 bridge at %s:%s", self.host, self.port)

    def _ensure_connected(self) -> rpyc.Connection:
        """Ensure we have an active connection."""
        if not self._conn or not self._connected:
            raise ConnectionError(
                f"Not connected to MT5 bridge at {self.host}:{self.port}"
            )
        return self._conn

    @property
    def symbol(self) -> str:
        """Resolved symbol name (e.g., 'XAUUSDm' on Exness)."""
        return self._symbol or "XAUUSD"

    async def get_candles(
        self,
        symbol: str = "XAUUSD",
        timeframe: str = "M5",
        count: int = 500,
    ) -> pd.DataFrame:
        """Fetch OHLCV candles from MT5.

        Args:
            symbol: Trading symbol. 'XAUUSD' is auto-resolved to broker variant.
            timeframe: MT5 timeframe string (M1, M5, M15, M30, H1, H4, D1).
            count: Number of candles to fetch.

        Returns:
            DataFrame with timestamp index and OHLCV columns.
        """
        conn = self._ensure_connected()
        resolved = self._resolve_symbol_name(symbol)
        tf = MT5_TIMEFRAMES.get(timeframe, MT5_TIMEFRAMES["M5"])

        try:
            # Try JSON method first (much faster — single network transfer)
            try:
                json_str = await asyncio.to_thread(
                    conn.root.copy_rates_from_pos_json, resolved, tf, 0, count,
                )
                if json_str:
                    import json as _json
                    local_rates = _json.loads(json_str)
                    df = pd.DataFrame(local_rates)
                    if "time" in df.columns:
                        df["time"] = pd.to_datetime(df["time"], unit="s")
                    if "tick_volume" in df.columns:
                        df = df.rename(columns={"tick_volume": "volume"})
                    if "time" in df.columns and "timestamp" not in df.columns:
                        df = df.rename(columns={"time": "timestamp"})
                    if "timestamp" in df.columns:
                        df = df.set_index("timestamp")
                    return df
            except Exception:
                pass  # Fall back to netref method

            # Fallback: netref method (slow for large datasets)
            rates = await asyncio.to_thread(
                conn.root.copy_rates_from_pos, resolved, tf, 0, count,
            )
            if rates is None or len(rates) == 0:
                logger.warning("No data returned for %s %s", resolved, timeframe)
                return pd.DataFrame()

            data = []
            for item in rates:
                row = {}
                for k in CANDLE_COLUMNS:
                    try:
                        v = item[k]
                        # Force plain Python types — netrefs become stale after disconnect
                        if k == "time":
                            row[k] = int(v)
                        else:
                            row[k] = float(v)
                    except (KeyError, Exception):
                        pass
                data.append(row)

            df = pd.DataFrame(data)

            if "time" in df.columns:
                df["time"] = pd.to_datetime(df["time"].astype(int), unit="s")

            # Normalize column names
            rename_map = {}
            if "tick_volume" in df.columns:
                rename_map["tick_volume"] = "volume"
            if "time" in df.columns and "timestamp" not in df.columns:
                rename_map["time"] = "timestamp"

            df = df.rename(columns=rename_map)

            if "timestamp" in df.columns:
                df = df.set_index("timestamp")

            return df

        except Exception as e:
            logger.error("Error fetching candles for %s %s: %s", resolved, timeframe, e)
            return pd.DataFrame()

    def _resolve_symbol_name(self, symbol: str) -> str:
        """Resolve a generic symbol name to broker-specific variant."""
        if symbol == "XAUUSD" and self._symbol:
            return self._symbol
        # Check aliases
        aliases = SYMBOL_ALIASES.get(symbol, [symbol])
        if self._symbol and symbol == "XAUUSD":
            return self._symbol
        return symbol

    async def send_order(
        self,
        symbol: str,
        direction: str,
        lots: float,
        stop_loss: Optional[float] = None,
        take_profit: Optional[float] = None,
    ) -> OrderResult:
        """Send a market order to MT5.

        Uses individual arguments (not dict) for RPyC compatibility.
        """
        conn = self._ensure_connected()
        resolved = self._resolve_symbol_name(symbol)

        try:
            tick_netref = await asyncio.to_thread(
                conn.root.symbol_info_tick, resolved,
            )
            tick = _netref_to_dict(tick_netref, columns=TICK_COLUMNS)
            if not tick:
                return OrderResult(
                    success=False,
                    error=f"Symbol {resolved} not found",
                    timestamp=datetime.now(),
                )

            order_type = direction.upper()
            mt5_type = ORDER_TYPE_BUY if order_type == "BUY" else ORDER_TYPE_SELL
            price = float(tick["ask"]) if order_type == "BUY" else float(tick["bid"])

            sl = stop_loss if stop_loss else 0.0
            tp = take_profit if take_profit else 0.0

            # Use individual arguments, not dict — RPyC server expects kwargs
            result_netref = await asyncio.to_thread(
                conn.root.order_send,
                TRADE_ACTION_DEAL,    # action
                resolved,              # symbol
                lots,                  # volume
                mt5_type,              # order_type
                price,                 # price
                sl,                    # sl
                tp,                    # tp
                20,                    # deviation
                234000,                # magic
                f"god-port-{self.config.name.value}",  # comment
                ORDER_TIME_GTC,        # type_time
                ORDER_FILLING_FOK,      # type_filling (Exness requires FOK)
                0,                     # position (0 = new order)
            )
            result = _netref_to_dict(result_netref, columns=["retcode", "order", "price", "volume", "comment"])
            logger.info(
                "order_send raw: netref_type=%s, converted_keys=%s, retcode=%s",
                type(result_netref).__name__,
                list(result.keys()) if result else "None",
                result.get("retcode") if result else "N/A",
            )

            if result and result.get("retcode") == TRADE_RETCODE_DONE:
                return OrderResult(
                    success=True,
                    ticket=result.get("order"),
                    price=result.get("price"),
                    volume=result.get("volume"),
                    timestamp=datetime.now(),
                )
            else:
                retcode = result.get("retcode") if result else "No response"
                error_msg = f"Order rejected: {retcode}"
                logger.error(
                    "Order rejected: retcode=%s, result=%s, symbol=%s, direction=%s, lots=%s",
                    retcode, result, resolved, direction, lots,
                )
                return OrderResult(
                    success=False, error=error_msg, timestamp=datetime.now(),
                )

        except Exception as e:
            error_msg = f"Error sending order: {e}"
            logger.error(error_msg)
            return OrderResult(
                success=False, error=error_msg, timestamp=datetime.now(),
            )

    async def get_positions(self, symbol: str = "XAUUSD") -> list[dict]:
        """Get open positions for a symbol."""
        conn = self._ensure_connected()
        resolved = self._resolve_symbol_name(symbol)
        try:
            positions_netref = await asyncio.to_thread(
                conn.root.positions_get, symbol=resolved,
            )
            positions = _netref_to_list(positions_netref, columns=POSITION_COLUMNS)
            if not positions:
                return []
            return [
                {
                    "ticket": p.get("ticket", 0) if isinstance(p, dict) else p["ticket"],
                    "symbol": p.get("symbol", "") if isinstance(p, dict) else p["symbol"],
                    "type": "BUY" if (p.get("type", 1) if isinstance(p, dict) else p["type"]) == 0 else "SELL",
                    "volume": p.get("volume", 0) if isinstance(p, dict) else p["volume"],
                    "price_open": p.get("price_open", 0) if isinstance(p, dict) else p["price_open"],
                    "price_current": p.get("price_current", 0) if isinstance(p, dict) else p["price_current"],
                    "sl": p.get("sl", 0) if isinstance(p, dict) else p["sl"],
                    "tp": p.get("tp", 0) if isinstance(p, dict) else p["tp"],
                    "profit": p.get("profit", 0) if isinstance(p, dict) else p["profit"],
                    "time": datetime.fromtimestamp(
                        p.get("time", 0) if isinstance(p, dict) else p["time"]
                    ),
                }
                for p in positions
            ]
        except Exception as e:
            logger.error("Error getting positions: %s", e)
            return []

    async def close_position(self, ticket: int) -> bool:
        """Close a position by ticket number."""
        conn = self._ensure_connected()
        try:
            positions_netref = await asyncio.to_thread(
                conn.root.positions_get, ticket=ticket,
            )
            positions = _netref_to_list(positions_netref, columns=POSITION_COLUMNS)
            if not positions:
                logger.warning("Position %d not found", ticket)
                return False

            pos = positions[0]
            if isinstance(pos, dict):
                pos = dict(pos)  # Ensure it's a local dict

            pos_type = pos.get("type", 0)
            close_type = ORDER_TYPE_SELL if pos_type == 0 else ORDER_TYPE_BUY

            tick_netref = await asyncio.to_thread(
                conn.root.symbol_info_tick, pos["symbol"],
            )
            tick = _netref_to_dict(tick_netref, columns=TICK_COLUMNS)
            close_price = float(tick["bid"]) if pos_type == 0 else float(tick["ask"])

            result_netref = await asyncio.to_thread(
                conn.root.order_send,
                TRADE_ACTION_DEAL,    # action
                pos["symbol"],        # symbol
                pos["volume"],        # volume
                close_type,           # order_type
                close_price,          # price
                0.0,                  # sl
                0.0,                  # tp
                20,                   # deviation
                234000,               # magic
                f"close-{ticket}",    # comment
                ORDER_TIME_GTC,       # type_time
                ORDER_FILLING_FOK,    # type_filling (Exness requires FOK)
                ticket,               # position
            )
            result = _netref_to_dict(result_netref, columns=["retcode", "order", "price", "volume"])
            return result is not None and result.get("retcode") == TRADE_RETCODE_DONE

        except Exception as e:
            logger.error("Error closing position %d: %s", ticket, e)
            return False

    async def get_account_info(self) -> Optional[AccountInfo]:
        """Get current account information from MT5."""
        conn = self._ensure_connected()
        try:
            info_netref = await asyncio.to_thread(conn.root.account_info)
            info = _netref_to_dict(info_netref, columns=ACCOUNT_COLUMNS)
            if not info:
                return None
            return AccountInfo(
                balance=float(info.get("balance", 0)),
                equity=float(info.get("equity", 0)),
                margin=float(info.get("margin", 0)),
                free_margin=float(info.get("margin_free", 0)),
                leverage=int(info.get("leverage", 1)),
                currency=info.get("currency", "USD"),
                name=self.config.name,
            )
        except Exception as e:
            logger.error("Error getting account info: %s", e)
            return None

    async def health_check(self) -> bool:
        """Check if the bridge connection is alive."""
        try:
            conn = self._ensure_connected()
            version = await asyncio.to_thread(conn.root.version)
            return version is not None
        except Exception:
            self._connected = False
            return False

    # --- Synchronous convenience methods for non-async callers ---
    # IMPORTANT: These use a single asyncio.run() call per operation.
    # Do NOT mix sync methods with async — each sync method creates its own
    # event loop and connection, then disconnects.

    def fetch_candles_sync(
        self,
        symbol: str = "XAUUSD",
        timeframe: str = "M5",
        count: int = 500,
    ) -> pd.DataFrame:
        """Connect, fetch candles, and disconnect in one call."""
        async def _do():
            if not await self.connect():
                return pd.DataFrame()
            df = await self.get_candles(symbol, timeframe, count)
            await self.disconnect()
            return df
        return asyncio.run(_do())

    def fetch_account_info_sync(self) -> Optional[AccountInfo]:
        """Connect, get account info, and disconnect in one call."""
        async def _do():
            if not await self.connect():
                return None
            info = await self.get_account_info()
            await self.disconnect()
            return info
        return asyncio.run(_do())

    def health_check_sync(self) -> bool:
        """Connect, check health, and disconnect in one call."""
        async def _do():
            try:
                if not await self.connect():
                    return False
                ok = await self.health_check()
                await self.disconnect()
                return ok
            except Exception:
                return False
        return asyncio.run(_do())

    def send_order_sync(
        self,
        symbol: str,
        direction: str,
        lots: float,
        stop_loss: Optional[float] = None,
        take_profit: Optional[float] = None,
    ) -> dict:
        """Connect, send order, and disconnect in one call.

        Returns dict with keys: success, ticket, price, volume, error.
        """
        # Sanitize: convert numpy types to plain Python — rpyc can't serialize np.float64
        lots = float(lots)
        if stop_loss is not None:
            stop_loss = float(stop_loss)
        if take_profit is not None:
            take_profit = float(take_profit)

        async def _do():
            if not await self.connect():
                return {"success": False, "error": "bridge connection failed"}
            result = await self.send_order(symbol, direction, lots, stop_loss, take_profit)
            await self.disconnect()
            if result.success:
                return {
                    "success": True,
                    "ticket": result.ticket,
                    "price": result.price,
                    "volume": result.volume,
                }
            return {"success": False, "error": result.error}
        try:
            return asyncio.run(_do())
        except Exception as e:
            logger.error("send_order_sync error: %s", e)
            return {"success": False, "error": str(e)}

    def get_spread_sync(self, symbol: str = "XAUUSD") -> Optional[float]:
        """Connect, get spread from bid/ask, and disconnect in one call."""
        async def _do():
            if not await self.connect():
                return None
            try:
                conn = self._ensure_connected()
                resolved = self._resolve_symbol_name(symbol)
                tick_netref = await asyncio.to_thread(
                    conn.root.symbol_info_tick, resolved,
                )
                tick = _netref_to_dict(tick_netref, columns=TICK_COLUMNS)
                bid = tick.get("bid", 0)
                ask = tick.get("ask", 0)
                if bid > 0 and ask > 0:
                    point = 0.01
                    try:
                        info = await self.get_symbol_info(symbol)
                        if info:
                            point = info.get("point", 0.01)
                    except Exception:
                        pass
                    return round((ask - bid) / point, 0)
            except Exception as e:
                logger.error("Error fetching spread for %s: %s", resolved, e)
            finally:
                await self.disconnect()
            return None
        return asyncio.run(_do())


class PersistentMT5Bridge(MT5Bridge):
    """MT5 connection with keep-alive and auto-reconnect.

    Instead of connect/disconnect per cycle, maintains a persistent
    connection and reconnects only when needed. Designed for high-frequency
    cycles (e.g., M1 scalping at 60s intervals).
    """

    def __init__(self, config: AccountConfig):
        super().__init__(config)
        self._last_ping: float = 0.0
        self._ping_interval: float = 30.0  # seconds between health pings

    async def ensure_connected(self) -> bool:
        """Check connection health and reconnect if needed.

        Pings the bridge periodically. If the connection is dead,
        disconnects and reconnects. Returns True if connected.
        """
        import time as _time

        now = _time.monotonic()

        # Check if recently pinged and still alive
        if self._connected and (now - self._last_ping) < self._ping_interval:
            return True

        # Try a health ping
        if self._connected:
            try:
                ok = await asyncio.wait_for(self.health_check(), timeout=10.0)
                if ok:
                    self._last_ping = now
                    return True
            except Exception:
                logger.warning("Health ping failed for %s:%s, reconnecting", self.host, self.port)

        # Reconnect
        await self.disconnect()
        if await self.connect():
            self._last_ping = now
            return True

        return False

    def ensure_connected_sync(self) -> bool:
        """Synchronous version of ensure_connected."""
        return asyncio.run(self.ensure_connected())

    async def get_symbol_info(self, symbol: str = "XAUUSD") -> Optional[dict]:
        """Fetch symbol info including spread from MT5.

        Returns dict with: name, digits, spread, point, trade_contract_size,
        volume_min, volume_max, volume_step.
        """
        conn = self._ensure_connected()
        resolved = self._resolve_symbol_name(symbol)

        try:
            info_netref = await asyncio.to_thread(
                conn.root.symbol_info, resolved,
            )
            info = _netref_to_dict(info_netref, columns=SYMBOL_COLUMNS)
            if not info:
                return None
            return {
                "name": info.get("name", resolved),
                "digits": info.get("digits", 2),
                "point": info.get("point", 0.01),
                "spread": info.get("spread", 0),
                "trade_contract_size": info.get("trade_contract_size", 100),
                "volume_min": info.get("volume_min", 0.01),
                "volume_max": info.get("volume_max", 100),
                "volume_step": info.get("volume_step", 0.01),
            }
        except Exception as e:
            logger.error("Error fetching symbol info for %s: %s", resolved, e)
            return None

    def get_symbol_info_sync(self, symbol: str = "XAUUSD") -> Optional[dict]:
        """Synchronous wrapper for get_symbol_info."""
        async def _do():
            if not await self.ensure_connected():
                return None
            return await self.get_symbol_info(symbol)
        return asyncio.run(_do())

    def get_spread_sync(self, symbol: str = "XAUUSD") -> Optional[float]:
        """Get current spread in points from real-time bid/ask (symbol_info_tick).

        Uses symbol_info_tick (live bid/ask) instead of symbol_info (static data)
        because symbol_info.spread is a static default, often 0.
        """
        async def _do():
            if not await self.ensure_connected():
                return None
            return await self._get_spread(symbol)
        return asyncio.run(_do())

    async def _get_spread(self, symbol: str) -> Optional[float]:
        """Async: compute spread from symbol_info_tick bid/ask."""
        conn = self._ensure_connected()
        resolved = self._resolve_symbol_name(symbol)
        try:
            tick_netref = await asyncio.to_thread(
                conn.root.symbol_info_tick, resolved,
            )
            tick = _netref_to_dict(tick_netref, columns=TICK_COLUMNS)
            bid = tick.get("bid", 0)
            ask = tick.get("ask", 0)
            if bid > 0 and ask > 0:
                # Get point value from symbol info
                info = await self.get_symbol_info(symbol)
                point = info.get("point", 0.01) if info else 0.01
                return round((ask - bid) / point, 0)
        except Exception as e:
            logger.error("Error fetching spread for %s: %s", resolved, e)
        return None

    async def fetch_candles_persistent(
        self,
        symbol: str = "XAUUSD",
        timeframe: str = "M5",
        count: int = 500,
    ) -> pd.DataFrame:
        """Fetch candles using the persistent connection (no connect/disconnect)."""
        if not await self.ensure_connected():
            return pd.DataFrame()
        return await self.get_candles(symbol, timeframe, count)

    def fetch_candles_persistent_sync(
        self,
        symbol: str = "XAUUSD",
        timeframe: str = "M5",
        count: int = 500,
    ) -> pd.DataFrame:
        """Synchronous wrapper for fetch_candles_persistent."""
        async def _do():
            return await self.fetch_candles_persistent(symbol, timeframe, count)
        return asyncio.run(_do())

    async def disconnect(self) -> None:
        """Disconnect and reset ping timer."""
        self._last_ping = 0.0
        await super().disconnect()