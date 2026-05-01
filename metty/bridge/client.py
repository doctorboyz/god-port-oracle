"""MT5 Bridge Client — connects to custom RPyC bridge server.

Each MT5Bridge instance connects to one MT5 terminal via the custom
RPyC bridge server (mt5_bridge_server.py) running in Wine Python
inside a Docker container on the VPS.

The bridge server exposes MetaTrader5 functions as RPyC service methods
and converts namedtuples/numpy arrays to plain dicts/lists for serialization.
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
ORDER_FILLING_IOC = 2


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

    async def connect(self) -> bool:
        """Connect to the RPyC bridge server and initialize MT5."""
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                self._conn = await asyncio.to_thread(
                    rpyc.connect, self.host, self.port
                )
                ok = await asyncio.to_thread(self._conn.root.initialize)
                if ok:
                    self._connected = True
                    logger.info(
                        f"Connected to MT5 bridge at {self.host}:{self.port} "
                        f"(attempt {attempt})"
                    )
                    return True
                else:
                    err = await asyncio.to_thread(self._conn.root.last_error)
                    logger.warning(
                        f"MT5 initialize failed on attempt {attempt}: {err}"
                    )
                    self._conn.close()
                    self._conn = None
            except Exception as e:
                logger.warning(
                    f"Connection attempt {attempt}/{MAX_RETRIES} failed "
                    f"for {self.host}:{self.port}: {e}"
                )
                if self._conn:
                    self._conn.close()
                    self._conn = None

            if attempt < MAX_RETRIES:
                await asyncio.sleep(RETRY_DELAY_SECONDS)

        logger.error(
            f"Failed to connect to MT5 bridge at "
            f"{self.host}:{self.port} after {MAX_RETRIES} attempts"
        )
        self._connected = False
        return False

    async def connect_with_login(
        self, login: int, password: str, server: str
    ) -> bool:
        """Connect and login with credentials.

        Use this when MT5 terminal hasn't logged in yet (after first VNC login,
        the terminal remembers credentials, so plain connect() usually suffices).
        """
        if not await self.connect():
            return False
        try:
            ok = await asyncio.to_thread(
                self._conn.root.login, login, password, server
            )
            if ok:
                logger.info(f"Logged in to {server} as {login}")
                return True
            err = await asyncio.to_thread(self._conn.root.last_error)
            logger.error(f"Login failed: {err}")
            return False
        except Exception as e:
            logger.error(f"Login error: {e}")
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
            logger.info(f"Disconnected from MT5 bridge at {self.host}:{self.port}")

    def _ensure_connected(self) -> rpyc.Connection:
        """Ensure we have an active connection."""
        if not self._conn or not self._connected:
            raise ConnectionError(
                f"Not connected to MT5 bridge at {self.host}:{self.port}"
            )
        return self._conn

    async def get_candles(
        self,
        symbol: str = "XAUUSD",
        timeframe: str = "M5",
        count: int = 500,
    ) -> pd.DataFrame:
        """Fetch OHLCV candles from MT5.

        Returns a DataFrame with columns: timestamp, open, high, low, close, volume
        """
        conn = self._ensure_connected()
        tf = MT5_TIMEFRAMES.get(timeframe, MT5_TIMEFRAMES["M5"])
        try:
            rates = await asyncio.to_thread(
                conn.root.copy_rates_from_pos, symbol, tf, 0, count
            )
            if rates is None or len(rates) == 0:
                logger.warning(f"No data returned for {symbol} {timeframe}")
                return pd.DataFrame()

            df = pd.DataFrame(rates)
            df["time"] = pd.to_datetime(df["time"], unit="s")
            df = df.rename(columns={
                "time": "timestamp",
                "tick_volume": "volume",
            })
            df = df.set_index("timestamp")
            return df

        except Exception as e:
            logger.error(f"Error fetching candles for {symbol} {timeframe}: {e}")
            return pd.DataFrame()

    async def send_order(
        self,
        symbol: str,
        direction: str,
        lots: float,
        stop_loss: Optional[float] = None,
        take_profit: Optional[float] = None,
    ) -> OrderResult:
        """Send a market order to MT5."""
        conn = self._ensure_connected()

        try:
            tick = await asyncio.to_thread(conn.root.symbol_info_tick, symbol)
            if tick is None:
                return OrderResult(
                    success=False,
                    error=f"Symbol {symbol} not found",
                    timestamp=datetime.now(),
                )

            order_type = direction.upper()
            mt5_type = ORDER_TYPE_BUY if order_type == "BUY" else ORDER_TYPE_SELL
            price = tick["ask"] if order_type == "BUY" else tick["bid"]

            request = {
                "action": TRADE_ACTION_DEAL,
                "symbol": symbol,
                "volume": lots,
                "type": mt5_type,
                "price": price,
                "deviation": 20,
                "magic": 234000,
                "comment": f"god-port-{self.config.name.value}",
                "type_time": ORDER_TIME_GTC,
                "type_filling": ORDER_FILLING_IOC,
            }

            if stop_loss:
                request["sl"] = stop_loss
            if take_profit:
                request["tp"] = take_profit

            result = await asyncio.to_thread(conn.root.order_send, request)

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
                logger.error(error_msg)
                return OrderResult(
                    success=False, error=error_msg, timestamp=datetime.now()
                )

        except Exception as e:
            error_msg = f"Error sending order: {e}"
            logger.error(error_msg)
            return OrderResult(
                success=False, error=error_msg, timestamp=datetime.now()
            )

    async def get_positions(self, symbol: str = "XAUUSD") -> list[dict]:
        """Get open positions for a symbol."""
        conn = self._ensure_connected()
        try:
            positions = await asyncio.to_thread(
                conn.root.positions_get, symbol=symbol
            )
            if positions is None:
                return []
            return [
                {
                    "ticket": p.get("ticket"),
                    "symbol": p.get("symbol"),
                    "type": "BUY" if p.get("type") == 0 else "SELL",
                    "volume": p.get("volume"),
                    "price_open": p.get("price_open"),
                    "price_current": p.get("price_current"),
                    "sl": p.get("sl"),
                    "tp": p.get("tp"),
                    "profit": p.get("profit"),
                    "time": datetime.fromtimestamp(p.get("time", 0)),
                }
                for p in positions
            ]
        except Exception as e:
            logger.error(f"Error getting positions: {e}")
            return []

    async def close_position(self, ticket: int) -> bool:
        """Close a position by ticket number."""
        conn = self._ensure_connected()
        try:
            positions = await asyncio.to_thread(
                conn.root.positions_get, ticket=ticket
            )
            if not positions:
                logger.warning(f"Position {ticket} not found")
                return False

            pos = positions[0]
            pos_type = pos.get("type", 0)
            close_type = ORDER_TYPE_SELL if pos_type == 0 else ORDER_TYPE_BUY
            tick = await asyncio.to_thread(
                conn.root.symbol_info_tick, pos["symbol"]
            )
            close_price = tick["bid"] if pos_type == 0 else tick["ask"]

            request = {
                "action": TRADE_ACTION_DEAL,
                "symbol": pos["symbol"],
                "volume": pos["volume"],
                "type": close_type,
                "position": ticket,
                "price": close_price,
                "deviation": 20,
                "magic": 234000,
                "comment": f"god-port-close-{self.config.name.value}",
                "type_filling": ORDER_FILLING_IOC,
            }

            result = await asyncio.to_thread(conn.root.order_send, request)
            return (
                result is not None
                and result.get("retcode") == TRADE_RETCODE_DONE
            )

        except Exception as e:
            logger.error(f"Error closing position {ticket}: {e}")
            return False

    async def get_account_info(self) -> Optional[AccountInfo]:
        """Get current account information from MT5."""
        conn = self._ensure_connected()
        try:
            info = await asyncio.to_thread(conn.root.account_info)
            if info is None:
                return None
            return AccountInfo(
                balance=info.get("balance", 0),
                equity=info.get("equity", 0),
                margin=info.get("margin", 0),
                free_margin=info.get("margin_free", 0),
                leverage=info.get("leverage", 1),
                currency=info.get("currency", "USD"),
                name=self.config.name,
            )
        except Exception as e:
            logger.error(f"Error getting account info: {e}")
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