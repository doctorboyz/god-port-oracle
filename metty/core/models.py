"""Metty core models for multi-account execution and ML data collection."""

from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class AccountName(str, Enum):
    A = "A"
    B = "B"
    C = "C"


class SignalGroup(str, Enum):
    VOLUME = "volume"
    OB_OS = "ob_os"
    MA = "ma"
    SENTIMENT = "sentiment"


class OrderStatus(str, Enum):
    PENDING = "pending"
    FILLED = "filled"
    CANCELLED = "cancelled"
    REJECTED = "rejected"


class ExitReason(str, Enum):
    STOP_LOSS = "stop_loss"
    TAKE_PROFIT = "take_profit"
    MAX_HOLDING = "max_holding"
    MANUAL = "manual"
    CIRCUIT_BREAKER = "circuit_breaker"


class AccountConfig(BaseModel):
    """Configuration for a single MT5 demo account."""
    name: AccountName
    broker_login: str = ""
    broker_password: str = ""
    broker_server: str = "Exness-MT5"
    bridge_host: str = "localhost"
    bridge_port: int = 5005
    initial_balance: float = 100.0
    leverage: int = 2000
    signal_group: SignalGroup = SignalGroup.VOLUME
    is_active: bool = True


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


class OrderResult(BaseModel):
    """Result of an order sent to MT5."""
    success: bool
    ticket: Optional[int] = None
    price: Optional[float] = None
    volume: Optional[float] = None
    error: Optional[str] = None
    timestamp: datetime = Field(default_factory=datetime.now)


class AccountInfo(BaseModel):
    """Current account information from MT5."""
    balance: float
    equity: float
    margin: float
    free_margin: float
    leverage: int
    currency: str = "USD"
    name: AccountName