"""Scalp trader — M1 scalping mode for the oracle engine.

Runs alongside the M5 swing trader on the same accounts. Uses:
- Faster indicator periods (EMA 5/13, MACD 6/13/5, etc.)
- Session gate (London + Overlap only)
- Spread filter (skip if spread > 30 points)
- Tighter risk (1% risk, 1x ATR SL, 1.5x R:R)
- No D1 counter-trend filter (scalps are too short for D1 alignment)
- Persistent bridge connection (keep-alive + auto-reconnect)
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

from broky.indicators.atr import calculate_atr
from broky.risk.circuit_breaker import CircuitBreaker
from broky.risk.position_sizing import (
    calculate_position_size,
    calculate_stop_loss,
    calculate_take_profit,
)
from broky.risk.spread_filter import check_spread
from broky.signals.scalp_generator import generate_scalp_signal, SCALP_SPREAD_MAX
from metty.core.db import (
    close_live_trade,
    get_latest_signal_id,
    get_open_trades,
    init_db,
    insert_live_trade,
    insert_rejected_signal,
)
from shared.events import Event, EventBus, EventType
from shared.models import SignalType, TradingMode

logger = logging.getLogger(__name__)

# Account IDs in the database
ACCOUNT_IDS = {"A": 1, "B": 2, "C": 3}

# Contract size: 1 lot XAUUSD = 100 oz
CONTRACT_SIZE = 100.0

# ML filter circuit breaker: stop trading after N consecutive ML failures
ML_MAX_CONSECUTIVE_FAILS = 5


@dataclass
class ScalpRiskConfig:
    """Risk configuration for M1 scalping."""
    risk_per_trade: float = 0.01       # 1% (conservative for high freq)
    atr_multiplier: float = 1.0        # Tight SL: 1x ATR (M1 ATR ~1-3 pts)
    risk_reward_ratio: float = 1.5     # Smaller TP target
    min_confidence: float = 0.45       # Lower threshold (M1 needs more trades)
    max_holding_bars: int = 20         # 20 min max hold
    cooldown_bars: int = 3             # 3 min cooldown
    spread_buffer: float = 1.5          # Tighter buffer
    consecutive_loss_limit: int = 5    # Same as swing
    daily_loss_limit_pct: float = 0.03  # 3% (tighter than 5% for scalping)
    max_spread_points: float = 35      # Skip if spread > max (overridden per-account: A=40, B/C=30)
    bar_seconds: int = 60              # M1 = 60s
    # Partial TP (Option C): close at TP1, open scale-in position
    partial_tp_enabled: bool = False  # Feature flag — must be explicitly enabled
    tp1_ratio: float = 0.5   # TP1 at 50% of TP distance from entry
    rr_scale_in: float = 2.5  # RR ratio for the scale-in position


class ScalpTrader:
    """M1 scalping trader with persistent bridge connection.

    Each account runs its own ScalpTrader instance. The persistent bridge
    keeps the connection alive between cycles, reconnecting only when needed.

    Usage:
        trader = ScalpTrader(account="A", db_path="data/oracle.db")
        trader.run_once()  # Single cycle
        trader.run(interval=60, max_cycles=0)  # Continuous loop
    """

    def __init__(
        self,
        account: str = "A",
        db_path: Optional[Path] = None,
        data_dir: Optional[Path] = None,
        dry_run: bool = True,
        risk_config: Optional[ScalpRiskConfig] = None,
        event_bus: Optional[EventBus] = None,
    ):
        self.account = account.upper()
        self.db_path = db_path
        self.data_dir = data_dir or Path("data/xau-data")
        self.dry_run = dry_run
        self.account_id = ACCOUNT_IDS.get(self.account, 1)
        self.risk = risk_config or ScalpRiskConfig()
        # Per-account max spread override (Account A = XAUUSDm Standard, wider spread)
        per_account_spread = {
            "A": float(os.environ.get("SCALP_MAX_SPREAD_A", os.environ.get("SCALP_MAX_SPREAD", "40"))),
            "B": float(os.environ.get("SCALP_MAX_SPREAD_B", os.environ.get("SCALP_MAX_SPREAD", "30"))),
            "C": float(os.environ.get("SCALP_MAX_SPREAD_C", os.environ.get("SCALP_MAX_SPREAD", "30"))),
        }
        self.risk.max_spread_points = per_account_spread.get(self.account, self.risk.max_spread_points)
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
        self.strategy_id = f"scalp-{self.account}"
        self.circuit_breaker = CircuitBreaker(
            consecutive_loss_limit=self.risk.consecutive_loss_limit,
            daily_loss_limit_pct=self.risk.daily_loss_limit_pct,
        )
        self._calendar_cache: list = []
        self._calendar_cache_time: float = 0
        self._sentiment_cache: dict = {}
        self._sentiment_cache_time: float = 0
        self._mfe_mae_state: dict[int, dict] = {}  # trade_id → {mfe, mae, mfe_pct, mae_pct}
        self._last_d1_trend: Optional[str] = None
        self._last_h4_trend: Optional[str] = None
        self._last_exit_time: Optional[datetime] = None
        self._cycle_count: int = 0
        self._bridge = None  # PersistentMT5Bridge, initialized lazily
        self.event_bus = event_bus
        # ML filter — only enabled if models have decent accuracy
        self._ml_enabled = os.environ.get("ML_FILTER_ENABLED", "0") == "1"
        self._ml_predictor = None
        self._ml_fail_count: int = 0  # consecutive ML prediction failures
        if self._ml_enabled:
            try:
                from broky.ml.trade_outcome_predictor import TradeOutcomePredictor
                self._ml_predictor = TradeOutcomePredictor(
                    loss_threshold=float(os.environ.get("ML_LOSS_THRESHOLD", "0.65")),
                )
                logger.info("[Scalp:%s] ML filter enabled: %s", self.account,
                           "models loaded" if self._ml_predictor.enabled else "no models")
                # Health check: verify ML predictor can actually produce predictions
                if self._ml_predictor.enabled:
                    healthy, reason = self._ml_predictor.health_check()
                    if not healthy:
                        logger.critical("[Scalp:%s] ML filter UNHEALTHY: %s — disabling", self.account, reason)
                        self._ml_enabled = False
                        self._ml_predictor = None
                    else:
                        logger.info("[Scalp:%s] ML filter health check passed: %s", self.account, reason)
            except Exception as e:
                logger.warning("[Scalp:%s] ML filter init failed: %s", self.account, e)
                self._ml_enabled = False

    def _get_bridge(self):
        """Get or create a persistent bridge connection."""
        if self._bridge is not None:
            return self._bridge

        try:
            from metty.bridge.client import PersistentMT5Bridge
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

            self._bridge = PersistentMT5Bridge(config)
            return self._bridge

        except ImportError:
            logger.warning("PersistentMT5Bridge not available")
            return None

    def _fetch_candles(self, retries: int = 2) -> Optional[dict[str, pd.DataFrame]]:
        """Fetch M1 candle data from persistent bridge, fall back to CSV.

        Retries bridge connection on transient failures before falling back.
        """
        from broky.data.resampler import resample_timeframe
        from metty.execution.historical_collector import _normalize_columns

        bridge = self._get_bridge()
        if bridge is not None:
            for attempt in range(retries + 1):
                try:
                    if not bridge.ensure_connected_sync():
                        logger.warning(
                            "Bridge M1 fetch: ensure_connected_sync=False (attempt %d/%d)",
                            attempt + 1, retries + 1,
                        )
                        if attempt < retries:
                            import time as _time
                            _time.sleep(1.0 * (attempt + 1))
                        continue

                    m1 = bridge.fetch_candles_persistent_sync("XAUUSD", "M1", 500)
                    if m1 is not None and not m1.empty:
                        m1 = _normalize_columns(m1)
                        candles = {"M1": m1}
                        for tf in ["M5", "M15", "H1"]:
                            try:
                                candles[tf] = _normalize_columns(
                                    resample_timeframe(m1.reset_index(), tf)
                                )
                            except Exception:
                                pass
                        logger.info("Fetched M1 candles from bridge: %d bars", len(m1))
                        return candles
                    else:
                        logger.warning(
                            "Bridge M1 fetch returned empty (attempt %d/%d)",
                            attempt + 1, retries + 1,
                        )
                except Exception as e:
                    logger.warning(
                        "Bridge M1 fetch failed (attempt %d/%d): %s",
                        attempt + 1, retries + 1, e,
                    )
                    if attempt < retries:
                        import time as _time
                        _time.sleep(1.0 * (attempt + 1))
        else:
            logger.warning("Bridge M1 fetch: _get_bridge() returned None for account %s", self.account)

        logger.info("Falling back to CSV for M1 candles (account=%s)", self.account)
        return self._fetch_candles_csv()

    def _fetch_candles_csv(self) -> Optional[dict[str, pd.DataFrame]]:
        """Fallback: load candles from CSV."""
        try:
            from broky.data.loader import load_timeframe
            from broky.data.resampler import resample_timeframe
            from metty.execution.historical_collector import _normalize_columns

            m1_raw = load_timeframe(self.data_dir, "M1")
            if m1_raw.empty:
                return None

            m1 = _normalize_columns(m1_raw.tail(500))
            candles = {"M1": m1}
            for tf in ["M5", "M15", "H1"]:
                try:
                    candles[tf] = _normalize_columns(resample_timeframe(m1_raw, tf))
                except Exception:
                    pass
            return candles
        except Exception as e:
            logger.error("CSV fallback failed: %s", e)
            return None

    def _get_spread(self) -> float:
        """Get current spread from real-time bid/ask."""
        bridge = self._get_bridge()
        if bridge is None:
            return 0.0

        try:
            if bridge.ensure_connected_sync():
                spread = bridge.get_spread_sync("XAUUSD")
                if spread is not None and spread > 0:
                    return spread
        except Exception as e:
            logger.warning("Spread fetch failed: %s", e)

        return 0.0

    def _check_cooldown(self) -> bool:
        """Check if we're still in cooldown after last exit."""
        if self._last_exit_time is None:
            return False
        elapsed = (datetime.now(timezone.utc) - self._last_exit_time).total_seconds()
        cooldown_seconds = self.risk.cooldown_bars * self.risk.bar_seconds
        return elapsed < cooldown_seconds

    def _check_existing_scalp_position(self) -> bool:
        """Check if there's an open scalp position for this strategy."""
        open_trades = get_open_trades(self.account_id, self.db_path)
        for trade in open_trades:
            strategy = trade.get("strategy_id", "") if "strategy_id" in trade else ""
            # Also check by trading_mode column
            mode = trade.get("trading_mode", "") if "trading_mode" in trade else ""
            if strategy == self.strategy_id or mode == "scalp":
                return True
        return False

    def _monitor_positions(self, candles: dict[str, pd.DataFrame]) -> list[dict]:
        """Check open scalp trades for exit conditions."""
        open_trades = get_open_trades(self.account_id, self.db_path)
        closed = []

        if not open_trades or "M1" not in candles:
            return closed

        m1 = candles["M1"]
        current_price = float(m1["close"].iloc[-1])
        current_high = float(m1["high"].iloc[-1]) if "high" in m1.columns else current_price
        current_low = float(m1["low"].iloc[-1]) if "low" in m1.columns else current_price
        now_str = datetime.now(timezone.utc).isoformat()

        for trade in open_trades:
            # Only manage scalp trades
            strategy = trade.get("strategy_id", "") if "strategy_id" in trade else ""
            mode = trade.get("trading_mode", "") if "trading_mode" in trade else ""
            if strategy != self.strategy_id and mode != "scalp":
                continue

            trade_id = trade["id"]
            direction = trade["direction"]
            entry_price = trade["entry_price"]
            sl = trade["stop_loss"]
            tp = trade["take_profit"]
            lot_size = trade["lot_size"]

            # Update MFE/MAE tracking
            if trade_id not in self._mfe_mae_state:
                self._mfe_mae_state[trade_id] = {"mfe": 0.0, "mae": 0.0}
            state = self._mfe_mae_state[trade_id]
            if direction == "BUY":
                favorable = current_high - entry_price
                adverse = entry_price - current_low
            else:
                favorable = entry_price - current_low
                adverse = current_high - entry_price
            state["mfe"] = max(state["mfe"], favorable)
            state["mae"] = max(state["mae"], adverse)

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
                    closed.extend(self._execute_tp1_close(trade, tp1_price, state, now_str))
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
                    if entry_time in m1.index:
                        bars_held = len(m1[m1.index > entry_time])
                    else:
                        bars_held = len(m1[m1.index > entry_time.tz_localize(None)])
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

                # MFE/MAE from tracking state
                mfe = state["mfe"] if entry_price > 0 else None
                mae = state["mae"] if entry_price > 0 else None
                mfe_pct = (mfe / entry_price * 100) if mfe and entry_price > 0 else None
                mae_pct = (mae / entry_price * 100) if mae and entry_price > 0 else None

                close_live_trade(
                    trade_id=trade_id,
                    exit_price=exit_price,
                    exit_time=now_str,
                    pnl=round(pnl, 2),
                    pnl_pct=round(pnl_pct, 4),
                    exit_reason=exit_reason,
                    mfe=round(mfe, 2) if mfe else None,
                    mae=round(mae, 2) if mae else None,
                    mfe_pct=round(mfe_pct, 4) if mfe_pct else None,
                    mae_pct=round(mae_pct, 4) if mae_pct else None,
                    exit_regime="unknown",
                    exit_d1_trend=self._last_d1_trend,
                    exit_h4_trend=self._last_h4_trend,
                    tp1_price=trade.get("tp1_price"),
                    db_path=self.db_path,
                )
                # Clean up MFE/MAE state
                self._mfe_mae_state.pop(trade_id, None)

                if pnl > 0:
                    self.circuit_breaker.record_win(pnl)
                else:
                    self.circuit_breaker.record_loss(pnl)

                self._last_exit_time = datetime.now(timezone.utc)
                closed.append({
                    "trade_id": trade["id"],
                    "direction": direction,
                    "exit_reason": exit_reason,
                    "exit_price": exit_price,
                    "pnl": round(pnl, 2),
                    "pnl_pct": round(pnl_pct, 4),
                })
                logger.info(
                    "Scalp trade #%d closed: %s %s @ %.2f → %.2f (%s, PnL=%.2f)",
                    trade["id"], direction, trade.get("symbol", "XAUUSD"),
                    entry_price, exit_price, exit_reason, pnl,
                )

                if self.event_bus:
                    self.event_bus.publish(Event(
                        type=EventType.TRADE_CLOSED,
                        data={
                            "direction": direction, "symbol": trade.get("symbol", "XAUUSD"),
                            "entry_price": entry_price, "exit_price": exit_price,
                            "pnl": round(pnl, 2), "pnl_pct": round(pnl_pct, 4),
                            "exit_reason": exit_reason, "account": self.account,
                            "trading_mode": "scalp",
                        },
                    ))

        return closed

    def _classify_session(self, timestamp: datetime) -> str:
        hour = timestamp.hour
        if 13 <= hour < 16:
            return "overlap"
        if 8 <= hour < 16:
            return "london"
        if 13 <= hour < 22:
            return "ny"
        return "asian"

    def _check_trend_flips(self, d1_trend: str, h4_trend: str | None) -> None:
        """Detect D1/H4 trend changes and send Telegram alert + EventBus."""
        if self._notifier is None or not self._notifier.enabled:
            return

        from datetime import timezone
        now = datetime.now(timezone.utc).strftime("%H:%M")
        alerts = []

        if self._last_d1_trend is not None and d1_trend != "unknown":
            if d1_trend != self._last_d1_trend:
                direction = "🟢 BULLISH" if d1_trend == "bullish" else "🔴 BEARISH"
                alerts.append(
                    f"<b>D1 Trend Flip</b> {now}\n"
                    f"Account {self.account}: {self._last_d1_trend} → {direction}"
                )
                if self.event_bus:
                    self.event_bus.publish(Event(
                        type=EventType.TREND_FLIP,
                        data={
                            "timeframe": "D1",
                            "direction": d1_trend,
                            "old_direction": self._last_d1_trend,
                            "symbol": "XAUUSD",
                            "account": self.account,
                        },
                    ))

        if self._last_h4_trend is not None and h4_trend and h4_trend != "unknown":
            if h4_trend != self._last_h4_trend:
                direction = "🟢 BULLISH" if h4_trend == "bullish" else "🔴 BEARISH"
                alerts.append(
                    f"<b>H4 Trend Flip</b> {now}\n"
                    f"Account {self.account}: {self._last_h4_trend} → {direction}"
                )
                if self.event_bus:
                    self.event_bus.publish(Event(
                        type=EventType.TREND_FLIP,
                        data={
                            "timeframe": "H4",
                            "direction": h4_trend,
                            "old_direction": self._last_h4_trend,
                            "symbol": "XAUUSD",
                            "account": self.account,
                        },
                    ))

        # Update tracking (only after comparison)
        if d1_trend != "unknown":
            self._last_d1_trend = d1_trend
        if h4_trend and h4_trend != "unknown":
            self._last_h4_trend = h4_trend

        for msg in alerts:
            try:
                self._notifier.send(msg)
            except Exception:
                pass

    def _determine_m1_trend(self, m1: pd.DataFrame) -> str:
        """Determine short-term trend from EMA 21/50 on M1 data."""
        if m1 is None or len(m1) < 50:
            return "unknown"
        close = m1["close"]
        ema21 = close.ewm(span=21, adjust=False).mean()
        ema50 = close.ewm(span=50, adjust=False).mean()
        if pd.isna(ema50.iloc[-1]):
            return "unknown"
        if ema21.iloc[-1] > ema50.iloc[-1]:
            return "bullish"
        return "bearish"

    def _get_equity(self) -> float:
        """Get current account equity from MT5."""
        bridge = self._get_bridge()
        if bridge is None:
            return 500.0
        try:
            if bridge.ensure_connected_sync():
                info = bridge.fetch_account_info_sync()
                return info.equity if info else 500.0
        except Exception:
            pass
        return 500.0

    def _get_sentiment(self) -> dict:
        """Get sentiment data with 15-minute cache."""
        now = time.time()
        if now - self._sentiment_cache_time > 900:
            try:
                from metty.execution.live_collector import fetch_live_sentiment
                self._sentiment_cache = fetch_live_sentiment()
            except Exception as e:
                logger.warning("[Scalp:%s] Sentiment fetch failed: %s", self.account, e)
                self._sentiment_cache = {}
            self._sentiment_cache_time = now
        return self._sentiment_cache

    def _get_calendar_context(self) -> tuple[int | None, str | None, str | None]:
        """Get minutes to next high-impact event and its type/impact."""
        try:
            from broky.data.calendar import fetch_calendar
            now = time.time()
            if now - self._calendar_cache_time > 3600:
                self._calendar_cache = fetch_calendar(days_ahead=2, filter_currencies={"USD"})
                self._calendar_cache_time = now
            if not self._calendar_cache:
                return None, None, None
            from datetime import datetime as _dt, timezone as _tz
            now_utc = _dt.now(_tz.utc)
            min_minutes = None
            min_event_type = None
            min_event_impact = None
            for ev in self._calendar_cache:
                impact = ev.get("impact", "")
                if impact not in ("High", "high"):
                    continue
                ev_time = ev.get("time") or ev.get("datetime")
                if not ev_time:
                    continue
                try:
                    if isinstance(ev_time, str):
                        ev_dt = _dt.fromisoformat(ev_time.replace("Z", "+00:00"))
                    else:
                        ev_dt = ev_time
                    delta = (ev_dt - now_utc).total_seconds() / 60
                    if delta > 0 and (min_minutes is None or delta < min_minutes):
                        min_minutes = int(delta)
                        min_event_type = ev.get("title", ev.get("event", "unknown"))
                        min_event_impact = impact
                except Exception:
                    continue
            return min_minutes, min_event_type, min_event_impact
        except Exception:
            return None, None, None

    def _record_rejection(self, signal, reason: str, session: str = "unknown",
                          d1_trend: str | None = None, h4_trend: str | None = None) -> None:
        """Record a rejected signal for survivorship bias analysis."""
        try:
            ts_str = datetime.now(timezone.utc).isoformat()
            insert_rejected_signal(
                account_id=self.account_id,
                timestamp=ts_str,
                direction=signal.signal_type.value if signal else "HOLD",
                confidence=signal.confidence if signal else 0.0,
                price=signal.price if signal else 0.0,
                rejection_reason=reason,
                trading_mode=TradingMode.SCALP.value,
                strategy_id=self.strategy_id,
                regime=signal.regime if signal else "unknown",
                session=session,
                d1_trend=d1_trend,
                db_path=self.db_path,
            )
        except Exception as e:
            logger.warning("[Scalp:%s] Rejected signal recording failed: %s", self.account, e)

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

        mfe = mfe_mae.get("mfe", 0) if mfe_mae else 0
        mae = mfe_mae.get("mae", 0) if mfe_mae else 0
        mfe_pct = (mfe / entry_price * 100) if mfe and entry_price > 0 else None
        mae_pct = (mae / entry_price * 100) if mae and entry_price > 0 else None

        exit_d1_trend = self._last_d1_trend
        exit_h4_trend = self._last_h4_trend

        close_live_trade(
            trade_id=trade["id"],
            exit_price=exit_price,
            exit_time=now_str,
            pnl=round(pnl, 2),
            pnl_pct=round(pnl_pct, 4),
            exit_reason="tp1_hit",
            mfe=round(mfe, 2) if mfe else None,
            mae=round(mae, 2) if mae else None,
            mfe_pct=round(mfe_pct, 4) if mfe_pct else None,
            mae_pct=round(mae_pct, 4) if mae_pct else None,
            exit_regime="unknown",
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
                logger.warning("[Scalp:%s] Failed to close position %s at TP1 in MT5: %s",
                               self.account, trade["ticket"], e)

        closed.append({
            "trade_id": trade["id"],
            "direction": direction,
            "exit_reason": "tp1_hit",
            "exit_price": exit_price,
            "pnl": round(pnl, 2),
            "pnl_pct": round(pnl_pct, 4),
        })

        logger.info("[Scalp:%s] TP1_CLOSED #%d %s @ %.2f (PnL=%.2f)",
                     self.account, trade_id, direction, exit_price, pnl)

        # 2. Open scale-in position (position 2) at current price
        remaining_distance = abs(tp - tp1_price)
        if direction == "BUY":
            new_sl = tp1_price - remaining_distance / self.risk.rr_scale_in
        else:
            new_sl = tp1_price + remaining_distance / self.risk.rr_scale_in

        new_lots = lot_size  # Same lot size (Exness min = 0.01)
        current_price = tp1_price  # Approximate entry for scale-in

        # Open scale-in in MT5 (if not dry run)
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
                    logger.error("[Scalp:%s] Scale-in order FAILED at TP1: %s",
                                 self.account, order_result.error)
                    return closed  # Don't insert scale-in if MT5 order failed
            except Exception as e:
                logger.error("[Scalp:%s] Scale-in MT5 error at TP1: %s", self.account, e)
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
            trading_mode="scalp",
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
        self._mfe_mae_state[scale_in_id] = {"mfe": 0.0, "mae": 0.0}

        logger.info("[Scalp:%s] SCALE_IN #%d %s @ %.2f SL=%.2f TP=%.2f (from #%d)",
                     self.account, scale_in_id, direction, current_price, new_sl, tp, trade_id)

        return closed

    def run_once(self) -> dict:
        """Run a single scalp trading cycle."""
        init_db(self.db_path)
        self._cycle_count += 1

        # 1. Fetch M1 candles
        candles = self._fetch_candles()
        if not candles or "M1" not in candles:
            return {"action": "skip", "reason": "no M1 candle data"}

        m1 = candles["M1"]
        if len(m1) < 50:
            return {"action": "skip", "reason": f"M1 data too short ({len(m1)} bars)"}

        # 2. Monitor existing scalp positions first
        closed = self._monitor_positions(candles)

        # 3. Get spread and check
        spread = self._get_spread()
        if spread > 0 and not check_spread(spread, self.risk.max_spread_points):
            self._record_rejection(None, f"spread {spread:.0f} > max {self.risk.max_spread_points:.0f}")
            return {
                "action": "hold",
                "reason": f"spread {spread:.0f} > max {self.risk.max_spread_points:.0f}",
                "spread": spread,
            }

        # 4. Classify session
        timestamp = m1.index[-1]
        if hasattr(timestamp, "to_pydatetime"):
            timestamp = timestamp.to_pydatetime().replace(tzinfo=timezone.utc)
        session = self._classify_session(timestamp)

        # Session gate: only trade during liquid sessions (London, Overlap, NY)
        if session not in ("london", "overlap", "ny"):
            self._record_rejection(None, f"scalp blocked: {session} session", session=session)
            return {
                "action": "hold",
                "reason": f"scalp blocked: {session} session",
                "session": session,
            }

        # 5. Generate scalp signal
        signal = generate_scalp_signal(
            close=m1["close"],
            high=m1["high"],
            low=m1["low"],
            volume=m1["volume"],
            current_price=float(m1["close"].iloc[-1]),
            timestamp=timestamp,
            spread=spread if spread > 0 else None,
            min_confidence=self.risk.min_confidence,
            max_spread=self.risk.max_spread_points,
        )
        signal.strategy_id = self.strategy_id

        if signal.signal_type == SignalType.HOLD:
            return {
                "action": "hold",
                "reason": signal.reason,
                "signal": signal,
            }

        # 6. Risk checks
        can_trade, cb_reason = self.circuit_breaker.can_open_trade()
        if not can_trade:
            self._record_rejection(signal, f"circuit breaker: {cb_reason}", session=session)
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
            self._record_rejection(signal, "cooldown after last scalp exit", session=session)
            return {
                "action": "hold",
                "reason": "cooldown after last scalp exit",
                "signal": signal,
            }

        if self._check_existing_scalp_position():
            self._record_rejection(signal, "scalp position already open", session=session)
            return {
                "action": "hold",
                "reason": "scalp position already open",
                "signal": signal,
            }

        # 6.5. ML filter — risk-scale position size based on P(LOSS) prediction
        ml_risk_multiplier = 1.0
        ml_risk_reason: str | None = None
        ml_loss_proba: float | None = None
        ml_model_used: str | None = None
        ml_model_version: str | None = None

        # Circuit breaker: if ML filter has failed too many times, stop trading
        if self._ml_enabled and self._ml_fail_count >= ML_MAX_CONSECUTIVE_FAILS:
            logger.critical(
                "[Scalp:%s] ML filter failed %d times consecutively — circuit breaker: holding",
                self.account, self._ml_fail_count,
            )
            self._record_rejection(signal, f"ml_filter_circuit_break:{self._ml_fail_count}_fails", session=session)
            return {"action": "hold", "reason": f"ML circuit breaker ({self._ml_fail_count} consecutive failures)", "signal": signal}

        if self._ml_enabled and self._ml_predictor is not None:
            try:
                from broky.ml.trade_outcome_predictor import compute_features_from_candles

                _sentiment = self._get_sentiment()
                # Derive D1 trend proxy from H1 EMA50 (scalp doesn't have D1 data)
                h1 = candles.get("H1")
                _d1_proxy = "unknown"
                if h1 is not None and len(h1) >= 50:
                    try:
                        ema50 = h1["close"].ewm(span=50, adjust=False).mean()
                        if h1["close"].iloc[-1] > ema50.iloc[-1]:
                            _d1_proxy = "bullish"
                        else:
                            _d1_proxy = "bearish"
                    except Exception:
                        pass
                ml_features = compute_features_from_candles(
                    candles, str(signal.signal_type.value),
                    spread=spread if spread > 0 else 0,
                    d1_trend=_d1_proxy,
                    h4_trend="unknown",
                    session=session,
                    sentiment=_sentiment,
                )
                ml_risk_multiplier, ml_risk_reason, ml_loss_proba, ml_model_used = self._ml_predictor.get_risk_multiplier(
                    ml_features, ml_features.get("regime", "trending"), str(signal.signal_type.value),
                )
                # ML filter succeeded — reset failure counter
                self._ml_fail_count = 0

                if ml_risk_multiplier == 0:
                    logger.info("[Scalp:%s] ML filter blocked trade: %s", self.account, ml_risk_reason)
                    self._record_rejection(signal, ml_risk_reason or "ml_filter_blocked", session=session)
                    return {"action": "hold", "reason": ml_risk_reason, "signal": signal}
                elif ml_risk_multiplier < 1.0:
                    logger.info("[Scalp:%s] ML risk-scaling: %s", self.account, ml_risk_reason)

            except Exception as e:
                self._ml_fail_count += 1
                logger.error(
                    "[Scalp:%s] ML filter crashed (fail %d/%d): %s — proceeding WITHOUT ML protection",
                    self.account, self._ml_fail_count, ML_MAX_CONSECUTIVE_FAILS, e,
                )
                # ML filter is down — trade proceeds at full size (1.0) with no ML scaling

        # 7. Calculate SL/TP/lots using M1 ATR
        try:
            atr_series = calculate_atr(m1["high"], m1["low"], m1["close"], period=7)
            atr_val = float(atr_series.iloc[-1]) if not pd.isna(atr_series.iloc[-1]) else 2.0
        except Exception:
            atr_val = 2.0

        direction = signal.signal_type.value
        sl = calculate_stop_loss(
            signal.price, atr_val, direction,
            self.risk.atr_multiplier, self.risk.spread_buffer,
        )
        tp = calculate_take_profit(
            signal.price, sl, direction, self.risk.risk_reward_ratio,
        )

        # TP1 = tp1_ratio of TP distance (for partial TP tracking)
        tp_distance = abs(tp - signal.price)
        if direction == "BUY":
            tp1_price = round(signal.price + tp_distance * self.risk.tp1_ratio, 2)
        else:
            tp1_price = round(signal.price - tp_distance * self.risk.tp1_ratio, 2)

        equity = self._get_equity()
        lots = calculate_position_size(
            equity, self.risk.risk_per_trade, signal.price, sl, CONTRACT_SIZE,
        )
        lots *= ml_risk_multiplier  # ML risk-scaling
        if lots < 0.01:
            logger.info("[Scalp:%s] ML risk-scaling: lots=%.4f < 0.01, skipping", self.account, lots)
            self._record_rejection(signal, f"ml_lot_too_small ({lots:.4f})", session=session)
            return {"action": "hold", "reason": f"ML risk: lot too small ({lots:.4f})", "signal": signal}

        # 7.5. Calendar context for data collection
        minutes_to_next_event, next_event_type, next_event_impact = self._get_calendar_context()

        # 8. Execute or dry-run
        ts_str = (
            m1.index[-1].isoformat()
            if hasattr(m1.index[-1], "isoformat")
            else str(m1.index[-1])
        )
        m1_trend = self._determine_m1_trend(m1)

        # Store trend state for exit context (scalp uses M1 as D1 proxy)
        self._last_d1_trend = _d1_proxy  # H1 EMA50-based proxy
        self._last_h4_trend = "unknown"  # scalp doesn't fetch H4

        # Detect trend flips and send Telegram alert
        self._check_trend_flips(_d1_proxy, None)

        # Build indicator scores JSON for debugging/feature importance
        import json as _json
        indicator_scores_json = _json.dumps(signal.indicators) if signal.indicators else None
        ref_signal_id = get_latest_signal_id(self.account_id, self.db_path)

        if self.dry_run:
            trade_id = insert_live_trade(
                account_id=self.account_id,
                timestamp=ts_str,
                direction=direction,
                entry_price=signal.price,
                stop_loss=sl,
                take_profit=tp,
                lot_size=lots,
                confidence=signal.confidence,
                regime=signal.regime or "unknown",
                session=session,
                d1_trend=_d1_proxy,
                reason=signal.reason,
                ticket=None,
                trading_mode=TradingMode.SCALP.value,
                strategy_id=self.strategy_id,
                signal_id=ref_signal_id,
                atr_at_entry=atr_val,
                indicator_scores_json=indicator_scores_json,
                spread_at_entry=spread if spread > 0 else None,
                ml_risk_multiplier=ml_risk_multiplier if ml_risk_multiplier != 1.0 else None,
                ml_risk_reason=ml_risk_reason,
                ml_loss_proba=ml_loss_proba,
                ml_model_used=ml_model_used,
                ml_model_version=ml_model_version,
                minutes_to_next_event=minutes_to_next_event,
                next_event_type=next_event_type,
                next_event_impact=next_event_impact,
                tp1_price=tp1_price,
                atr_multiplier=self.risk.atr_multiplier,
                rr_ratio=self.risk.risk_reward_ratio,
                min_confidence_threshold=self.risk.min_confidence,
                db_path=self.db_path,
            )
            logger.info(
                "[SCALP DRY-RUN] %s @ %.2f SL=%.2f TP=%.2f lots=%.4f conf=%.2f (%s)",
                direction, signal.price, sl, tp, lots, signal.confidence, signal.reason,
            )
            if self.event_bus:
                self.event_bus.publish(Event(
                    type=EventType.TRADE_OPENED,
                    data={
                        "direction": direction, "symbol": "XAUUSD", "price": signal.price,
                        "sl": sl, "tp": tp, "lots": lots, "confidence": signal.confidence,
                        "regime": signal.regime or "unknown", "reason": signal.reason,
                        "account": self.account, "trading_mode": "scalp", "dry_run": True,
                    },
                ))
            return {
                "action": "dry_run",
                "direction": direction,
                "price": signal.price,
                "sl": sl,
                "tp": tp,
                "lots": lots,
                "confidence": signal.confidence,
                "regime": signal.regime,
                "trade_id": trade_id,
            }

        # Live execution
        try:
            bridge = self._get_bridge()
            if bridge is None or not bridge.ensure_connected_sync():
                return {"action": "error", "reason": "bridge connection failed", "signal": signal}

            import asyncio
            order_result = asyncio.run(bridge.send_order("XAUUSD", direction, lots, sl, tp))
            ticket = order_result.ticket if order_result and order_result.success else None

            trade_id = insert_live_trade(
                account_id=self.account_id,
                timestamp=ts_str,
                direction=direction,
                entry_price=signal.price,
                stop_loss=sl,
                take_profit=tp,
                lot_size=lots,
                confidence=signal.confidence,
                regime=signal.regime or "unknown",
                session=session,
                d1_trend=_d1_proxy,
                reason=signal.reason,
                ticket=ticket,
                trading_mode=TradingMode.SCALP.value,
                strategy_id=self.strategy_id,
                signal_id=ref_signal_id,
                atr_at_entry=atr_val,
                indicator_scores_json=indicator_scores_json,
                spread_at_entry=spread if spread > 0 else None,
                ml_risk_multiplier=ml_risk_multiplier if ml_risk_multiplier != 1.0 else None,
                ml_risk_reason=ml_risk_reason,
                ml_loss_proba=ml_loss_proba,
                ml_model_used=ml_model_used,
                ml_model_version=ml_model_version,
                minutes_to_next_event=minutes_to_next_event,
                next_event_type=next_event_type,
                next_event_impact=next_event_impact,
                tp1_price=tp1_price,
                atr_multiplier=self.risk.atr_multiplier,
                rr_ratio=self.risk.risk_reward_ratio,
                min_confidence_threshold=self.risk.min_confidence,
                db_path=self.db_path,
            )

            if order_result and order_result.success:
                logger.info(
                    "SCALP ORDER FILLED: %s @ %.2f SL=%.2f TP=%.2f lots=%.4f ticket=%s",
                    direction, signal.price, sl, tp, lots, ticket,
                )
                if self.event_bus:
                    self.event_bus.publish(Event(
                        type=EventType.TRADE_OPENED,
                        data={
                            "direction": direction, "symbol": "XAUUSD", "price": signal.price,
                            "sl": sl, "tp": tp, "lots": lots, "confidence": signal.confidence,
                            "regime": signal.regime or "unknown", "reason": signal.reason,
                            "account": self.account, "trading_mode": "scalp", "ticket": ticket,
                        },
                    ))
            else:
                error = order_result.error if order_result else "connection failed"
                logger.error("SCALP ORDER FAILED: %s — %s", direction, error)

            return {
                "action": "executed" if (order_result and order_result.success) else "order_failed",
                "direction": direction,
                "price": signal.price,
                "sl": sl,
                "tp": tp,
                "lots": lots,
                "confidence": signal.confidence,
                "regime": signal.regime,
                "ticket": ticket,
                "trade_id": trade_id,
            }
        except Exception as e:
            logger.error("Scalp execution error: %s", e)
            return {"action": "error", "reason": str(e), "signal": signal}

    def run(self, interval: int = 60, max_cycles: int = 0) -> dict:
        """Run continuous scalp trading loop.

        Args:
            interval: Seconds between cycles (default 60 for M1).
            max_cycles: Max cycles (0 = infinite).

        Returns:
            Dict with stats.
        """
        cycle = 0
        trades_opened = 0
        errors = 0
        holds = 0

        mode = "DRY-RUN" if self.dry_run else "LIVE"
        logger.info(
            "Starting SCALP %s trader (interval=%ds, account=%s, strategy=%s)",
            mode, interval, self.account, self.strategy_id,
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

            except Exception as e:
                logger.error("Scalp cycle %d failed: %s", cycle, e)
                errors += 1

            logger.info(
                "Scalp cycle %d complete (opened=%d, holds=%d, errors=%d)",
                cycle, trades_opened, holds, errors,
            )

            if max_cycles > 0 and cycle >= max_cycles:
                break

            logger.info("Scalp sleeping %d seconds until next cycle...", interval)
            time.sleep(interval)

        return {
            "cycles": cycle,
            "trades_opened": trades_opened,
            "holds": holds,
            "errors": errors,
        }

    def shutdown(self) -> None:
        """Disconnect the persistent bridge on shutdown."""
        if self._bridge is not None:
            try:
                import asyncio
                asyncio.run(self._bridge.disconnect())
            except Exception as e:
                logger.warning("Error disconnecting scalp bridge: %s", e)
            self._bridge = None