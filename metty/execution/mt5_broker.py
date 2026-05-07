"""MT5Broker — wraps MT5Bridge to implement BrokerABC.

Provides a clean abstraction layer so execution code depends on BrokerABC,
not directly on the RPyC bridge.
"""

from __future__ import annotations

import logging
import os
from typing import Optional

from metty.execution.broker_abc import (
    AccountInfo as BrokerAccountInfo,
    BrokerABC,
    OrderResult,
    PositionInfo,
)
from metty.core.models import AccountConfig, AccountName

logger = logging.getLogger(__name__)


class MT5Broker(BrokerABC):
    """Broker implementation that delegates to MT5Bridge.

    Handles connection lifecycle and converts between MT5Bridge data formats
    and the standard BrokerABC dataclasses.
    """

    def __init__(self, account: str = "B"):
        self.account = account.upper()
        self._bridge = None
        self._connected = False
        self._account_config = self._build_config()

    def _build_config(self) -> AccountConfig:
        """Build AccountConfig from environment variables."""
        port_map = {"A": 5005, "B": 5006, "C": 5007}
        host_map = {"A": "mt5a", "B": "mt5b", "C": "mt5c"}

        return AccountConfig(
            name=AccountName[self.account],
            bridge_host=os.environ.get(
                f"MT5_BRIDGE_{self.account}_HOST",
                host_map.get(self.account, "100.68.106.101"),
            ),
            bridge_port=int(os.environ.get(
                f"MT5_BRIDGE_{self.account}_PORT",
                str(port_map.get(self.account, 5006)),
            )),
            broker_login=os.environ.get(f"MT5_LOGIN_{self.account}", ""),
            broker_server=os.environ.get(
                f"MT5_SERVER_{self.account}", "Exness-MT5Trial17",
            ),
        )

    def connect(self) -> bool:
        """Connect to MT5 bridge."""
        try:
            from metty.bridge.client import MT5Bridge
            self._bridge = MT5Bridge(self._account_config)
            result = self._bridge.health_check_sync()
            self._connected = result
            return result
        except Exception as e:
            logger.error("MT5Broker connect failed: %s", e)
            self._connected = False
            return False

    def disconnect(self) -> None:
        """Disconnect from MT5 bridge."""
        if self._bridge:
            try:
                import asyncio
                asyncio.run(self._bridge.disconnect())
            except Exception:
                pass
        self._connected = False
        self._bridge = None

    def get_account_info(self) -> Optional[BrokerAccountInfo]:
        """Get account info from MT5."""
        if not self._bridge:
            return None
        try:
            info = self._bridge.fetch_account_info_sync()
            if info is None:
                return None
            return BrokerAccountInfo(
                balance=info.balance,
                equity=info.equity,
                margin=info.margin,
                free_margin=info.free_margin,
                leverage=info.leverage,
                currency=info.currency,
            )
        except Exception as e:
            logger.error("get_account_info failed: %s", e)
            return None

    def get_positions(self, symbol: str = "XAUUSD") -> list[PositionInfo]:
        """Get open positions from MT5."""
        if not self._bridge:
            return []
        try:
            import asyncio
            positions = asyncio.run(self._bridge.get_positions(symbol))
            if positions is None:
                return []
            result = []
            for p in positions:
                result.append(PositionInfo(
                    ticket=p.get("ticket", 0),
                    symbol=p.get("symbol", symbol),
                    direction="BUY" if p.get("type", 0) == 0 else "SELL",
                    volume=p.get("volume", 0.0),
                    open_price=p.get("price_open", 0.0),
                    stop_loss=p.get("sl", 0.0),
                    take_profit=p.get("tp", 0.0),
                    profit=p.get("profit", 0.0),
                    comment=p.get("comment", ""),
                ))
            return result
        except Exception as e:
            logger.error("get_positions failed: %s", e)
            return []

    def open_trade(
        self,
        symbol: str,
        direction: str,
        volume: float,
        stop_loss: float,
        take_profit: float,
        comment: str = "",
    ) -> OrderResult:
        """Open a trade via MT5."""
        if not self._bridge:
            return OrderResult(success=False, error="not connected")
        try:
            result = self._bridge.send_order_sync(
                symbol, direction, volume, stop_loss, take_profit,
            )
            if result.get("success"):
                return OrderResult(
                    success=True,
                    ticket=result.get("ticket"),
                )
            return OrderResult(
                success=False,
                error=result.get("error", "unknown error"),
            )
        except Exception as e:
            return OrderResult(success=False, error=str(e))

    def close_trade(self, ticket: int) -> OrderResult:
        """Close a trade by ticket via MT5."""
        if not self._bridge:
            return OrderResult(success=False, error="not connected")
        try:
            import asyncio
            success = asyncio.run(self._bridge.close_position(ticket))
            if success:
                return OrderResult(success=True, ticket=ticket)
            return OrderResult(success=False, error=f"close failed for ticket {ticket}")
        except Exception as e:
            return OrderResult(success=False, error=str(e))

    def get_candles(
        self,
        symbol: str = "XAUUSD",
        timeframe: str = "M5",
        count: int = 500,
    ) -> Optional[dict]:
        """Get candles from MT5."""
        if not self._bridge:
            return None
        try:
            return self._bridge.fetch_candles_sync(timeframes=[timeframe], count=count)
        except Exception as e:
            logger.error("get_candles failed: %s", e)
            return None

    def health_check(self) -> bool:
        """Check MT5 bridge health."""
        if not self._bridge:
            return False
        try:
            return self._bridge.health_check_sync()
        except Exception:
            return False