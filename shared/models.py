"""Shared data models for the MT5 trading system.

All models use Pydantic for validation. Each model is a pure data container
with no business logic — functions in other modules operate on these models.
"""

from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field, field_validator, model_validator


class SignalType(str, Enum):
    BUY = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"


class PositionAction(str, Enum):
    OPEN = "OPEN"
    CLOSE = "CLOSE"
    SCALE_IN = "SCALE_IN"
    SCALE_OUT = "SCALE_OUT"
    HOLD = "HOLD"


class ScalingAction(str, Enum):
    HOLD = "HOLD"
    BUY = "BUY"
    SELL = "SELL"


class SessionType(str, Enum):
    ASIAN = "asian"
    LONDON = "london"
    NY = "ny"
    OVERLAP = "overlap"


class MarketData(BaseModel):
    """Single OHLCV candle."""
    timestamp: datetime
    open: float = Field(gt=0)
    high: float = Field(gt=0)
    low: float = Field(gt=0)
    close: float = Field(gt=0)
    volume: float = Field(ge=0)

    @model_validator(mode="after")
    def validate_ohlcv(self):
        if self.high < self.low:
            raise ValueError("high must be >= low")
        if self.close > self.high:
            raise ValueError("close must be <= high")
        if self.close < self.low:
            raise ValueError("close must be >= low")
        return self


class Signal(BaseModel):
    """Trading signal produced by Broky's analysis."""
    symbol: str = "XAUUSD"
    signal_type: SignalType
    confidence: float = Field(ge=0.0, le=1.0)
    price: float = Field(gt=0)
    timestamp: datetime
    timeframe: str = "M5"
    indicators: dict = Field(default_factory=dict)
    reason: str = ""

    @field_validator("confidence")
    @classmethod
    def confidence_range(cls, v):
        # Confidence can be 0 for HOLD signals; actionable signals need >= 0.3
        return max(0.0, min(1.0, v))


class Position(BaseModel):
    """Open or pending position."""
    symbol: str = "XAUUSD"
    direction: SignalType
    entry_price: float = Field(gt=0)
    current_price: float = Field(gt=0)
    lot_size: float = Field(gt=0)
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    opened_at: datetime
    session: Optional[SessionType] = None

    @property
    def price_change_pct(self) -> float:
        """Percentage change from entry price. Positive = profit, negative = loss."""
        if self.entry_price == 0:
            return 0.0
        return ((self.current_price - self.entry_price) / self.entry_price) * 100

    @property
    def is_profitable(self) -> bool:
        return self.current_price > self.entry_price if self.direction == SignalType.BUY else self.current_price < self.entry_price


class ScalingDecision(BaseModel):
    """JPMorgan-style position scaling decision based on price change from entry."""
    price_change_pct: float
    action: ScalingAction
    adjustment_pct: float = Field(ge=0, le=100, description="Percentage of original position to adjust by")
    reason: str = ""


class TradeResult(BaseModel):
    """Result of a completed trade."""
    symbol: str = "XAUUSD"
    direction: SignalType
    entry_price: float = Field(gt=0)
    exit_price: float = Field(gt=0)
    lot_size: float = Field(gt=0)
    pnl: float
    pnl_pct: float
    opened_at: datetime
    closed_at: datetime
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    session: Optional[SessionType] = None
    exit_reason: str = ""


class CircuitBreakerState(BaseModel):
    """State of the circuit breaker risk management system."""
    consecutive_losses: int = 0
    daily_loss_pct: float = 0.0
    is_active: bool = False
    cooldown_until: Optional[datetime] = None
    flash_crash_detected: bool = False