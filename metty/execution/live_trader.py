"""Live trader — generates signals from MT5 data and executes trades via the bridge.

Each cycle:
1. Fetch candles from MT5 bridge
2. Generate signal via Broky's weighted score system
3. Check risk (circuit breaker, calendar, existing position)
4. Calculate SL/TP/lots from ATR
5. Send order to MT5 (or log in dry-run mode)
6. Monitor open positions for exits
7. Log trade to SQLite
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

from broky.data.calendar import fetch_calendar, should_avoid_trading
from broky.indicators.atr import calculate_atr
from broky.risk.circuit_breaker import CircuitBreaker
from broky.risk.position_sizing import (
    calculate_position_size,
    calculate_stop_loss,
    calculate_take_profit,
)
from broky.risk.sizing import SIZING_METHODS, fixed_fraction_size, kelly_size, risk_per_trade_size, volatility_adjusted_size
from broky.signals.generator import generate_signal
from metty.core.db import (
    close_live_trade,
    get_latest_signal_id,
    get_open_trades,
    init_db,
    insert_live_trade,
    insert_rejected_signal,
)
from shared.events import Event, EventBus, EventType
from shared.logging_utils import log_trade, log_signal, log_position, log_circuit_break
from shared.models import Signal, SignalType, TradingMode

logger = logging.getLogger(__name__)

# Account IDs in the database
ACCOUNT_IDS = {"A": 1, "B": 2, "C": 3}

# Risk per trade by account (conservative for demo)
ACCOUNT_RISK = {"A": 0.01, "B": 0.02, "C": 0.02}

# Contract size: 1 lot XAUUSD = 100 oz
CONTRACT_SIZE = 100.0

# ML filter circuit breaker: stop trading after N consecutive ML failures
ML_MAX_CONSECUTIVE_FAILS = 5


@dataclass
class RiskConfig:
    risk_per_trade: float = 0.02
    atr_multiplier: float = 2.0
    risk_reward_ratio: float = 2.5
    min_confidence: float = 0.45
    max_holding_bars: int = 36  # 3 hours on M5
    cooldown_bars: int = 12  # 1 hour cooldown after exit
    spread_buffer: float = 2.0
    consecutive_loss_limit: int = 3
    daily_loss_limit_pct: float = 0.05
    bar_seconds: int = 300  # M5 = 300s, M1 = 60s
    sizing_method: str = "risk_per_trade"  # risk_per_trade, kelly, volatility_adjusted, fixed_fraction
    # Partial TP (Option C): close at TP1, open scale-in position
    partial_tp_enabled: bool = False  # Feature flag — must be explicitly enabled
    tp1_ratio: float = 0.5   # TP1 at 50% of TP distance from entry
    rr_scale_in: float = 2.5  # RR ratio for the scale-in position


class LiveTrader:
    """Live trading loop: fetch candles → generate signal → execute trade.

    Each account runs one position at a time. The trader checks:
    - Circuit breaker (consecutive losses, daily loss)
    - Calendar avoidance (high-impact news)
    - Existing positions (no double-entry)

    Usage:
        trader = LiveTrader(account="B", db_path="data/oracle.db")
        trader.run_once()  # Single cycle
        trader.run(interval=300, max_cycles=0)  # Continuous loop
    """

    def __init__(
        self,
        account: str = "B",
        db_path: Optional[Path] = None,
        data_dir: Optional[Path] = None,
        dry_run: bool = False,
        risk_config: Optional[RiskConfig] = None,
        event_bus: Optional[EventBus] = None,
        notifier: Optional["TelegramNotifier"] = None,
    ):
        self.account = account.upper()
        self.db_path = db_path
        self.data_dir = data_dir or Path("data/xau-data")
        self.dry_run = dry_run
        self.learning_mode = os.environ.get("LEARNING_MODE", "0") == "1"
        per_account_limits = {
            "A": int(os.environ.get("MAX_POSITIONS_A", os.environ.get("MAX_POSITIONS_PER_ACCOUNT", "5"))),
            "B": int(os.environ.get("MAX_POSITIONS_B", os.environ.get("MAX_POSITIONS_PER_ACCOUNT", "5"))),
            "C": int(os.environ.get("MAX_POSITIONS_C", os.environ.get("MAX_POSITIONS_PER_ACCOUNT", "5"))),
        }
        self.max_positions = per_account_limits.get(self.account, int(os.environ.get("MAX_POSITIONS_PER_ACCOUNT", "5")))
        self.account_id = ACCOUNT_IDS.get(self.account, 3)
        self.risk = risk_config or RiskConfig(
            risk_per_trade=ACCOUNT_RISK.get(self.account, 0.02),
        )
        # Per-account strategy overrides via env vars (for testing different configs)
        per_account_atr = {
            "A": float(os.environ.get("ATR_MULTIPLIER_A", os.environ.get("ATR_MULTIPLIER", "2.0"))),
            "B": float(os.environ.get("ATR_MULTIPLIER_B", os.environ.get("ATR_MULTIPLIER", "2.0"))),
            "C": float(os.environ.get("ATR_MULTIPLIER_C", os.environ.get("ATR_MULTIPLIER", "2.0"))),
        }
        per_account_rr = {
            "A": float(os.environ.get("RR_RATIO_A", os.environ.get("RR_RATIO", "2.5"))),
            "B": float(os.environ.get("RR_RATIO_B", os.environ.get("RR_RATIO", "2.5"))),
            "C": float(os.environ.get("RR_RATIO_C", os.environ.get("RR_RATIO", "2.5"))),
        }
        per_account_conf = {
            "A": float(os.environ.get("MIN_CONFIDENCE_A", os.environ.get("MIN_CONFIDENCE", "0.45"))),
            "B": float(os.environ.get("MIN_CONFIDENCE_B", os.environ.get("MIN_CONFIDENCE", "0.45"))),
            "C": float(os.environ.get("MIN_CONFIDENCE_C", os.environ.get("MIN_CONFIDENCE", "0.45"))),
        }
        if not risk_config:
            self.risk.atr_multiplier = per_account_atr.get(self.account, self.risk.atr_multiplier)
            self.risk.risk_reward_ratio = per_account_rr.get(self.account, self.risk.risk_reward_ratio)
            self.risk.min_confidence = per_account_conf.get(self.account, self.risk.min_confidence)
        # Partial TP overrides per account
        per_account_ptp = {
            "A": os.environ.get("PARTIAL_TP_ENABLED_A", os.environ.get("PARTIAL_TP_ENABLED", "0")) == "1",
            "B": os.environ.get("PARTIAL_TP_ENABLED_B", os.environ.get("PARTIAL_TP_ENABLED", "0")) == "1",
            "C": os.environ.get("PARTIAL_TP_ENABLED_C", os.environ.get("PARTIAL_TP_ENABLED", "0")) == "1",
        }
        per_account_tp1r = {
            "A": float(os.environ.get("TP1_RATIO_A", os.environ.get("TP1_RATIO", "0.5"))),
            "B": float(os.environ.get("TP1_RATIO_B", os.environ.get("TP1_RATIO", "0.5"))),
            "C": float(os.environ.get("TP1_RATIO_C", os.environ.get("TP1_RATIO", "0.5"))),
        }
        per_account_rrsi = {
            "A": float(os.environ.get("RR_SCALE_IN_A", os.environ.get("RR_SCALE_IN", "2.5"))),
            "B": float(os.environ.get("RR_SCALE_IN_B", os.environ.get("RR_SCALE_IN", "2.5"))),
            "C": float(os.environ.get("RR_SCALE_IN_C", os.environ.get("RR_SCALE_IN", "2.5"))),
        }
        self.risk.partial_tp_enabled = per_account_ptp.get(self.account, self.risk.partial_tp_enabled)
        self.risk.tp1_ratio = per_account_tp1r.get(self.account, self.risk.tp1_ratio)
        self.risk.rr_scale_in = per_account_rrsi.get(self.account, self.risk.rr_scale_in)
        # Override sizing method from env if set
        env_sizing = os.environ.get("POSITION_SIZING_METHOD", "").strip()
        if env_sizing and env_sizing in SIZING_METHODS:
            self.risk.sizing_method = env_sizing
        self._sizing_fn = SIZING_METHODS[self.risk.sizing_method]
        self.circuit_breaker = CircuitBreaker(
            consecutive_loss_limit=self.risk.consecutive_loss_limit,
            daily_loss_limit_pct=self.risk.daily_loss_limit_pct,
        )
        self._calendar_cache: list = []
        self._calendar_cache_time: float = 0
        self._sentiment_cache: dict = {}
        self._sentiment_cache_time: float = 0
        self._mfe_mae_state: dict[int, dict] = {}  # trade_id -> {mfe, mae, entry_price}
        self._last_exit_time: Optional[datetime] = None
        self._notifier = notifier
        self._last_d1_trend: Optional[str] = None
        self._last_h4_trend: Optional[str] = None
        self._cycle_count: int = 0
        self.strategy_id = f"swing-{self.account}"
        self.event_bus = event_bus
        # ML filter — risk-scale position size based on P(LOSS) prediction
        self._ml_enabled = os.environ.get("ML_FILTER_ENABLED", "0") == "1"
        self._ml_predictor = None
        self._ml_fail_count: int = 0  # consecutive ML prediction failures
        if self._ml_enabled:
            try:
                from broky.ml.trade_outcome_predictor import TradeOutcomePredictor
                self._ml_predictor = TradeOutcomePredictor(
                    loss_threshold=float(os.environ.get("ML_LOSS_THRESHOLD", "0.65")),
                )
                logger.info("[Swing:%s] ML filter enabled: %s", self.account,
                           "models loaded" if self._ml_predictor.enabled else "no models")
                # Health check: verify ML predictor can actually produce predictions
                if self._ml_predictor.enabled:
                    healthy, reason = self._ml_predictor.health_check()
                    if not healthy:
                        logger.critical("[Swing:%s] ML filter UNHEALTHY: %s — disabling", self.account, reason)
                        self._ml_enabled = False
                        self._ml_predictor = None
                    else:
                        logger.info("[Swing:%s] ML filter health check passed: %s", self.account, reason)
            except Exception as e:
                logger.warning("[Swing:%s] ML filter init failed: %s", self.account, e)
                self._ml_enabled = False

    def _get_calendar(self) -> list:
        now = time.time()
        if now - self._calendar_cache_time > 3600:
            try:
                self._calendar_cache = fetch_calendar(days_ahead=2, filter_currencies={"USD"})
            except Exception as e:
                logger.warning("Calendar fetch failed: %s", e)
                self._calendar_cache = []
            self._calendar_cache_time = now
        return self._calendar_cache

    def _get_sentiment(self) -> dict:
        """Fetch real sentiment data with 15-minute cache."""
        now = time.time()
        if now - self._sentiment_cache_time > 900:  # 15 min cache
            try:
                from metty.execution.live_collector import fetch_live_sentiment
                self._sentiment_cache = fetch_live_sentiment() or {}
            except Exception as e:
                logger.warning("[Swing:%s] Sentiment fetch failed: %s", self.account, e)
                self._sentiment_cache = {}
            self._sentiment_cache_time = now
        return self._sentiment_cache

    def _get_current_spread(self) -> float:
        """Get current spread from MT5 bridge. Returns 0.0 if unavailable."""
        try:
            from metty.bridge.client import MT5Bridge
            from metty.core.models import AccountConfig, AccountName
            account_configs = {
                "A": AccountConfig(
                    name=AccountName.A,
                    broker_login=os.environ.get("MT5_LOGIN_A", ""),
                    broker_server=os.environ.get("MT5_SERVER_A", "Exness-MT5Trial17"),
                    balance=100.0, leverage=2000,
                    bridge_host=os.environ.get("MT5_BRIDGE_A_HOST", "100.68.106.101"),
                    bridge_port=int(os.environ.get("MT5_BRIDGE_A_PORT", "5005")),
                    signal_group="volume",
                ),
                "B": AccountConfig(
                    name=AccountName.B,
                    broker_login=os.environ.get("MT5_LOGIN_B", ""),
                    broker_server=os.environ.get("MT5_SERVER_B", "Exness-MT5Trial17"),
                    balance=100.0, leverage=2000,
                    bridge_host=os.environ.get("MT5_BRIDGE_B_HOST", "100.68.106.102"),
                    bridge_port=int(os.environ.get("MT5_BRIDGE_B_PORT", "5005")),
                    signal_group="volume",
                ),
                "C": AccountConfig(
                    name=AccountName.C,
                    broker_login=os.environ.get("MT5_LOGIN_C", ""),
                    broker_server=os.environ.get("MT5_SERVER_C", "Exness-MT5Trial17"),
                    balance=100.0, leverage=2000,
                    bridge_host=os.environ.get("MT5_BRIDGE_C_HOST", "100.68.106.103"),
                    bridge_port=int(os.environ.get("MT5_BRIDGE_C_PORT", "5005")),
                    signal_group="volume",
                ),
            }
            if self.account in account_configs:
                bridge = MT5Bridge(account_configs[self.account])
                spread = bridge.get_spread_sync("XAUUSD")
                if spread is not None and spread >= 0:
                    return float(spread)
        except Exception as e:
            logger.debug("[Swing:%s] Spread fetch failed: %s", self.account, e)
        return 0.0

    def _get_calendar_context(self) -> tuple[int | None, str | None, str | None]:
        """Get minutes to next event, event type, and impact level."""
        calendar = self._get_calendar()
        if not calendar:
            return None, None, None
        try:
            now_utc = datetime.now(timezone.utc)
            for event in calendar:
                event_time = event.get("date")
                if not event_time:
                    continue
                if isinstance(event_time, str):
                    event_time = datetime.fromisoformat(event_time.replace("Z", "+00:00"))
                minutes_left = int((event_time - now_utc).total_seconds() / 60)
                if minutes_left > 0:
                    return minutes_left, event.get("title", ""), event.get("impact", "")
            return None, None, None
        except Exception:
            return None, None, None

    def _record_rejection(self, signal: Signal, reason: str, session: str = "",
                          d1_trend: str = "", candles: dict | None = None) -> None:
        """Record a rejected signal for survivorship bias analysis."""
        try:
            import json
            ts_str = signal.timestamp.isoformat() if hasattr(signal.timestamp, 'isoformat') else str(signal.timestamp)
            indicator_json = json.dumps(signal.indicators) if signal.indicators else None
            insert_rejected_signal(
                account_id=self.account_id,
                timestamp=ts_str,
                direction=signal.signal_type.value,
                confidence=signal.confidence,
                price=signal.price,
                rejection_reason=reason,
                trading_mode=TradingMode.SWING.value,
                strategy_id=self.strategy_id,
                regime=signal.regime,
                session=session,
                d1_trend=d1_trend,
                signal_json=indicator_json,
                db_path=self.db_path,
            )
        except Exception as e:
            logger.warning("[Swing:%s] Failed to record rejection: %s", self.account, e)

    def _fetch_candles(self) -> Optional[dict[str, pd.DataFrame]]:
        """Fetch candle data from MT5 bridge, fall back to CSV."""
        try:
            from metty.bridge.client import MT5Bridge
            from metty.core.models import AccountConfig, AccountName
            from broky.data.resampler import resample_timeframe
            from metty.execution.historical_collector import _normalize_columns

            account_configs = {
                "A": AccountConfig(
                    name=AccountName.A,
                    broker_login=os.environ.get("MT5_LOGIN_A", ""),
                    broker_server=os.environ.get("MT5_SERVER_A", "Exness-MT5Trial17"),
                    balance=100.0, leverage=2000,
                    bridge_host=os.environ.get("MT5_BRIDGE_A_HOST", "100.68.106.101"),
                    bridge_port=int(os.environ.get("MT5_BRIDGE_A_PORT", "5005")),
                    signal_group="volume",
                ),
                "B": AccountConfig(
                    name=AccountName.B,
                    broker_login=os.environ.get("MT5_LOGIN_B", ""),
                    broker_server=os.environ.get("MT5_SERVER_B", "Exness-MT5Trial17"),
                    balance=500.0, leverage=500,
                    bridge_host=os.environ.get("MT5_BRIDGE_B_HOST", "100.68.106.101"),
                    bridge_port=int(os.environ.get("MT5_BRIDGE_B_PORT", "5006")),
                    signal_group="ob_os",
                ),
                "C": AccountConfig(
                    name=AccountName.C,
                    broker_login=os.environ.get("MT5_LOGIN_C", ""),
                    broker_server=os.environ.get("MT5_SERVER_C", "Exness-MT5Trial7"),
                    balance=1000.0, leverage=500,
                    bridge_host=os.environ.get("MT5_BRIDGE_C_HOST", "100.68.106.101"),
                    bridge_port=int(os.environ.get("MT5_BRIDGE_C_PORT", "5007")),
                    signal_group="ma",
                ),
            }

            config = account_configs.get(self.account)
            if not config:
                logger.warning("Unknown account: %s", self.account)
                return None

            bridge = MT5Bridge(config)
            candles = {}

            for tf in ["M5", "H1", "H4", "D1"]:
                df = bridge.fetch_candles_sync("XAUUSD", tf, 500)
                if not df.empty:
                    candles[tf] = _normalize_columns(df)

            if not candles:
                return self._fetch_candles_csv()

            # Resample M5 for higher TFs if missing
            if "M5" in candles:
                m5 = candles["M5"]
                for tf in ["H1", "H4", "D1"]:
                    if tf not in candles:
                        try:
                            candles[tf] = _normalize_columns(
                                resample_timeframe(m5.reset_index(), tf)
                            )
                        except Exception:
                            pass

            return candles

        except Exception as e:
            logger.warning("MT5 bridge fetch failed: %s", e)
            return self._fetch_candles_csv()

    def _fetch_candles_csv(self) -> Optional[dict[str, pd.DataFrame]]:
        """Fallback: load candles from CSV."""
        try:
            from broky.data.loader import load_timeframe
            from broky.data.resampler import resample_timeframe
            from metty.execution.historical_collector import _normalize_columns, WINDOW_SIZE

            m5_raw = load_timeframe(self.data_dir, "M5").tail(WINDOW_SIZE)
            if m5_raw.empty:
                return None

            candles = {
                "M5": _normalize_columns(m5_raw),
                "H1": _normalize_columns(resample_timeframe(m5_raw, "H1")),
                "H4": _normalize_columns(resample_timeframe(m5_raw, "H4")),
                "D1": _normalize_columns(resample_timeframe(m5_raw, "D1")),
            }
            return candles
        except Exception as e:
            logger.error("CSV fallback failed: %s", e)
            return None

    def _generate_signal(self, candles: dict[str, pd.DataFrame]) -> Optional[Signal]:
        """Generate a trading signal from candle data."""
        m5 = candles.get("M5")
        if m5 is None or len(m5) < 50:
            return None

        # Determine D1 trend from EMA 50/200
        d1 = candles.get("D1")
        d1_trend = self._determine_d1_trend(d1)
        d1_trend_strength = self._compute_d1_trend_strength(d1)
        price_momentum_24h = self._compute_price_momentum_24h(d1)

        # Determine H4 trend from EMA 10/50 (faster override for D1)
        h4 = candles.get("H4")
        h4_trend = self._compute_h4_trend(h4)

        # Detect trend flips and send Telegram alert
        self._check_trend_flips(d1_trend, h4_trend)

        try:
            signal = generate_signal(
                close=m5["close"],
                high=m5["high"],
                low=m5["low"],
                volume=m5["volume"],
                current_price=float(m5["close"].iloc[-1]),
                timestamp=m5.index[-1].to_pydatetime().replace(tzinfo=timezone.utc)
                if hasattr(m5.index[-1], "to_pydatetime")
                else datetime.now(timezone.utc),
                d1_trend=d1_trend,
                h4_trend=h4_trend,
                d1_trend_strength=d1_trend_strength,
                price_momentum_24h=price_momentum_24h,
                min_confidence=self.risk.min_confidence,
                learning_mode=self.learning_mode,
            )
            return signal
        except Exception as e:
            logger.error("Signal generation failed: %s", e)
            return None

    def _determine_d1_trend(self, d1: Optional[pd.DataFrame]) -> str:
        if d1 is None or len(d1) < 200:
            return "unknown"
        close = d1["close"]
        ema50 = close.ewm(span=50, adjust=False).mean()
        ema200 = close.ewm(span=200, adjust=False).mean()
        if pd.isna(ema200.iloc[-1]):
            return "unknown"
        return "bullish" if ema50.iloc[-1] > ema200.iloc[-1] else "bearish"

    def _compute_h4_trend(self, h4: Optional[pd.DataFrame]) -> Optional[str]:
        """Compute H4 trend using EMA 10/50 crossover (faster than D1 EMA 50/200)."""
        if h4 is None or len(h4) < 50:
            return None
        try:
            ema10 = h4["close"].ewm(span=10, adjust=False).mean()
            ema50 = h4["close"].ewm(span=50, adjust=False).mean()
            if pd.isna(ema10.iloc[-1]) or pd.isna(ema50.iloc[-1]):
                return None
            return "bullish" if ema10.iloc[-1] > ema50.iloc[-1] else "bearish"
        except Exception:
            return None

    def _check_trend_flips(self, d1_trend: str, h4_trend: Optional[str]) -> None:
        """Detect D1/H4 trend changes and send Telegram alert."""
        if self._notifier is None:
            return
        if not self._notifier.enabled:
            return

        now = datetime.now(timezone.utc).strftime("%H:%M")
        alerts = []

        if self._last_d1_trend is not None and d1_trend != "unknown":
            if d1_trend != self._last_d1_trend:
                direction = "🟢 BULLISH" if d1_trend == "bullish" else "🔴 BEARISH"
                alerts.append(
                    f"<b>D1 Trend Flip</b> {now}\n"
                    f"Account {self.account}: {self._last_d1_trend} → {direction}"
                )

        if self._last_h4_trend is not None and h4_trend and h4_trend != "unknown":
            if h4_trend != self._last_h4_trend:
                direction = "🟢 BULLISH" if h4_trend == "bullish" else "🔴 BEARISH"
                alerts.append(
                    f"<b>H4 Trend Flip</b> {now}\n"
                    f"Account {self.account}: {self._last_h4_trend} → {direction}"
                )

        # Update tracking
        if d1_trend != "unknown":
            self._last_d1_trend = d1_trend
        if h4_trend and h4_trend != "unknown":
            self._last_h4_trend = h4_trend

        # Send alerts (deduplicate across accounts via notifier's rate limit)
        for msg in alerts:
            try:
                self._notifier.send(msg)
            except Exception:
                pass

    def _compute_d1_trend_strength(self, d1: Optional[pd.DataFrame]) -> Optional[float]:
        """Compute normalized D1 trend strength from EMA 50/200 spread.

        Returns 0.0 (flat) to ~1.0 (very strong trend).
        EMA50-EMA200 spread divided by price gives a normalized measure.
        """
        if d1 is None or len(d1) < 200:
            return None
        try:
            close = d1["close"]
            ema50 = close.ewm(span=50, adjust=False).mean()
            ema200 = close.ewm(span=200, adjust=False).mean()
            if pd.isna(ema200.iloc[-1]) or pd.isna(ema50.iloc[-1]):
                return None
            spread = abs(ema50.iloc[-1] - ema200.iloc[-1])
            price = close.iloc[-1]
            if price <= 0:
                return None
            # Normalize: 0.5% spread = moderate, 2%+ = very strong
            strength = spread / price / 0.02
            return float(max(0.0, min(1.0, strength)))
        except Exception:
            return None

    def _compute_price_momentum_24h(self, d1: Optional[pd.DataFrame]) -> Optional[float]:
        """Compute 24h price momentum as ratio of change.

        Returns negative for falling price, positive for rising.
        e.g. -0.015 = price dropped 1.5% in ~24h.
        Uses last 2 D1 candles as proxy for 24-48h window.
        """
        if d1 is None or len(d1) < 2:
            return None
        try:
            close = d1["close"]
            prev = close.iloc[-2]
            curr = close.iloc[-1]
            if pd.isna(prev) or pd.isna(curr) or prev <= 0:
                return None
            return float((curr - prev) / prev)
        except Exception:
            return None

    def _classify_session(self, timestamp: datetime) -> str:
        hour = timestamp.hour
        if 13 <= hour < 16:
            return "overlap"
        if 8 <= hour < 16:
            return "london"
        if 13 <= hour < 22:
            return "ny"
        return "asian"

    def _get_equity(self) -> float:
        """Get current account equity from MT5."""
        try:
            from metty.bridge.client import MT5Bridge
            from metty.core.models import AccountConfig, AccountName

            configs = {
                "A": AccountConfig(name=AccountName.A, bridge_host=os.environ.get("MT5_BRIDGE_A_HOST", "100.68.106.101"), bridge_port=int(os.environ.get("MT5_BRIDGE_A_PORT", "5005")), broker_login="", broker_server=""),
                "B": AccountConfig(name=AccountName.B, bridge_host=os.environ.get("MT5_BRIDGE_B_HOST", "100.68.106.101"), bridge_port=int(os.environ.get("MT5_BRIDGE_B_PORT", "5006")), broker_login="", broker_server=""),
                "C": AccountConfig(name=AccountName.C, bridge_host=os.environ.get("MT5_BRIDGE_C_HOST", "100.68.106.101"), bridge_port=int(os.environ.get("MT5_BRIDGE_C_PORT", "5007")), broker_login="", broker_server=""),
            }
            info = MT5Bridge(configs[self.account]).fetch_account_info_sync()
            return info.equity if info else 500.0
        except Exception:
            return 500.0

    def _check_existing_position(self) -> bool:
        """Check if there's an open position or trade for this account."""
        # Check DB for open trades
        open_trades = get_open_trades(self.account_id, self.db_path)
        if open_trades:
            return True

        # Check MT5 for open positions
        try:
            import rpyc
            port_map = {"A": 5005, "B": 5006, "C": 5007}
            host = os.environ.get(f"MT5_BRIDGE_{self.account}_HOST", "100.68.106.101")
            port = int(os.environ.get(f"MT5_BRIDGE_{self.account}_PORT", str(port_map[self.account])))

            conn = rpyc.connect(host, port, config={"sync_request_timeout": 10})
            positions = conn.root.positions_get(symbol="XAUUSD")
            conn.close()
            return positions is not None and len(positions) > 0
        except Exception:
            return False

    def _check_cooldown(self) -> bool:
        """Check if we're still in cooldown after last exit."""
        if self._last_exit_time is None:
            return False
        elapsed = (datetime.now(timezone.utc) - self._last_exit_time).total_seconds()
        cooldown_seconds = self.risk.cooldown_bars * self.risk.bar_seconds
        return elapsed < cooldown_seconds

    def _calculate_lots(self, equity: float, price: float, sl: float, atr: float) -> float:
        """Calculate position size using the configured sizing method."""
        if self.risk.sizing_method == "risk_per_trade":
            return risk_per_trade_size(
                equity, self.risk.risk_per_trade, price, sl, CONTRACT_SIZE,
            )
        elif self.risk.sizing_method == "kelly":
            # Use last 50 closed trades for Kelly estimation
            from metty.core.db import get_closed_trades
            closed = get_closed_trades(self.account_id, self.db_path, limit=50)
            if len(closed) < 10:
                return risk_per_trade_size(
                    equity, self.risk.risk_per_trade, price, sl, CONTRACT_SIZE,
                )
            wins = [t for t in closed if t.get("pnl", 0) > 0]
            losses = [t for t in closed if t.get("pnl", 0) <= 0]
            win_rate = len(wins) / len(closed) if closed else 0.5
            avg_win = sum(t["pnl"] for t in wins) / len(wins) if wins else 1.0
            avg_loss = abs(sum(t["pnl"] for t in losses) / len(losses)) if losses else 1.0
            return kelly_size(
                equity, win_rate, avg_win, avg_loss, price, sl, CONTRACT_SIZE,
            )
        elif self.risk.sizing_method == "volatility_adjusted":
            return volatility_adjusted_size(
                equity, self.risk.risk_per_trade, price, sl, atr, CONTRACT_SIZE,
            )
        elif self.risk.sizing_method == "fixed_fraction":
            return fixed_fraction_size(0.01)
        else:
            return risk_per_trade_size(
                equity, self.risk.risk_per_trade, price, sl, CONTRACT_SIZE,
            )

    def _monitor_positions(self, candles: dict[str, pd.DataFrame]) -> list[dict]:
        """Check open trades for exit conditions (SL/TP hit, max holding)."""
        open_trades = get_open_trades(self.account_id, self.db_path)
        closed = []

        if not open_trades or "M5" not in candles:
            return closed

        m5 = candles["M5"]
        current_price = float(m5["close"].iloc[-1])
        now_str = datetime.now(timezone.utc).isoformat()

        for trade in open_trades:
            direction = trade["direction"]
            entry_price = trade["entry_price"]
            sl = trade["stop_loss"]
            tp = trade["take_profit"]
            lot_size = trade["lot_size"]
            trade_id = trade["id"]

            # Update MFE/MAE tracking
            m5_high = float(m5["high"].iloc[-1])
            m5_low = float(m5["low"].iloc[-1])
            mfe_mae = self._mfe_mae_state.get(trade_id, {"mfe": 0, "mae": 0, "entry_price": entry_price})
            if direction == "BUY":
                favorable = m5_high - entry_price
                adverse = entry_price - m5_low
            else:
                favorable = entry_price - m5_low
                adverse = m5_high - entry_price
            mfe_mae["mfe"] = max(mfe_mae["mfe"], favorable)
            mfe_mae["mae"] = max(mfe_mae["mae"], adverse)
            self._mfe_mae_state[trade_id] = mfe_mae

            # === Partial TP (Option C): detect TP1 hit ===
            tp_level = trade.get("tp_level", 1) or 1
            tp1_price = trade.get("tp1_price")
            if (
                self.risk.partial_tp_enabled
                and tp_level == 1
                and tp1_price
                and tp1_price > 0
            ):
                tp1_hit = False
                if direction == "BUY" and current_price >= tp1_price:
                    tp1_hit = True
                elif direction == "SELL" and current_price <= tp1_price:
                    tp1_hit = True

                if tp1_hit:
                    closed.extend(self._execute_tp1_close(trade, tp1_price, mfe_mae, now_str))
                    continue  # Trade closed + scale-in opened, skip normal exit

            exit_reason = None
            exit_price = current_price

            # Check SL/TP
            if direction == "BUY":
                if sl > 0 and current_price <= sl:
                    exit_reason = "stop_loss"
                    exit_price = sl
                elif tp > 0 and current_price >= tp:
                    exit_reason = "take_profit"
                    exit_price = tp
            elif direction == "SELL":
                if sl > 0 and current_price >= sl:
                    exit_reason = "stop_loss"
                    exit_price = sl
                elif tp > 0 and current_price <= tp:
                    exit_reason = "take_profit"
                    exit_price = tp

            # Check max holding time
            if exit_reason is None:
                entry_time = pd.Timestamp(trade["timestamp"])
                bars_held = 0
                try:
                    if entry_time in m5.index:
                        bars_held = len(m5[m5.index > entry_time])
                    else:
                        bars_held = len(m5[m5.index > entry_time.tz_localize(None)])
                except Exception:
                    pass

                if self.risk.max_holding_bars > 0 and bars_held >= self.risk.max_holding_bars:
                    exit_reason = "max_holding"

            if exit_reason:
                # Calculate PnL
                if direction == "BUY":
                    pnl = (exit_price - entry_price) * lot_size * CONTRACT_SIZE
                    pnl_pct = (exit_price - entry_price) / entry_price * 100
                else:
                    pnl = (entry_price - exit_price) * lot_size * CONTRACT_SIZE
                    pnl_pct = (entry_price - exit_price) / entry_price * 100

                # Close in DB with MFE/MAE data
                mfe_mae = self._mfe_mae_state.pop(trade_id, {"mfe": 0, "mae": 0, "entry_price": entry_price})
                mfe = mfe_mae.get("mfe", 0)
                mae = mfe_mae.get("mae", 0)
                mfe_pct = round(mfe / entry_price * 100, 4) if entry_price > 0 else 0
                mae_pct = round(mae / entry_price * 100, 4) if entry_price > 0 else 0

                # Get exit context (regime/trend at exit time)
                exit_d1_trend = self._last_d1_trend
                exit_h4_trend = self._last_h4_trend
                exit_regime = exit_d1_trend if exit_d1_trend and exit_d1_trend not in ("neutral", "unknown") else None

                close_live_trade(
                    trade_id=trade["id"],
                    exit_price=exit_price,
                    exit_time=now_str,
                    pnl=round(pnl, 2),
                    pnl_pct=round(pnl_pct, 4),
                    exit_reason=exit_reason,
                    mfe=round(mfe, 2),
                    mae=round(mae, 2),
                    mfe_pct=mfe_pct,
                    mae_pct=mae_pct,
                    exit_regime=exit_regime,
                    exit_d1_trend=exit_d1_trend,
                    exit_h4_trend=exit_h4_trend,
                    tp1_price=trade.get("tp1_price"),
                    db_path=self.db_path,
                )

                # Update circuit breaker
                if pnl > 0:
                    self.circuit_breaker.record_win(pnl)
                else:
                    self.circuit_breaker.record_loss(pnl)

                self._last_exit_time = datetime.now(timezone.utc)

                # Close in MT5 if ticket exists
                if trade.get("ticket") and not self.dry_run:
                    try:
                        from metty.bridge.client import MT5Bridge
                        from metty.core.models import AccountConfig, AccountName

                        port_map = {"A": 5005, "B": 5006, "C": 5007}
                        host = os.environ.get(f"MT5_BRIDGE_{self.account}_HOST", "100.68.106.101")
                        port = int(os.environ.get(f"MT5_BRIDGE_{self.account}_PORT", str(port_map[self.account])))

                        config = AccountConfig(
                            name=AccountName[self.account],
                            bridge_host=host,
                            bridge_port=port,
                            broker_login="", broker_server="",
                        )
                        bridge = MT5Bridge(config)

                        async def _close():
                            if await bridge.connect():
                                await bridge.close_position(trade["ticket"])
                                await bridge.disconnect()

                        import asyncio
                        asyncio.run(_close())
                    except Exception as e:
                        logger.warning("Failed to close position %s in MT5: %s", trade["ticket"], e)

                closed.append({
                    "trade_id": trade["id"],
                    "direction": direction,
                    "exit_reason": exit_reason,
                    "exit_price": exit_price,
                    "pnl": round(pnl, 2),
                    "pnl_pct": round(pnl_pct, 4),
                })

                log_trade(logger, "CLOSED", account=self.account, direction=direction,
                         price=exit_price, pnl=pnl, ticket=str(trade.get("ticket", "")),
                         reason=exit_reason)

                if self.event_bus:
                    self.event_bus.publish(Event(
                        type=EventType.TRADE_CLOSED,
                        data={
                            "direction": direction, "symbol": trade.get("symbol", "XAUUSD"),
                            "entry_price": entry_price, "exit_price": exit_price,
                            "pnl": round(pnl, 2), "pnl_pct": round(pnl_pct, 4),
                            "exit_reason": exit_reason, "account": self.account,
                            "trading_mode": trade.get("trading_mode", "swing"),
                        },
                    ))

        return closed

    def _execute_tp1_close(
        self,
        trade: dict,
        tp1_price: float,
        mfe_mae: dict,
        now_str: str,
    ) -> list[dict]:
        """Close position 1 at TP1, open scale-in position 2 (Option C).

        Flow:
        1. Close position 1 at TP1 price → take profit
        2. Open position 2 at current price with new SL based on rr_scale_in
        3. Position 2's TP = original final TP
        """
        closed = []
        direction = trade["direction"]
        entry_price = trade["entry_price"]
        sl = trade["stop_loss"]
        tp = trade["take_profit"]
        lot_size = trade["lot_size"]
        trade_id = trade["id"]

        # 1. Close position 1 at TP1
        exit_price = tp1_price
        if direction == "BUY":
            pnl = (exit_price - entry_price) * lot_size * CONTRACT_SIZE
            pnl_pct = (exit_price - entry_price) / entry_price * 100
        else:
            pnl = (entry_price - exit_price) * lot_size * CONTRACT_SIZE
            pnl_pct = (entry_price - exit_price) / entry_price * 100

        mfe = mfe_mae.get("mfe", 0)
        mae = mfe_mae.get("mae", 0)
        mfe_pct = round(mfe / entry_price * 100, 4) if entry_price > 0 else 0
        mae_pct = round(mae / entry_price * 100, 4) if entry_price > 0 else 0

        exit_d1_trend = self._last_d1_trend
        exit_h4_trend = self._last_h4_trend
        exit_regime = exit_d1_trend if exit_d1_trend and exit_d1_trend not in ("neutral", "unknown") else None

        close_live_trade(
            trade_id=trade["id"],
            exit_price=exit_price,
            exit_time=now_str,
            pnl=round(pnl, 2),
            pnl_pct=round(pnl_pct, 4),
            exit_reason="tp1_hit",
            mfe=round(mfe, 2),
            mae=round(mae, 2),
            mfe_pct=mfe_pct,
            mae_pct=mae_pct,
            exit_regime=exit_regime,
            exit_d1_trend=exit_d1_trend,
            exit_h4_trend=exit_h4_trend,
            tp1_price=tp1_price,
            tp_level=1,
            remaining_lots=0,
            db_path=self.db_path,
        )

        # Update circuit breaker for position 1 profit
        if pnl > 0:
            self.circuit_breaker.record_win(pnl)
        else:
            self.circuit_breaker.record_loss(pnl)

        self._last_exit_time = datetime.now(timezone.utc)

        # Clean up MFE/MAE state for position 1
        self._mfe_mae_state.pop(trade_id, None)

        # Close in MT5 if ticket exists
        if trade.get("ticket") and not self.dry_run:
            try:
                from metty.bridge.client import MT5Bridge
                from metty.core.models import AccountConfig, AccountName

                port_map = {"A": 5005, "B": 5006, "C": 5007}
                host = os.environ.get(f"MT5_BRIDGE_{self.account}_HOST", "100.68.106.101")
                port = int(os.environ.get(f"MT5_BRIDGE_{self.account}_PORT", str(port_map[self.account])))

                config = AccountConfig(
                    name=AccountName[self.account],
                    bridge_host=host,
                    bridge_port=port,
                    broker_login="", broker_server="",
                )
                bridge = MT5Bridge(config)

                async def _close():
                    if await bridge.connect():
                        await bridge.close_position(trade["ticket"])
                        await bridge.disconnect()

                import asyncio
                asyncio.run(_close())
            except Exception as e:
                logger.warning("[Swing:%s] Failed to close position %s at TP1 in MT5: %s",
                               self.account, trade["ticket"], e)

        closed.append({
            "trade_id": trade["id"],
            "direction": direction,
            "exit_reason": "tp1_hit",
            "exit_price": exit_price,
            "pnl": round(pnl, 2),
            "pnl_pct": round(pnl_pct, 4),
        })

        log_trade(logger, "TP1_CLOSED", account=self.account, direction=direction,
                  price=exit_price, pnl=pnl, ticket=str(trade.get("ticket", "")),
                  reason="tp1_hit")

        if self.event_bus:
            self.event_bus.publish(Event(
                type=EventType.TRADE_CLOSED,
                data={
                    "direction": direction, "symbol": trade.get("symbol", "XAUUSD"),
                    "entry_price": entry_price, "exit_price": exit_price,
                    "pnl": round(pnl, 2), "pnl_pct": round(pnl_pct, 4),
                    "exit_reason": "tp1_hit", "account": self.account,
                    "trading_mode": trade.get("trading_mode", "swing"),
                },
            ))

        # 2. Open scale-in position (position 2) at current price
        # SL = tp1_price ± (remaining_distance / rr_scale_in)
        # TP = original final TP
        remaining_distance = abs(tp - tp1_price)
        if direction == "BUY":
            new_sl = tp1_price - remaining_distance / self.risk.rr_scale_in
        else:
            new_sl = tp1_price + remaining_distance / self.risk.rr_scale_in

        # Use same lot size (Exness minimum = 0.01)
        new_lots = lot_size
        current_price = tp1_price  # Approximate entry for scale-in

        # Open scale-in in MT5 first (if not dry run)
        ticket = None
        if not self.dry_run:
            try:
                from metty.bridge.client import MT5Bridge
                from metty.core.models import AccountConfig, AccountName

                port_map = {"A": 5005, "B": 5006, "C": 5007}
                host = os.environ.get(f"MT5_BRIDGE_{self.account}_HOST", "100.68.106.101")
                port = int(os.environ.get(f"MT5_BRIDGE_{self.account}_PORT", str(port_map[self.account])))

                config = AccountConfig(
                    name=AccountName[self.account],
                    bridge_host=host,
                    bridge_port=port,
                    broker_login=os.environ.get(f"MT5_LOGIN_{self.account}", ""),
                    broker_server=os.environ.get(f"MT5_SERVER_{self.account}", "Exness-MT5Trial17"),
                )
                bridge = MT5Bridge(config)

                async def _open():
                    if not await bridge.connect():
                        return None
                    result = await bridge.send_order("XAUUSD", direction, new_lots, new_sl, tp)
                    await bridge.disconnect()
                    return result

                import asyncio
                order_result = asyncio.run(_open())
                ticket = order_result.ticket if order_result and order_result.success else None
                if order_result and not order_result.success:
                    logger.error("[Swing:%s] Scale-in order FAILED at TP1: %s",
                                 self.account, order_result.error)
                    return closed  # Don't insert scale-in if MT5 order failed
            except Exception as e:
                logger.error("[Swing:%s] Scale-in MT5 error at TP1: %s", self.account, e)
                return closed  # Don't insert scale-in if MT5 errored

        # Insert scale-in trade in DB
        scale_in_id = insert_live_trade(
            account_id=self.account_id,
            timestamp=now_str,
            direction=direction,
            entry_price=current_price,
            stop_loss=round(new_sl, 2),
            take_profit=tp,
            lot_size=new_lots,
            confidence=trade.get("confidence", 0),
            regime=trade.get("regime", "unknown") or "unknown",
            session=trade.get("session", "unknown") or "unknown",
            d1_trend=exit_d1_trend or "unknown",
            reason=f"scale-in from trade #{trade_id} (tp1_hit)",
            ticket=ticket,
            symbol=trade.get("symbol", "XAUUSD"),
            trading_mode=trade.get("trading_mode", "swing"),
            strategy_id=self.strategy_id,
            tp1_price=tp1_price,
            tp_level=2,
            parent_trade_id=trade_id,
            atr_multiplier=self.risk.atr_multiplier,
            rr_ratio=self.risk.risk_reward_ratio,
            min_confidence_threshold=self.risk.min_confidence,
            db_path=self.db_path,
        )

        # Initialize MFE/MAE tracking for scale-in position
        self._mfe_mae_state[scale_in_id] = {
            "mfe": 0, "mae": 0, "entry_price": current_price,
        }

        log_trade(logger, "SCALE_IN", account=self.account, direction=direction,
                  price=current_price, lots=new_lots, sl=new_sl, tp=tp,
                  reason=f"scale-in from #{trade_id}")

        if self.event_bus:
            self.event_bus.publish(Event(
                type=EventType.TRADE_OPENED,
                data={
                    "direction": direction, "symbol": "XAUUSD",
                    "price": current_price, "sl": new_sl, "tp": tp,
                    "lots": new_lots, "confidence": 0,
                    "regime": exit_regime or "unknown", "reason": "scale-in",
                    "account": self.account, "trading_mode": "swing",
                    "parent_trade_id": trade_id, "tp_level": 2,
                },
            ))

        return closed

    def run_once(self) -> dict:
        """Run a single trading cycle.

        Returns dict with: action, signal, trade details, etc.
        """
        init_db(self.db_path)
        self._cycle_count += 1

        # 1. Fetch candles
        candles = self._fetch_candles()
        if not candles or "M5" not in candles:
            return {"action": "skip", "reason": "no candle data"}

        m5 = candles["M5"]

        # 2. Monitor existing positions first
        closed = self._monitor_positions(candles)

        # 3. Generate signal
        signal = self._generate_signal(candles)
        if signal is None:
            return {"action": "skip", "reason": "signal generation failed"}

        price = signal.price
        session = self._classify_session(
            m5.index[-1].to_pydatetime().replace(tzinfo=timezone.utc)
            if hasattr(m5.index[-1], "to_pydatetime")
            else datetime.now(timezone.utc)
        )
        d1_trend = self._determine_d1_trend(candles.get("D1"))
        h4_trend = self._compute_h4_trend(candles.get("H4"))

        # 4. Risk checks
        if signal.signal_type == SignalType.HOLD:
            return {
                "action": "hold",
                "reason": f"no signal (conf={signal.confidence:.2f})",
                "signal": signal,
            }

        # 4b. Position limit check (always enforced, even in learning mode)
        open_trades = get_open_trades(self.account_id, self.db_path)
        if len(open_trades) >= self.max_positions:
            log_position(logger, "LIMIT", account=self.account, count=len(open_trades), max=self.max_positions)
            self._record_rejection(signal, "position_limit", session, d1_trend, candles)
            return {
                "action": "hold",
                "reason": f"position limit ({len(open_trades)}/{self.max_positions})",
                "signal": signal,
            }

        # 4c. Existing position check (always enforced — prevents churn)
        if self._check_existing_position():
            self._record_rejection(signal, "existing_position", session, d1_trend, candles)
            return {
                "action": "hold",
                "reason": "position already open",
                "signal": signal,
            }

        # 4d. Learning mode: bypass remaining risk checks for data collection
        if not self.learning_mode:
            can_trade, cb_reason = self.circuit_breaker.can_open_trade()
            if not can_trade:
                log_circuit_break(logger, "BLOCKED", account=self.account, reason=cb_reason)
                self._record_rejection(signal, f"circuit_breaker:{cb_reason}", session, d1_trend, candles)
                if self.event_bus:
                    self.event_bus.publish(Event(
                        type=EventType.CIRCUIT_BREAKER_TRIGGERED,
                        data={
                            "account": self.account, "reason": cb_reason,
                            "consecutive_losses": self.circuit_breaker.state.consecutive_losses,
                            "daily_loss_pct": self.circuit_breaker.state.daily_loss_pct,
                        },
                    ))
                return {
                    "action": "hold",
                    "reason": f"circuit breaker: {cb_reason}",
                    "signal": signal,
                }

            if self._check_cooldown():
                self._record_rejection(signal, "cooldown", session, d1_trend, candles)
                return {
                    "action": "hold",
                    "reason": "cooldown after last exit",
                    "signal": signal,
                }

            calendar = self._get_calendar()
            if should_avoid_trading(calendar):
                self._record_rejection(signal, "calendar_avoid", session, d1_trend, candles)
                return {
                    "action": "hold",
                    "reason": "high-impact news nearby",
                    "signal": signal,
                }

        # 5. ML filter — risk-scale position size based on P(LOSS) prediction
        ml_risk_multiplier = 1.0
        ml_loss_proba = None
        ml_model_used = None
        ml_risk_reason = None
        _live_spread = self._get_current_spread()

        # Circuit breaker: if ML filter has failed too many times, stop trading
        if self._ml_enabled and self._ml_fail_count >= ML_MAX_CONSECUTIVE_FAILS:
            logger.critical(
                "[Swing:%s] ML filter failed %d times consecutively — circuit breaker: holding",
                self.account, self._ml_fail_count,
            )
            self._record_rejection(signal, f"ml_filter_circuit_break:{self._ml_fail_count}_fails", session, d1_trend, candles)
            return {"action": "hold", "reason": f"ML circuit breaker ({self._ml_fail_count} consecutive failures)", "signal": signal}

        if self._ml_enabled and self._ml_predictor is not None:
            try:
                from broky.ml.trade_outcome_predictor import compute_features_from_candles

                sentiment_data = self._get_sentiment()
                ml_features = compute_features_from_candles(
                    candles, str(signal.signal_type.value),
                    spread=_live_spread,
                    d1_trend=d1_trend or "neutral",
                    h4_trend=h4_trend or "unknown",
                    session=session,
                    sentiment=sentiment_data,
                )
                regime = d1_trend if d1_trend and d1_trend not in ("neutral", "unknown") else "trending"
                ml_risk_multiplier, ml_reason, ml_loss_proba, ml_model_used = self._ml_predictor.get_risk_multiplier(
                    ml_features, regime, str(signal.signal_type.value),
                )
                # ML filter succeeded — reset failure counter
                self._ml_fail_count = 0

                if ml_risk_multiplier == 0:
                    if self.learning_mode:
                        # In learning mode, never block — allow trade with min lot
                        # to collect diverse outcomes for ML retraining
                        logger.info("[Swing:%s] ML would block, but learning mode: allowing min-lot trade (%s)", self.account, ml_reason)
                        ml_risk_multiplier = 0.01  # minimal position for data collection
                    else:
                        logger.info("[Swing:%s] ML filter blocked trade: %s", self.account, ml_reason)
                        self._record_rejection(signal, f"ml_filter:{ml_reason}", session, d1_trend, candles)
                        return {"action": "hold", "reason": ml_reason, "signal": signal}
                elif ml_risk_multiplier < 1.0:
                    logger.info("[Swing:%s] ML risk-scaling: %s", self.account, ml_reason)
                else:
                    logger.info("[Swing:%s] ML filter pass: %s", self.account, ml_reason)

            except Exception as e:
                self._ml_fail_count += 1
                logger.error(
                    "[Swing:%s] ML filter crashed (fail %d/%d): %s — proceeding WITHOUT ML protection",
                    self.account, self._ml_fail_count, ML_MAX_CONSECUTIVE_FAILS, e,
                )
                # ML filter is down — trade proceeds at full size (1.0) with no ML scaling
                # Circuit breaker above will stop trading after ML_MAX_CONSECUTIVE_FAILS

        # 6. Calculate SL/TP/lots
        try:
            atr_series = calculate_atr(m5["high"], m5["low"], m5["close"], period=14)
            atr_val = float(atr_series.iloc[-1]) if not pd.isna(atr_series.iloc[-1]) else 5.0
        except Exception:
            atr_val = 5.0

        direction = signal.signal_type.value
        sl = calculate_stop_loss(
            price, atr_val, direction,
            self.risk.atr_multiplier, self.risk.spread_buffer,
        )
        tp = calculate_take_profit(
            price, sl, direction, self.risk.risk_reward_ratio,
        )

        # TP1 = 50% of TP distance (for partial TP tracking)
        tp_distance = abs(tp - price)
        if direction == "BUY":
            tp1_price = round(price + tp_distance * 0.5, 2)
        else:
            tp1_price = round(price - tp_distance * 0.5, 2)

        equity = self._get_equity()
        lots = self._calculate_lots(equity, price, sl, atr_val)
        lots *= ml_risk_multiplier  # ML risk-scaling
        if lots < 0.01:
            if self.learning_mode:
                # Force minimum lot to ensure trade executes for data collection
                logger.info("[Swing:%s] ML would skip (lot=%.4f), but learning mode: forcing min lot 0.01", self.account, lots)
                lots = 0.01
            else:
                logger.info("[Swing:%s] ML risk-scaling: lot_size=%.4f < 0.01, skipping", self.account, lots)
                self._record_rejection(signal, f"ml_lot_too_small:{lots:.4f}", session, d1_trend, candles)
                return {"action": "hold", "reason": f"ML risk: lot too small ({lots:.4f})", "signal": signal}

        # 7. Execute or dry-run
        ts_str = (
            m5.index[-1].isoformat()
            if hasattr(m5.index[-1], "isoformat")
            else str(m5.index[-1])
        )

        # Build indicator scores JSON for debugging/feature importance
        indicator_scores_json = None
        if signal.indicators:
            import json
            indicator_scores_json = json.dumps(signal.indicators)

        # Calendar context
        minutes_to_next, next_event_type, next_event_impact = self._get_calendar_context()

        # Link trade to latest collector snapshot for ML training
        ref_signal_id = get_latest_signal_id(self.account_id, self.db_path)

        if self.dry_run:
            trade_id = insert_live_trade(
                account_id=self.account_id,
                timestamp=ts_str,
                direction=direction,
                entry_price=price,
                stop_loss=sl,
                take_profit=tp,
                lot_size=lots,
                confidence=signal.confidence,
                regime=signal.regime or "unknown",
                session=session,
                d1_trend=d1_trend,
                reason=signal.reason,
                ticket=None,
                trading_mode=TradingMode.SWING.value,
                strategy_id=self.strategy_id,
                signal_id=ref_signal_id,
                atr_at_entry=atr_val,
                spread_at_entry=_live_spread if _live_spread > 0 else None,
                ml_risk_multiplier=ml_risk_multiplier,
                ml_risk_reason=ml_risk_reason,
                ml_loss_proba=ml_loss_proba,
                ml_model_used=ml_model_used,
                minutes_to_next_event=minutes_to_next,
                next_event_type=next_event_type,
                next_event_impact=next_event_impact,
                indicator_scores_json=indicator_scores_json,
                tp1_price=tp1_price,
                atr_multiplier=self.risk.atr_multiplier,
                rr_ratio=self.risk.risk_reward_ratio,
                min_confidence_threshold=self.risk.min_confidence,
                db_path=self.db_path,
            )
            log_trade(logger, "OPENED", account=self.account, direction=direction,
                     price=price, lots=lots, sl=sl, tp=tp, tp1=tp1_price,
                     confidence=signal.confidence, reason=signal.reason)
            if self.event_bus:
                self.event_bus.publish(Event(
                    type=EventType.TRADE_OPENED,
                    data={
                        "direction": direction, "symbol": signal.symbol, "price": price,
                        "sl": sl, "tp": tp, "tp1": tp1_price, "lots": lots, "confidence": signal.confidence,
                        "regime": signal.regime or "unknown", "reason": signal.reason,
                        "account": self.account, "trading_mode": "swing", "dry_run": True,
                    },
                ))
            return {
                "action": "dry_run",
                "direction": direction,
                "price": price,
                "sl": sl,
                "tp": tp,
                "tp1": tp1_price,
                "lots": lots,
                "confidence": signal.confidence,
                "regime": signal.regime,
                "trade_id": trade_id,
            }

        # Live execution
        try:
            from metty.bridge.client import MT5Bridge
            from metty.core.models import AccountConfig, AccountName

            port_map = {"A": 5005, "B": 5006, "C": 5007}
            host = os.environ.get(f"MT5_BRIDGE_{self.account}_HOST", "100.68.106.101")
            port = int(os.environ.get(f"MT5_BRIDGE_{self.account}_PORT", str(port_map[self.account])))

            config = AccountConfig(
                name=AccountName[self.account],
                bridge_host=host,
                bridge_port=port,
                broker_login=os.environ.get(f"MT5_LOGIN_{self.account}", ""),
                broker_server=os.environ.get(f"MT5_SERVER_{self.account}", "Exness-MT5Trial17"),
            )
            bridge = MT5Bridge(config)

            async def _execute():
                if not await bridge.connect():
                    return None
                result = await bridge.send_order("XAUUSD", direction, lots, sl, tp)
                await bridge.disconnect()
                return result

            import asyncio
            order_result = asyncio.run(_execute())

            ticket = order_result.ticket if order_result and order_result.success else None

            trade_id = insert_live_trade(
                account_id=self.account_id,
                timestamp=ts_str,
                direction=direction,
                entry_price=price,
                stop_loss=sl,
                take_profit=tp,
                lot_size=lots,
                confidence=signal.confidence,
                regime=signal.regime or "unknown",
                session=session,
                d1_trend=d1_trend,
                reason=signal.reason,
                ticket=ticket,
                trading_mode=TradingMode.SWING.value,
                strategy_id=self.strategy_id,
                signal_id=ref_signal_id,
                atr_at_entry=atr_val,
                spread_at_entry=_live_spread if _live_spread > 0 else None,
                ml_risk_multiplier=ml_risk_multiplier,
                ml_risk_reason=ml_risk_reason,
                ml_loss_proba=ml_loss_proba,
                ml_model_used=ml_model_used,
                minutes_to_next_event=minutes_to_next,
                next_event_type=next_event_type,
                next_event_impact=next_event_impact,
                indicator_scores_json=indicator_scores_json,
                tp1_price=tp1_price,
                atr_multiplier=self.risk.atr_multiplier,
                rr_ratio=self.risk.risk_reward_ratio,
                min_confidence_threshold=self.risk.min_confidence,
                db_path=self.db_path,
            )

            if order_result and order_result.success:
                log_trade(logger, "FILLED", account=self.account, direction=direction,
                         price=price, lots=lots, sl=sl, tp=tp, ticket=ticket)
                if self.event_bus:
                    self.event_bus.publish(Event(
                        type=EventType.TRADE_OPENED,
                        data={
                            "direction": direction, "symbol": signal.symbol, "price": price,
                            "sl": sl, "tp": tp, "lots": lots, "confidence": signal.confidence,
                            "regime": signal.regime or "unknown", "reason": signal.reason,
                            "account": self.account, "trading_mode": "swing", "ticket": ticket,
                        },
                    ))
            else:
                error = order_result.error if order_result else "connection failed"
                logger.error("ORDER FAILED: %s — %s", direction, error)

            return {
                "action": "executed" if (order_result and order_result.success) else "order_failed",
                "direction": direction,
                "price": price,
                "sl": sl,
                "tp": tp,
                "lots": lots,
                "confidence": signal.confidence,
                "regime": signal.regime,
                "ticket": ticket,
                "trade_id": trade_id,
                "order_result": order_result,
            }

        except Exception as e:
            logger.error("Order execution error: %s", e)
            return {"action": "error", "reason": str(e), "signal": signal}

    def run(self, interval: int = 300, max_cycles: int = 0) -> dict:
        """Run continuous trading loop.

        Args:
            interval: Seconds between cycles (default 300 = 5min for M5).
            max_cycles: Max cycles (0 = infinite).

        Returns:
            Dict with stats.
        """
        cycle = 0
        trades_opened = 0
        trades_closed = 0
        errors = 0
        holds = 0

        mode = "DRY-RUN" if self.dry_run else "LIVE"
        logger.info(
            "Starting %s trader (interval=%ds, account=%s, mode=%s)",
            mode, interval, self.account, mode,
        )

        while max_cycles == 0 or cycle < max_cycles:
            cycle += 1
            try:
                result = self.run_once()
                action = result.get("action", "unknown")

                if action in ("executed", "dry_run"):
                    trades_opened += 1
                elif action == "hold":
                    holds += 1
                elif action in ("order_failed", "error"):
                    errors += 1

                # Count closed trades from monitoring
                # (they're logged inside _monitor_positions)

            except Exception as e:
                logger.error("Trading cycle %d failed: %s", cycle, e)
                errors += 1

            logger.info(
                "Cycle %d complete (opened=%d, holds=%d, errors=%d)",
                cycle, trades_opened, holds, errors,
            )

            if max_cycles > 0 and cycle >= max_cycles:
                break

            logger.info("Sleeping %d seconds until next cycle...", interval)
            time.sleep(interval)

        return {
            "cycles": cycle,
            "trades_opened": trades_opened,
            "holds": holds,
            "errors": errors,
        }