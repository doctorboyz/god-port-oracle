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


class MarketRegime(str, Enum):
    TRENDING = "trending"
    RANGING = "ranging"
    VOLATILE = "volatile"


class TradingMode(str, Enum):
    SWING = "swing"
    SCALP = "scalp"
    M5_SCALP = "m5_scalp"


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
    regime: Optional[str] = None
    trading_mode: TradingMode = TradingMode.SWING
    strategy_id: str = ""

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


class SignalGroup(str, Enum):
    """Indicator groups for ML data collection."""
    VOLUME = "volume"
    OB_OS = "ob_os"
    MA = "ma"
    SENTIMENT = "sentiment"


class GroupSignal(BaseModel):
    """Signal from a single indicator group."""
    group: SignalGroup
    direction: str = Field(pattern="^(BUY|SELL)$")
    confidence: float = Field(ge=0.0, le=1.0)
    triggering_indicators: list[str] = Field(default_factory=list)
    price: float = Field(gt=0)
    timestamp: datetime
    timeframe: str = "M5"
    regime: Optional[str] = None
    session: Optional[str] = None
    d1_trend: Optional[str] = None


class FeatureSnapshot(BaseModel):
    """Complete indicator snapshot at signal time for ML training.

    Records ALL indicator values across ALL groups when ANY group triggers.
    This enables ML to learn cross-group correlations.
    """
    timestamp: datetime
    price: float = Field(gt=0)
    timeframe: str = "M5"
    session: str = ""
    d1_trend: str = ""

    # Volume group
    obv: Optional[float] = None
    obv_slope: Optional[float] = None
    mfi: Optional[float] = None
    vwap_offset_pct: Optional[float] = None
    volume_roc: Optional[float] = None
    ad_line: Optional[float] = None
    ad_line_slope: Optional[float] = None
    cmf: Optional[float] = None

    # OB/OS group
    rsi: Optional[float] = None
    stoch_k: Optional[float] = None
    stoch_d: Optional[float] = None
    williams_r: Optional[float] = None
    cci: Optional[float] = None
    demarker: Optional[float] = None
    roc: Optional[float] = None

    # MA group
    sma_10: Optional[float] = None
    sma_20: Optional[float] = None
    sma_50: Optional[float] = None
    ema_9: Optional[float] = None
    ema_21: Optional[float] = None
    ema_50: Optional[float] = None
    ema_200: Optional[float] = None
    dema_21: Optional[float] = None
    tema_21: Optional[float] = None
    ichimoku_tenkan: Optional[float] = None
    ichimoku_kijun: Optional[float] = None
    ichimoku_senkou_a: Optional[float] = None
    ichimoku_senkou_b: Optional[float] = None
    ichimoku_chikou: Optional[float] = None
    price_vs_cloud: str = ""

    # Sentiment group
    tick_volume_ratio: Optional[float] = None
    spread_ratio: Optional[float] = None
    long_short_ratio: Optional[float] = None
    session_strength: Optional[float] = None

    # Broky original indicators (for comparison)
    macd_hist: Optional[float] = None
    adx: Optional[float] = None
    plus_di: Optional[float] = None
    minus_di: Optional[float] = None
    boll_pct_b: Optional[float] = None
    boll_bw: Optional[float] = None
    atr: Optional[float] = None
    atr_to_price: Optional[float] = None

    # Control variables
    balance_at_entry: Optional[float] = None
    leverage_at_entry: Optional[int] = None

    # External sentiment (from Fear & Greed, news, etc.)
    fear_greed_value: Optional[float] = None
    gold_bias_strength: Optional[float] = None
    news_sentiment: Optional[float] = None


class ExitReason(str, Enum):
    STOP_LOSS = "stop_loss"
    TAKE_PROFIT = "take_profit"
    MAX_HOLDING = "max_holding"
    MANUAL = "manual"
    CIRCUIT_BREAKER = "circuit_breaker"


class LiveTrade(BaseModel):
    """Trade journal entry for live/paper trading."""
    id: Optional[int] = None
    account_id: int
    timestamp: datetime
    direction: SignalType
    symbol: str = "XAUUSD"
    entry_price: float = Field(gt=0)
    stop_loss: float = 0.0
    take_profit: float = 0.0
    lot_size: float = Field(gt=0)
    confidence: float = Field(ge=0.0, le=1.0)
    regime: str = "unknown"
    session: str = "unknown"
    d1_trend: str = "unknown"
    reason: str = ""
    trading_mode: TradingMode = TradingMode.SWING
    strategy_id: str = ""
    ticket: Optional[int] = None
    exit_price: Optional[float] = None
    exit_time: Optional[datetime] = None
    pnl: Optional[float] = None
    pnl_pct: Optional[float] = None
    exit_reason: Optional[str] = None