"""Multi-Account Manager — manages 3 MT5 connections for ML data collection.

Routes signals to accounts based on group-to-account mapping.
Fetches market data from the primary account (all see the same market).
Manages position tracking and account health across all connections.
"""

import asyncio
import logging
from typing import Optional

import pandas as pd

from metty.bridge.client import MT5Bridge
from metty.core.models import AccountConfig, AccountInfo, AccountName, OrderResult, SignalGroup

logger = logging.getLogger(__name__)

# Default group-to-account routing
DEFAULT_ROUTING: dict[SignalGroup, list[AccountName]] = {
    SignalGroup.VOLUME: [AccountName.A],
    SignalGroup.OB_OS: [AccountName.B],
    SignalGroup.MA: [AccountName.C],
    SignalGroup.SENTIMENT: [AccountName.A, AccountName.B, AccountName.C],
}

DEFAULT_ACCOUNT_CONFIGS: dict[AccountName, dict] = {
    AccountName.A: {
        "balance": 100.0,
        "leverage": 2000,
        "signal_group": SignalGroup.VOLUME,
        "port": 5005,
    },
    AccountName.B: {
        "balance": 500.0,
        "leverage": 2000,
        "signal_group": SignalGroup.OB_OS,
        "port": 5006,
    },
    AccountName.C: {
        "balance": 1000.0,
        "leverage": 500,
        "signal_group": SignalGroup.MA,
        "port": 5007,
    },
}


class MultiAccountManager:
    """Manages 3 MT5 demo accounts with different balance/leverage combinations.

    Each account connects to a separate MT5 Docker container via RPyC bridge.
    Signals are routed to accounts based on their originating group.
    Market data is fetched from Account A (all accounts see the same market).
    """

    def __init__(
        self,
        account_configs: Optional[dict[AccountName, AccountConfig]] = None,
        routing: Optional[dict[SignalGroup, list[AccountName]]] = None,
    ):
        self.bridges: dict[AccountName, MT5Bridge] = {}
        self.routing = routing or DEFAULT_ROUTING
        self._primary = AccountName.A  # Primary data source

        if account_configs:
            for name, config in account_configs.items():
                self.bridges[name] = MT5Bridge(config)

    def add_account(self, config: AccountConfig) -> None:
        """Add an account connection."""
        self.bridges[config.name] = MT5Bridge(config)

    async def connect_all(self) -> dict[AccountName, bool]:
        """Connect to all MT5 bridge servers.

        Returns a dict of account name -> connection success.
        """
        results = {}
        for name, bridge in self.bridges.items():
            success = await bridge.connect()
            results[name] = success
            if success:
                logger.info(f"Account {name.value}: Connected to {bridge.host}:{bridge.port}")
            else:
                logger.error(f"Account {name.value}: Failed to connect to {bridge.host}:{bridge.port}")
        return results

    async def disconnect_all(self) -> None:
        """Disconnect from all MT5 bridge servers."""
        for name, bridge in self.bridges.items():
            await bridge.disconnect()
        logger.info("All MT5 connections closed")

    async def fetch_candles(
        self,
        symbol: str = "XAUUSD",
        timeframes: Optional[list[str]] = None,
        count: int = 500,
    ) -> dict[str, pd.DataFrame]:
        """Fetch latest candles from the primary account.

        All accounts see the same market, so we only need one connection
        for data. Returns a dict of timeframe -> DataFrame.
        """
        if timeframes is None:
            timeframes = ["M5", "M15", "H1", "H4", "D1"]

        primary = self.bridges.get(self._primary)
        if not primary:
            logger.error("Primary account bridge not available")
            return {}

        result = {}
        for tf in timeframes:
            df = await primary.get_candles(symbol, tf, count)
            if not df.empty:
                result[tf] = df
            else:
                logger.warning(f"No data for {symbol} {tf}")

        return result

    async def send_order(
        self,
        symbol: str,
        direction: str,
        lots: float,
        account_name: AccountName,
        stop_loss: Optional[float] = None,
        take_profit: Optional[float] = None,
    ) -> OrderResult:
        """Send an order to a specific account."""
        bridge = self.bridges.get(account_name)
        if not bridge:
            return OrderResult(
                success=False,
                error=f"No bridge for account {account_name.value}",
                timestamp=__import__("datetime").datetime.now(),
            )
        return await bridge.send_order(symbol, direction, lots, stop_loss, take_profit)

    async def send_to_group(
        self,
        symbol: str,
        direction: str,
        lots: float,
        group: SignalGroup,
        stop_loss: Optional[float] = None,
        take_profit: Optional[float] = None,
    ) -> list[tuple[AccountName, OrderResult]]:
        """Send an order to all accounts mapped to a signal group."""
        target_accounts = self.routing.get(group, [AccountName.A])
        results = []
        for account_name in target_accounts:
            result = await self.send_order(
                symbol, direction, lots, account_name, stop_loss, take_profit
            )
            results.append((account_name, result))
        return results

    async def check_positions(self, symbol: str = "XAUUSD") -> dict[AccountName, list[dict]]:
        """Check all open positions across all accounts."""
        all_positions = {}
        for name, bridge in self.bridges.items():
            positions = await bridge.get_positions(symbol)
            all_positions[name] = positions
        return all_positions

    async def close_position(self, account_name: AccountName, ticket: int) -> bool:
        """Close a position on a specific account."""
        bridge = self.bridges.get(account_name)
        if not bridge:
            logger.error(f"No bridge for account {account_name.value}")
            return False
        return await bridge.close_position(ticket)

    async def get_all_account_info(self) -> dict[AccountName, Optional[AccountInfo]]:
        """Get account info from all accounts."""
        info = {}
        for name, bridge in self.bridges.items():
            info[name] = await bridge.get_account_info()
        return info

    async def health_check_all(self) -> dict[AccountName, bool]:
        """Check health of all bridge connections."""
        health = {}
        for name, bridge in self.bridges.items():
            health[name] = await bridge.health_check()
        return health

    async def reconnect_failed(self) -> dict[AccountName, bool]:
        """Attempt to reconnect any failed connections."""
        results = {}
        for name, bridge in self.bridges.items():
            if not bridge._connected:
                logger.info(f"Attempting to reconnect account {name.value}...")
                success = await bridge.connect()
                results[name] = success
            else:
                results[name] = True
        return results