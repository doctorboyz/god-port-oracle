"""Broker abstraction — defines the interface for trade execution.

Enables mock/simulated trading for testing and future multi-broker support.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional


@dataclass
class AccountInfo:
    """Account information from broker."""
    balance: float
    equity: float
    margin: float
    free_margin: float
    leverage: int
    currency: str = "USD"


@dataclass
class PositionInfo:
    """Open position information."""
    ticket: int
    symbol: str
    direction: str  # "BUY" or "SELL"
    volume: float  # lots
    open_price: float
    stop_loss: float
    take_profit: float
    profit: float
    comment: str = ""


@dataclass
class OrderResult:
    """Result of an order execution."""
    success: bool
    ticket: Optional[int] = None
    error: Optional[str] = None


class BrokerABC(ABC):
    """Abstract broker interface for trade execution.

    All trading code should depend on this interface, not on a specific broker.
    """

    @abstractmethod
    def connect(self) -> bool:
        """Connect to the broker. Returns True if successful."""

    @abstractmethod
    def disconnect(self) -> None:
        """Disconnect from the broker."""

    @abstractmethod
    def get_account_info(self) -> Optional[AccountInfo]:
        """Get current account information."""

    @abstractmethod
    def get_positions(self, symbol: str = "XAUUSD") -> list[PositionInfo]:
        """Get open positions for a symbol."""

    @abstractmethod
    def open_trade(
        self,
        symbol: str,
        direction: str,
        volume: float,
        stop_loss: float,
        take_profit: float,
        comment: str = "",
    ) -> OrderResult:
        """Open a trade. Returns OrderResult with ticket on success."""

    @abstractmethod
    def close_trade(self, ticket: int) -> OrderResult:
        """Close a trade by ticket. Returns OrderResult."""

    @abstractmethod
    def get_candles(
        self,
        symbol: str = "XAUUSD",
        timeframe: str = "M5",
        count: int = 500,
    ) -> Optional[dict]:
        """Get OHLCV candles. Returns dict with 'M5', 'H1', 'D1' etc keys,
        each containing a DataFrame, or None on failure."""

    @abstractmethod
    def health_check(self) -> bool:
        """Check if broker connection is alive."""


class MockBroker(BrokerABC):
    """Mock broker for testing — records trades without real execution."""

    def __init__(
        self,
        balance: float = 10000.0,
        leverage: int = 2000,
        spread_points: float = 3.0,
    ):
        self.balance = balance
        self.leverage = leverage
        self.spread_points = spread_points
        self._connected = False
        self._positions: list[PositionInfo] = []
        self._next_ticket = 1000
        self._trade_log: list[dict] = []

    def connect(self) -> bool:
        self._connected = True
        return True

    def disconnect(self) -> None:
        self._connected = False

    def get_account_info(self) -> Optional[AccountInfo]:
        equity = self.balance + sum(p.profit for p in self._positions)
        return AccountInfo(
            balance=self.balance,
            equity=equity,
            margin=0.0,
            free_margin=equity,
            leverage=self.leverage,
        )

    def get_positions(self, symbol: str = "XAUUSD") -> list[PositionInfo]:
        return [p for p in self._positions if p.symbol == symbol]

    def open_trade(
        self,
        symbol: str,
        direction: str,
        volume: float,
        stop_loss: float,
        take_profit: float,
        comment: str = "",
    ) -> OrderResult:
        ticket = self._next_ticket
        self._next_ticket += 1
        position = PositionInfo(
            ticket=ticket,
            symbol=symbol,
            direction=direction,
            volume=volume,
            open_price=0.0,  # Would need price feed for real mock
            stop_loss=stop_loss,
            take_profit=take_profit,
            profit=0.0,
            comment=comment,
        )
        self._positions.append(position)
        self._trade_log.append({
            "action": "open", "ticket": ticket, "symbol": symbol,
            "direction": direction, "volume": volume,
            "stop_loss": stop_loss, "take_profit": take_profit,
        })
        return OrderResult(success=True, ticket=ticket)

    def close_trade(self, ticket: int) -> OrderResult:
        for i, p in enumerate(self._positions):
            if p.ticket == ticket:
                self._positions.pop(i)
                self._trade_log.append({
                    "action": "close", "ticket": ticket,
                })
                return OrderResult(success=True, ticket=ticket)
        return OrderResult(success=False, error=f"Ticket {ticket} not found")

    def get_candles(
        self,
        symbol: str = "XAUUSD",
        timeframe: str = "M5",
        count: int = 500,
    ) -> Optional[dict]:
        return None  # Mock doesn't provide price data

    def health_check(self) -> bool:
        return self._connected