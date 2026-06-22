"""M5 Scalp trader — 6-EMA Ribbon Cloud strategy with 4-level ATR-based TP.

Runs alongside the M5 swing trader and M1 scalp trader. Uses:
- 6-EMA Ribbon Cloud (8, 13, 21, 34, 55, 89) for trend identification
- H4 + D1 trend alignment as soft confidence filter
- 4-level ATR-based take profit for position scaling
- Session gate (London + Overlap + NY)
- Spread filter (skip if spread > 30 points)
- Moderate risk (1.5% risk, 1.5x ATR SL, 2.0x R:R)
- Signal quality scoring (ribbon expansion, ATR, session, pullback)
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

from broky.indicators.atr import calculate_atr
from broky.risk.circuit_breaker import CircuitBreaker
from broky.risk.drawdown_protection import DrawdownProtector, get_drawdown_config, get_buy_min_confidence
from broky.risk.position_sizing import calculate_position_size
from broky.risk.sizing import SIZING_METHODS, fixed_fraction_size, kelly_size, risk_per_trade_size, volatility_adjusted_size
from broky.risk.spread_filter import check_spread
from broky.signals.m5_scalp_generator import (
    generate_m5_scalp_signal,
    M5_SCALP_SPREAD_MAX,
)
from metty.bridge.client import MT5Bridge
from metty.core.account_registry import get_display_name
from metty.core.db import (
    close_live_trade,
    get_latest_signal_id,
    get_open_trades,
    init_db,
    insert_live_trade,
    insert_rejected_signal,
    reconcile_closed_positions,
)
from shared.events import Event, EventBus, EventType
from shared.logging_utils import log_trade, log_signal, log_position, log_circuit_break
from shared.models import SignalType, TradingMode

logger = logging.getLogger(__name__)

ACCOUNT_IDS = {"A": 1, "B": 2, "C": 3, "D": 4}
CONTRACT_SIZE = 100.0  # 1 lot XAUUSD = 100 oz

# ML filter circuit breaker: stop trading after N consecutive ML failures
ML_MAX_CONSECUTIVE_FAILS = 5


@dataclass
class M5ScalpRiskConfig:
    """Risk configuration for M5 scalping (Ribbon Cloud strategy)."""
    risk_per_trade: float = 0.015          # 1.5% risk
    atr_multiplier: float = 1.5            # SL: 1.5x ATR (wider than M1)
    risk_reward_ratio: float = 2.0          # Target 2:1 R:R
    min_confidence: float = 0.50            # Moderate threshold for M5
    max_holding_bars: int = 12              # 12 bars = 1 hour on M5
    cooldown_bars: int = 6                  # 30 min cooldown (6 * 5 min)
    spread_buffer: float = 1.5
    consecutive_loss_limit: int = 3         # Tighter CB for scalping
    daily_loss_limit_pct: float = 0.03      # 3% daily loss limit
    max_spread_points: float = 30           # Skip if spread > 30 pts
    bar_seconds: int = 300                  # M5 = 300s
    # 4-level TP (ATR multipliers and close percentages)
    tp_levels: list = field(default_factory=lambda: [1.0, 1.5, 2.5, 4.0])
    tp_close_percents: list = field(default_factory=lambda: [0.40, 0.25, 0.25, 0.10])
    sizing_method: str = "risk_per_trade"  # risk_per_trade, kelly, volatility_adjusted, fixed_fraction
    # Partial TP (Option C): close at TP1, open scale-in position
    partial_tp_enabled: bool = False  # Feature flag — must be explicitly enabled
    tp1_ratio: float = 0.5   # TP1 at 50% of TP distance from entry
    rr_scale_in: float = 2.5  # RR ratio for the scale-in position


class M5ScalpTrader:
    """M5 scalping trader using 6-EMA Ribbon Cloud strategy.

    Each account runs its own instance with a persistent bridge connection.
    Uses 4-level ATR-based take profit for position scaling.

    Usage:
        trader = M5ScalpTrader(account="A", db_path="data/oracle.db")
        trader.run_once()  # Single cycle
    """

    def __init__(
        self,
        account: str = "A",
        db_path: Optional[Path] = None,
        data_dir: Optional[Path] = None,
        dry_run: bool = True,
        risk_config: Optional[M5ScalpRiskConfig] = None,
        event_bus: Optional[EventBus] = None,
    ):
        self.account = account.upper()
        self.display_name = get_display_name(self.account)
        self.db_path = db_path
        self.data_dir = data_dir or Path("data/xau-data")
        self.dry_run = dry_run
        self.learning_mode = os.environ.get("LEARNING_MODE", "0") == "1"
        per_account_limits = {
            "A": int(os.environ.get("MAX_POSITIONS_A", os.environ.get("MAX_POSITIONS_PER_ACCOUNT", "5"))),
            "B": int(os.environ.get("MAX_POSITIONS_B", os.environ.get("MAX_POSITIONS_PER_ACCOUNT", "5"))),
            "C": int(os.environ.get("MAX_POSITIONS_C", os.environ.get("MAX_POSITIONS_PER_ACCOUNT", "5"))),
            "D": int(os.environ.get("MAX_POSITIONS_D", os.environ.get("MAX_POSITIONS_PER_ACCOUNT", "5"))),
        }
        self.max_positions = per_account_limits.get(self.account, int(os.environ.get("MAX_POSITIONS_PER_ACCOUNT", "5")))
        self.account_id = ACCOUNT_IDS.get(self.account, 1)
        self.risk = risk_config or M5ScalpRiskConfig()
        # Per-account strategy overrides via env vars (for testing different configs)
        per_account_atr = {
            "A": float(os.environ.get("ATR_MULTIPLIER_A", os.environ.get("ATR_MULTIPLIER", "1.5"))),
            "B": float(os.environ.get("ATR_MULTIPLIER_B", os.environ.get("ATR_MULTIPLIER", "1.5"))),
            "C": float(os.environ.get("ATR_MULTIPLIER_C", os.environ.get("ATR_MULTIPLIER", "1.5"))),
            "D": float(os.environ.get("ATR_MULTIPLIER_D", os.environ.get("ATR_MULTIPLIER", "1.5"))),
        }
        per_account_rr = {
            "A": float(os.environ.get("RR_RATIO_A", os.environ.get("RR_RATIO", "2.0"))),
            "B": float(os.environ.get("RR_RATIO_B", os.environ.get("RR_RATIO", "2.0"))),
            "C": float(os.environ.get("RR_RATIO_C", os.environ.get("RR_RATIO", "2.0"))),
            "D": float(os.environ.get("RR_RATIO_D", os.environ.get("RR_RATIO", "2.0"))),
        }
        per_account_conf = {
            "A": float(os.environ.get("MIN_CONFIDENCE_A", os.environ.get("MIN_CONFIDENCE", "0.50"))),
            "B": float(os.environ.get("MIN_CONFIDENCE_B", os.environ.get("MIN_CONFIDENCE", "0.50"))),
            "C": float(os.environ.get("MIN_CONFIDENCE_C", os.environ.get("MIN_CONFIDENCE", "0.50"))),
            "D": float(os.environ.get("MIN_CONFIDENCE_D", os.environ.get("MIN_CONFIDENCE", "0.50"))),
        }
        per_account_spread = {
            "A": float(os.environ.get("M5_MAX_SPREAD_A", os.environ.get("M5_MAX_SPREAD", "40"))),
            "B": float(os.environ.get("M5_MAX_SPREAD_B", os.environ.get("M5_MAX_SPREAD", "30"))),
            "C": float(os.environ.get("M5_MAX_SPREAD_C", os.environ.get("M5_MAX_SPREAD", "30"))),
            "D": float(os.environ.get("M5_MAX_SPREAD_D", os.environ.get("M5_MAX_SPREAD", "30"))),
        }
        if not risk_config:
            self.risk.atr_multiplier = per_account_atr.get(self.account, self.risk.atr_multiplier)
            self.risk.risk_reward_ratio = per_account_rr.get(self.account, self.risk.risk_reward_ratio)
            self.risk.min_confidence = per_account_conf.get(self.account, self.risk.min_confidence)
            self.risk.max_spread_points = per_account_spread.get(self.account, self.risk.max_spread_points)
        # Partial TP overrides per account
        per_account_ptp = {
            "A": os.environ.get("PARTIAL_TP_ENABLED_A", os.environ.get("PARTIAL_TP_ENABLED", "0")) == "1",
            "B": os.environ.get("PARTIAL_TP_ENABLED_B", os.environ.get("PARTIAL_TP_ENABLED", "0")) == "1",
            "C": os.environ.get("PARTIAL_TP_ENABLED_C", os.environ.get("PARTIAL_TP_ENABLED", "0")) == "1",
            "D": os.environ.get("PARTIAL_TP_ENABLED_D", os.environ.get("PARTIAL_TP_ENABLED", "0")) == "1",
        }
        per_account_tp1r = {
            "A": float(os.environ.get("TP1_RATIO_A", os.environ.get("TP1_RATIO", "0.5"))),
            "B": float(os.environ.get("TP1_RATIO_B", os.environ.get("TP1_RATIO", "0.5"))),
            "C": float(os.environ.get("TP1_RATIO_C", os.environ.get("TP1_RATIO", "0.5"))),
            "D": float(os.environ.get("TP1_RATIO_D", os.environ.get("TP1_RATIO", "0.5"))),
        }
        per_account_rrsi = {
            "A": float(os.environ.get("RR_SCALE_IN_A", os.environ.get("RR_SCALE_IN", "2.5"))),
            "B": float(os.environ.get("RR_SCALE_IN_B", os.environ.get("RR_SCALE_IN", "2.5"))),
            "C": float(os.environ.get("RR_SCALE_IN_C", os.environ.get("RR_SCALE_IN", "2.5"))),
            "D": float(os.environ.get("RR_SCALE_IN_D", os.environ.get("RR_SCALE_IN", "2.5"))),
        }
        self.risk.partial_tp_enabled = per_account_ptp.get(self.account, self.risk.partial_tp_enabled)
        self.risk.tp1_ratio = per_account_tp1r.get(self.account, self.risk.tp1_ratio)
        self.risk.rr_scale_in = per_account_rrsi.get(self.account, self.risk.rr_scale_in)
        # Override sizing method from env if set
        env_sizing = os.environ.get("POSITION_SIZING_METHOD", "").strip()
        if env_sizing and env_sizing in SIZING_METHODS:
            self.risk.sizing_method = env_sizing
        self._sizing_fn = SIZING_METHODS[self.risk.sizing_method]
        self.strategy_id = f"m5-scalp-{self.account}"
        self.circuit_breaker = CircuitBreaker(
            consecutive_loss_limit=self.risk.consecutive_loss_limit,
            daily_loss_limit_pct=self.risk.daily_loss_limit_pct,
        )
        # Drawdown protection (stricter for real accounts)
        dd_config = get_drawdown_config(self.account)
        self._drawdown_protector = DrawdownProtector(
            initial_equity=float(os.environ.get(f"INITIAL_EQUITY_{self.account}", "500")),
            daily_limit_pct=dd_config["daily_limit_pct"],
            weekly_limit_pct=dd_config["weekly_limit_pct"],
            account_limit_pct=dd_config["account_limit_pct"],
            cooldown_hours=dd_config["cooldown_hours"],
        )
        self._buy_min_confidence = float(os.environ.get(
            f"BUY_MIN_CONFIDENCE_{self.account}",
            str(get_buy_min_confidence(self.account)),
        ))
        self._last_exit_time: Optional[datetime] = None
        self._cycle_count: int = 0
        self._calendar_cache: list = []
        self._calendar_cache_time: float = 0
        self._sentiment_cache: dict = {}
        self._sentiment_cache_time: float = 0
        self._mfe_mae_state: dict[int, dict] = {}  # trade_id → {mfe, mae}
        self._last_d1_trend: Optional[str] = None
        self._last_h4_trend: Optional[str] = None
        self._notifier: Optional[object] = None  # Set by main loop if available
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
                logger.info("[M5Scalp:%s] ML filter enabled: %s", self.display_name,
                           "models loaded" if self._ml_predictor.enabled else "no models")
                # Health check: verify ML predictor can actually produce predictions
                if self._ml_predictor.enabled:
                    healthy, reason = self._ml_predictor.health_check()
                    if not healthy:
                        logger.critical("[M5Scalp:%s] ML filter UNHEALTHY: %s — disabling", self.display_name, reason)
                        self._ml_enabled = False
                        self._ml_predictor = None
                    else:
                        logger.info("[M5Scalp:%s] ML filter health check passed: %s", self.display_name, reason)
            except Exception as e:
                logger.warning("[M5Scalp:%s] ML filter init failed: %s", self.display_name, e)
                self._ml_enabled = False

    def _get_account_config(self):
        """Get account config for MT5Bridge from registry (single source of truth)."""
        from metty.core.account_registry import get_bridge_config

        try:
            return get_bridge_config(self.account)
        except ValueError:
            logger.warning("Unknown account: %s, falling back to account A", self.account)
            return get_bridge_config("A")

    def _fetch_candles(self, bridge: MT5Bridge) -> Optional[dict[str, pd.DataFrame]]:
        """Fetch M5 candles from MT5 bridge using an already-connected bridge."""
        try:
            symbol_map = {"A": "XAUUSDm", "B": "XAUUSD", "C": "XAUUSD", "D": "XAUUSD"}
            symbol = symbol_map.get(self.account, "XAUUSD")
            m5 = bridge.fetch_candles_sync(symbol, "M5", 500)

            if m5 is not None and not m5.empty:
                result = {"M5": m5}
                from broky.data.resampler import resample_timeframe
                for tf in ("M15", "H1", "H4"):
                    try:
                        resampled = resample_timeframe(m5, tf)
                        if resampled is not None and not resampled.empty:
                            result[tf] = resampled
                    except Exception:
                        pass
                logger.info("[M5Scalp:%s] Fetched M5: %d bars from MT5", self.display_name, len(m5))
                return result
        except Exception as e:
            logger.warning("[M5Scalp:%s] Bridge candle fetch failed: %s", self.display_name, e)

        return None

    def _check_trend_flips(
        self, d1_trend: str, h4_trend: str | None,
        prev_d1: str | None, prev_h4: str | None,
    ) -> None:
        """Detect D1/H4 trend changes and send Telegram alert + EventBus."""
        from datetime import timezone
        now = datetime.now(timezone.utc).strftime("%H:%M")
        alerts = []

        if prev_d1 is not None and d1_trend != "unknown" and d1_trend != prev_d1:
            direction = "🟢 BULLISH" if d1_trend == "bullish" else "🔴 BEARISH"
            alerts.append(
                f"<b>D1 Trend Flip</b> {now}\n"
                f"Account {self.account}: {prev_d1} → {direction}"
            )
            if self.event_bus:
                self.event_bus.publish(Event(
                    type=EventType.TREND_FLIP,
                    data={
                        "timeframe": "D1",
                        "direction": d1_trend,
                        "old_direction": prev_d1,
                        "symbol": "XAUUSD",
                        "account": self.account,
                    },
                ))

        if prev_h4 is not None and h4_trend and h4_trend != "unknown" and h4_trend != prev_h4:
            direction = "🟢 BULLISH" if h4_trend == "bullish" else "🔴 BEARISH"
            alerts.append(
                f"<b>H4 Trend Flip</b> {now}\n"
                f"Account {self.account}: {prev_h4} → {direction}"
            )
            if self.event_bus:
                self.event_bus.publish(Event(
                    type=EventType.TREND_FLIP,
                    data={
                        "timeframe": "H4",
                        "direction": h4_trend,
                        "old_direction": prev_h4,
                        "symbol": "XAUUSD",
                        "account": self.account,
                    },
                ))

        for msg in alerts:
            try:
                self._notifier.send(msg)
            except Exception:
                pass

    def _compute_d1_trend(self, candles: dict) -> Optional[str]:
        """Compute D1 trend from H1 data (50 EMA as proxy for D1 direction)."""
        from broky.indicators.ema import calculate_ema
        h1 = candles.get("H1")
        if h1 is not None and len(h1) >= 50:
            try:
                ema50 = calculate_ema(h1["close"], 50)
                latest = ema50.iloc[-1]
                latest_price = h1["close"].iloc[-1]
                if pd.notna(latest) and pd.notna(latest_price):
                    if latest_price > latest:
                        return "bullish"
                    elif latest_price < latest:
                        return "bearish"
            except Exception:
                pass
        return None

    def _compute_h4_trend(self, candles: dict) -> Optional[str]:
        """Compute H4 trend using EMA 10/50 crossover."""
        from broky.indicators.ema import calculate_ema
        h4 = candles.get("H4")
        if h4 is not None and len(h4) >= 50:
            try:
                ema10 = calculate_ema(h4["close"], 10).iloc[-1]
                ema50 = calculate_ema(h4["close"], 50).iloc[-1]
                if pd.notna(ema10) and pd.notna(ema50):
                    if ema10 > ema50:
                        return "bullish"
                    elif ema10 < ema50:
                        return "bearish"
            except Exception:
                pass
        return None

    def _get_spread(self, bridge: MT5Bridge) -> Optional[float]:
        """Get current spread from real-time bid/ask using already-connected bridge."""
        try:
            symbol_map = {"A": "XAUUSDm", "B": "XAUUSD", "C": "XAUUSD", "D": "XAUUSD"}
            symbol = symbol_map.get(self.account, "XAUUSD")
            return bridge.get_spread_sync(symbol)
        except Exception as e:
            logger.warning("[M5Scalp:%s] Spread fetch failed: %s", self.display_name, e)
        return None

    def _classify_session(self, timestamp: datetime) -> str:
        """Classify trading session by UTC hour."""
        hour = timestamp.hour
        if 13 <= hour < 16:
            return "overlap"
        if 8 <= hour < 16:
            return "london"
        if 13 <= hour < 22:
            return "ny"
        return "asian"

    def record_trade_result(self, pnl: float, equity: float, is_win: bool) -> None:
        """Record a trade result for circuit breaker and drawdown tracking."""
        if is_win:
            self.circuit_breaker.record_win(pnl)
        else:
            self.circuit_breaker.record_loss(pnl, equity)
        # Update drawdown protection
        self._drawdown_protector.record_pnl(round(pnl, 2), equity)
        self._last_exit_time = datetime.now(timezone.utc)

    def _check_cooldown(self) -> bool:
        """Check if we're in a cooldown period after a trade exit."""
        if self._last_exit_time is None:
            return False
        elapsed = (datetime.now(timezone.utc) - self._last_exit_time).total_seconds()
        cooldown_seconds = self.risk.cooldown_bars * self.risk.bar_seconds
        return elapsed < cooldown_seconds

    def _calculate_lots(self, equity: float, price: float, sl: float, atr: float) -> float:
        """Calculate position size using the configured sizing method."""
        if self.risk.sizing_method == "risk_per_trade":
            return risk_per_trade_size(equity, self.risk.risk_per_trade, price, sl, CONTRACT_SIZE)
        elif self.risk.sizing_method == "kelly":
            from metty.core.db import get_closed_trades
            closed = get_closed_trades(self.account_id, self.db_path, limit=50)
            if len(closed) < 10:
                return risk_per_trade_size(equity, self.risk.risk_per_trade, price, sl, CONTRACT_SIZE)
            wins = [t for t in closed if t.get("pnl", 0) > 0]
            losses = [t for t in closed if t.get("pnl", 0) <= 0]
            win_rate = len(wins) / len(closed) if closed else 0.5
            avg_win = sum(t["pnl"] for t in wins) / len(wins) if wins else 1.0
            avg_loss = abs(sum(t["pnl"] for t in losses) / len(losses)) if losses else 1.0
            return kelly_size(equity, win_rate, avg_win, avg_loss, price, sl, CONTRACT_SIZE)
        elif self.risk.sizing_method == "volatility_adjusted":
            return volatility_adjusted_size(equity, self.risk.risk_per_trade, price, sl, atr, CONTRACT_SIZE)
        elif self.risk.sizing_method == "fixed_fraction":
            return fixed_fraction_size(0.01)
        else:
            return risk_per_trade_size(equity, self.risk.risk_per_trade, price, sl, CONTRACT_SIZE)

    def _check_existing_m5_scalp_position(self) -> bool:
        """Check if there's already an open M5 scalp position for this account.

        Uses MT5 as PRIMARY source of truth — same pattern as LiveTrader.
        If MT5 has no position at all, reconciles DB trades with deal history
        and returns False. If MT5 has a position, checks DB for M5 scalp trades.
        """
        # Step 1: Check MT5 for any open position (source of truth)
        try:
            import rpyc
            from metty.core.account_registry import get_account_config

            cfg = get_account_config(self.account)
            conn = rpyc.connect(cfg.bridge_host, cfg.bridge_internal_port, config={"sync_request_timeout": 10})
            positions_raw = conn.root.positions_get(symbol=cfg.symbol)
            conn.close()

            if positions_raw is None or len(positions_raw) == 0:
                # MT5 says no position — reconcile DB trades with MT5 state
                open_trades = get_open_trades(self.account_id, self.db_path)
                if open_trades:
                    logger.warning(
                        "[M5Scalp:%s] %d open DB trades but no MT5 position — reconciling",
                        self.display_name, len(open_trades),
                    )
                    # Try to get deal history for accurate exit prices
                    deals = self._get_deal_history(days_back=7)
                    # Convert RPyC positions
                    positions_list = []
                    if positions_raw:
                        positions_list = [
                            p if isinstance(p, dict) else dict(p)
                            for p in positions_raw
                        ]
                    closed = reconcile_closed_positions(
                        self.account_id, open_trades,
                        positions_list, deals,
                        self.db_path,
                    )
                    if closed:
                        logger.info("[M5Scalp:%s] Reconciled %d closed positions", self.display_name, closed)
                        # Sync drawdown protector with DB — reconciliation-closed
                        # trades are invisible to in-memory PnL tracking
                        self._drawdown_protector.sync_pnl_from_db(
                            self.account_id, self.db_path,
                        )
                return False

            # MT5 has a position — check if it's an M5 scalp trade in DB
            open_trades = get_open_trades(self.account_id, self.db_path)
            for trade in open_trades:
                strategy = trade.get("strategy_id", "") if "strategy_id" in trade else ""
                mode = trade.get("trading_mode", "") if "trading_mode" in trade else ""
                if strategy == self.strategy_id or mode == "m5_scalp":
                    return True
            return False

        except Exception as e:
            logger.warning("[M5Scalp:%s] MT5 position check failed: %s — falling back to DB", self.display_name, e)
            # Fallback to DB only if MT5 is unreachable
            open_trades = get_open_trades(self.account_id, self.db_path)
            for trade in open_trades:
                strategy = trade.get("strategy_id", "") if "strategy_id" in trade else ""
                mode = trade.get("trading_mode", "") if "trading_mode" in trade else ""
                if strategy == self.strategy_id or mode == "m5_scalp":
                    return True
            return False

    def _get_deal_history(self, days_back: int = 7) -> list[dict]:
        """Fetch MT5 deal history for reconciliation."""
        try:
            from metty.core.account_registry import get_bridge_config
            bridge = MT5Bridge(get_bridge_config(self.account))
            deals = bridge.fetch_deal_history_sync(self.symbol, days_back=days_back)
            return deals or []
        except Exception as e:
            logger.debug("[M5Scalp:%s] Could not fetch deal history: %s", self.display_name, e)
            return []

    def _compute_tp_levels(self, entry_price: float, atr: float, direction: str) -> list[dict]:
        """Compute 4-level TP targets for position scaling.

        Returns list of dicts: [{level, price, close_pct, atr_mult}]
        """
        levels = []
        sign = 1 if direction == "BUY" else -1
        for i, (atr_mult, close_pct) in enumerate(
            zip(self.risk.tp_levels, self.risk.tp_close_percents)
        ):
            tp_price = entry_price + sign * atr * atr_mult
            levels.append({
                "level": i + 1,
                "price": round(tp_price, 2),
                "close_pct": close_pct,
                "atr_mult": atr_mult,
            })
        return levels

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

                port_map = {"A": 5005, "B": 5006, "C": 5007, "D": 5008}
                host = os.environ.get(f"MT5_BRIDGE_{self.account}_HOST", "100.68.106.101")
                port = int(os.environ.get(f"MT5_BRIDGE_{self.account}_PORT", str(port_map[self.account])))

                from metty.core.account_registry import get_bridge_config
                config = get_bridge_config(self.account)
                config = config.model_copy(update={"bridge_host": host, "bridge_port": port})
                bridge = MT5Bridge(config)

                async def _close():
                    if await bridge.connect():
                        await bridge.close_position(trade["ticket"])
                        await bridge.disconnect()

                import asyncio
                asyncio.run(_close())
            except Exception as e:
                logger.warning("[M5Scalp:%s] Failed to close position %s at TP1 in MT5: %s", self.display_name, trade["ticket"], e)

        closed.append({
            "trade_id": trade["id"],
            "direction": direction,
            "exit_reason": "tp1_hit",
            "exit_price": exit_price,
            "pnl": round(pnl, 2),
            "pnl_pct": round(pnl_pct, 4),
        })

        logger.info("[M5Scalp:%s] TP1_CLOSED #%d %s @ %.2f (PnL=%.2f)", self.display_name, trade_id, direction, exit_price, pnl)

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

                port_map = {"A": 5005, "B": 5006, "C": 5007, "D": 5008}
                host = os.environ.get(f"MT5_BRIDGE_{self.account}_HOST", "100.68.106.101")
                port = int(os.environ.get(f"MT5_BRIDGE_{self.account}_PORT", str(port_map[self.account])))

                from metty.core.account_registry import get_bridge_config
                config = get_bridge_config(self.account)
                config = config.model_copy(update={"bridge_host": host, "bridge_port": port})
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
                    logger.error("[M5Scalp:%s] Scale-in order FAILED at TP1: %s", self.display_name, order_result.error)
                    return closed  # Don't insert scale-in if MT5 order failed
            except Exception as e:
                logger.error("[M5Scalp:%s] Scale-in MT5 error at TP1: %s", self.display_name, e)
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
            trading_mode="m5_scalp",
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

        logger.info("[M5Scalp:%s] SCALE_IN #%d %s @ %.2f SL=%.2f TP=%.2f (from #%d)", self.display_name, scale_in_id, direction, current_price, new_sl, tp, trade_id)

        return closed

    def run_once(self) -> dict:
        """Run a single M5 scalping cycle."""
        self._cycle_count += 1
        logger.info("[M5Scalp:%s] Cycle #%d starting", self.display_name, self._cycle_count)
        try:
            return self._run_once_connected()
        except Exception:
            import traceback
            logger.error("[M5Scalp:%s] Traceback:\n%s", self.display_name, traceback.format_exc())
            raise

    def _run_once_connected(self) -> dict:
        """Core M5 scalp logic — one bridge per cycle, reused for all calls."""
        config = self._get_account_config()
        bridge = MT5Bridge(config)

        # 1. Fetch candles
        candles = self._fetch_candles(bridge)
        if not candles or "M5" not in candles:
            return {"action": "skip", "reason": "no M5 candle data"}

        m5 = candles["M5"]
        if len(m5) < 200:
            return {"action": "skip", "reason": f"M5 data too short ({len(m5)} bars)"}

        # Classify session early (needed for rejection recording)
        _ts = m5.index[-1]
        if hasattr(_ts, "to_pydatetime"):
            _ts = _ts.to_pydatetime().replace(tzinfo=timezone.utc)
        session = self._classify_session(_ts)

        # 1b. Monitor existing M5 scalp positions for exits
        closed = self._monitor_positions(candles)

        # Stale data check — skip if last candle is > 30 min old
        if hasattr(m5.index[-1], "to_pydatetime"):
            last_time = m5.index[-1].to_pydatetime().replace(tzinfo=timezone.utc)
        else:
            last_time = m5.index[-1]
        if hasattr(last_time, "timestamp"):
            age_seconds = (datetime.now(timezone.utc) - last_time).total_seconds()
            if age_seconds > 1800:  # 30 minutes
                return {"action": "skip", "reason": f"stale data ({age_seconds:.0f}s old)"}

        # 2. Check for existing M5 scalp position (always enforced — prevents churn)
        if self._check_existing_m5_scalp_position():
            self._record_rejection(None, "existing_m5_scalp_position", session=session, d1_trend=self._last_d1_trend, h4_trend=self._last_h4_trend)
            return {"action": "hold", "reason": "existing M5 scalp position open"}

        # 2b. Position limit check (always enforced, even in learning mode)
        open_trades = get_open_trades(self.account_id, self.db_path)
        if len(open_trades) >= self.max_positions:
            self._record_rejection(None, f"position limit ({len(open_trades)}/{self.max_positions})")
            return {
                "action": "hold",
                "reason": f"position limit ({len(open_trades)}/{self.max_positions})",
            }

        # 3. Cooldown check (learning mode: skip cooldown)
        if self._check_cooldown() and not self.learning_mode:
            self._record_rejection(None, "cooldown", session=session, d1_trend=self._last_d1_trend, h4_trend=self._last_h4_trend)
            return {"action": "hold", "reason": "cooldown after exit"}

        # 4. Spread check (learning mode: skip spread filter, pass spread to generator)
        spread = self._get_spread(bridge)
        if not self.learning_mode:
            if spread is not None and not check_spread(spread, self.risk.max_spread_points):
                self._record_rejection(None, f"spread {spread:.0f} > max {self.risk.max_spread_points:.0f}")
                return {
                    "action": "hold",
                    "reason": f"spread {spread:.0f} > max {self.risk.max_spread_points:.0f}",
                    "spread": spread,
                }
        spread_for_signal = spread if spread is not None else 0.0

        # 5. Session gate (learning mode: skip, trade all sessions for data)
        if session not in ("london", "overlap", "ny") and not self.learning_mode:
            self._record_rejection(None, f"m5 scalp blocked: {session} session", session=session)
            return {"action": "hold", "reason": f"m5 scalp blocked: {session} session"}

        # 6. Compute HTF trends
        d1_trend = self._compute_d1_trend(candles)
        h4_trend = self._compute_h4_trend(candles)

        # Detect trend flips before updating state
        prev_d1 = self._last_d1_trend
        prev_h4 = self._last_h4_trend

        # Store for exit context
        self._last_d1_trend = d1_trend
        self._last_h4_trend = h4_trend

        # Send trend flip alerts
        if self._notifier and self._notifier.enabled:
            self._check_trend_flips(d1_trend, h4_trend, prev_d1, prev_h4)

        # 7. Generate M5 scalp signal (learning_mode passed to bypass generator filters)
        signal = generate_m5_scalp_signal(
            close=m5["close"],
            high=m5["high"],
            low=m5["low"],
            volume=m5["volume"],
            current_price=float(m5["close"].iloc[-1]),
            timestamp=last_time,
            spread=spread_for_signal,
            d1_trend=d1_trend,
            h4_trend=h4_trend,
            min_confidence=self.risk.min_confidence,
            max_spread=self.risk.max_spread_points,
            learning_mode=self.learning_mode,
        )
        signal.strategy_id = self.strategy_id

        if signal.signal_type == SignalType.HOLD:
            return {"action": "hold", "reason": signal.reason, "signal": signal}

        # 7b. BUY confidence filter — require higher confidence for BUY on real accounts
        if signal.signal_type == SignalType.BUY and signal.confidence < self._buy_min_confidence:
            self._record_rejection(signal, f"buy_low_confidence:{signal.confidence:.2f}<{self._buy_min_confidence}", session=session, d1_trend=d1_trend)
            return {"action": "hold", "reason": f"BUY confidence too low: {signal.confidence:.2f} < {self._buy_min_confidence}"}

        # 7c. Drawdown protection check (sync from DB first — reconciliation-closed trades)
        self._drawdown_protector.sync_pnl_from_db(self.account_id, self.db_path)
        balance = self._get_balance()
        dd_can_trade, dd_reason = self._drawdown_protector.check(balance)
        if not dd_can_trade:
            self._record_rejection(signal, f"drawdown:{dd_reason}", session=session, d1_trend=d1_trend)
            return {"action": "hold", "reason": f"drawdown protection: {dd_reason}"}

        # 8. Circuit breaker check (learning mode: skip, trade anyway for data)
        if not self.learning_mode:
            balance = self._get_balance()
            can_trade, cb_reason = self.circuit_breaker.can_open_trade(equity=balance)
            if not can_trade:
                self._record_rejection(signal, f"circuit breaker: {cb_reason}", session=session, d1_trend=d1_trend)
                if self.event_bus:
                    self.event_bus.publish(Event(
                        type=EventType.CIRCUIT_BREAKER_TRIGGERED,
                        data={
                            "account": self.account,
                            "reason": cb_reason,
                            "consecutive_losses": self.circuit_breaker.state.consecutive_losses,
                            "daily_loss_pct": self.circuit_breaker.state.daily_loss_pct,
                            "trading_mode": "m5_scalp",
                        },
                    ))
                return {"action": "hold", "reason": f"circuit breaker: {cb_reason}"}

        # 8.5. ML filter — risk-scale position size based on P(LOSS) prediction
        ml_risk_multiplier = 1.0
        ml_risk_reason: str | None = None
        ml_loss_proba: float | None = None
        ml_model_used: str | None = None
        ml_model_version: str | None = None

        # Circuit breaker: if ML filter has failed too many times, stop trading
        if self._ml_enabled and self._ml_fail_count >= ML_MAX_CONSECUTIVE_FAILS:
            logger.critical(
                "[M5Scalp:%s] ML filter failed %d times consecutively — circuit breaker: holding", self.display_name, self._ml_fail_count,
            )
            self._record_rejection(signal, f"ml_filter_circuit_break:{self._ml_fail_count}_fails", session=session, d1_trend=d1_trend, h4_trend=h4_trend)
            return {"action": "hold", "reason": f"ML circuit breaker ({self._ml_fail_count} consecutive failures)", "signal": signal}

        if self._ml_enabled and self._ml_predictor is not None:
            try:
                from broky.ml.trade_outcome_predictor import compute_features_from_candles

                _sentiment = self._get_sentiment()
                ml_features = compute_features_from_candles(
                    candles, str(signal.signal_type.value),
                    spread=spread_for_signal,
                    d1_trend=d1_trend or "neutral",
                    h4_trend=h4_trend or "unknown",
                    session=session,
                    sentiment=_sentiment,
                )
                # Regime from features (derived from ADX), NOT d1_trend
                regime = ml_features.get("regime", "trending")
                ml_risk_multiplier, ml_risk_reason, ml_loss_proba, ml_model_used = self._ml_predictor.get_risk_multiplier(
                    ml_features, regime, str(signal.signal_type.value),
                )
                # ML filter succeeded — reset failure counter
                self._ml_fail_count = 0

                if ml_risk_multiplier == 0:
                    logger.info("[M5Scalp:%s] ML filter blocked trade: %s", self.display_name, ml_risk_reason)
                    self._record_rejection(signal, ml_risk_reason or "ml_filter_blocked", session=session, d1_trend=d1_trend, h4_trend=h4_trend)
                    return {"action": "hold", "reason": ml_risk_reason, "signal": signal}
                elif ml_risk_multiplier < 1.0:
                    logger.info("[M5Scalp:%s] ML risk-scaling: %s", self.display_name, ml_risk_reason)

            except Exception as e:
                self._ml_fail_count += 1
                logger.error(
                    "[M5Scalp:%s] ML filter crashed (fail %d/%d): %s — proceeding WITHOUT ML protection", self.display_name, self._ml_fail_count, ML_MAX_CONSECUTIVE_FAILS, e,
                )
                # ML filter is down — trade proceeds at full size (1.0) with no ML scaling

        # 9. Calculate ATR for SL and TP levels
        atr_series = calculate_atr(m5["high"], m5["low"], m5["close"], period=10)
        latest_atr = atr_series.iloc[-1] if pd.notna(atr_series.iloc[-1]) else None
        if latest_atr is None or latest_atr <= 0:
            return {"action": "hold", "reason": "ATR not available or zero"}

        # 10. Calculate position size and SL
        sl_distance = float(latest_atr * self.risk.atr_multiplier)
        if signal.signal_type == SignalType.BUY:
            stop_loss = float(signal.price - sl_distance)
            take_profit = float(signal.price + sl_distance * self.risk.risk_reward_ratio)
        else:
            stop_loss = float(signal.price + sl_distance)
            take_profit = float(signal.price - sl_distance * self.risk.risk_reward_ratio)

        # TP1 = tp1_ratio of TP distance (for partial TP tracking)
        tp_distance = abs(take_profit - signal.price)
        if signal.signal_type == SignalType.BUY:
            tp1_price = round(signal.price + tp_distance * self.risk.tp1_ratio, 2)
        else:
            tp1_price = round(signal.price - tp_distance * self.risk.tp1_ratio, 2)

        # Position sizing
        balance = self._get_balance()
        lot_size = float(self._calculate_lots(balance, signal.price, stop_loss, latest_atr))
        lot_size *= ml_risk_multiplier  # ML risk-scaling
        if lot_size < 0.01:
            logger.info("[M5Scalp:%s] ML risk-scaling: lot_size=%.4f < 0.01, skipping", self.account, lot_size)
            self._record_rejection(signal, f"ml_lot_too_small ({lot_size:.4f})", session=session, d1_trend=d1_trend, h4_trend=h4_trend)
            return {"action": "hold", "reason": f"ML risk: lot too small ({lot_size:.4f})", "signal": signal}

        # 8.7. Calendar context for data collection
        minutes_to_next_event, next_event_type, next_event_impact = self._get_calendar_context()

        # 4-level TP calculation
        tp_levels = self._compute_tp_levels(signal.price, latest_atr, signal.signal_type.value)

        # 11. Execute or dry-run
        direction = signal.signal_type.value  # "BUY" or "SELL"

        if self.dry_run:
            # Record dry-run trade in DB for performance tracking
            now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            insert_live_trade(
                account_id=self.account_id,
                timestamp=now_str,
                direction=direction,
                entry_price=signal.price,
                stop_loss=stop_loss,
                take_profit=take_profit,
                lot_size=lot_size,
                confidence=signal.confidence,
                regime=signal.regime or "unknown",
                session=session,
                d1_trend=d1_trend,
                reason=f"dry_run_{signal.reason}",
                trading_mode=TradingMode.M5_SCALP.value,
                strategy_id=self.strategy_id,
                signal_id=get_latest_signal_id(self.account_id, self.db_path),
                atr_at_entry=float(latest_atr) if latest_atr else None,
                indicator_scores_json=json.dumps({**signal.indicators, "h4_trend": h4_trend}) if signal.indicators and h4_trend else (json.dumps(signal.indicators) if signal.indicators else None),
                spread_at_entry=spread if spread and spread > 0 else None,
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
            log_trade(logger, "OPENED", account=self.account, direction=direction,
                     price=signal.price, lots=lot_size, sl=stop_loss, tp=take_profit,
                     confidence=signal.confidence, reason=signal.reason)
            if self.event_bus:
                self.event_bus.publish(Event(
                    type=EventType.TRADE_OPENED,
                    data={
                        "direction": direction,
                        "symbol": "XAUUSD",
                        "price": signal.price,
                        "sl": stop_loss,
                        "tp": take_profit,
                        "lots": lot_size,
                        "confidence": signal.confidence,
                        "regime": signal.regime,
                        "reason": signal.reason,
                        "account": self.account,
                        "trading_mode": "m5_scalp",
                        "tp_levels": tp_levels,
                    },
                ))
            return {
                "action": f"dry_run_{direction.lower()}",
                "price": signal.price,
                "stop_loss": stop_loss,
                "take_profit": take_profit,
                "lot_size": lot_size,
                "confidence": signal.confidence,
                "signal": signal,
                "tp_levels": tp_levels,
                "d1_trend": d1_trend,
                "h4_trend": h4_trend,
            }

        # Live execution
        ref_signal_id = get_latest_signal_id(self.account_id, self.db_path)
        log_trade(logger, "EXECUTING", account=self.account, direction=direction,
                 price=signal.price, lots=lot_size, sl=stop_loss, tp=take_profit,
                 confidence=signal.confidence)
        try:
            # Retry order up to 3 times using same bridge
            result = None
            for attempt in range(1, 4):
                result = bridge.send_order_sync("XAUUSD", direction, lot_size, stop_loss, take_profit)
                if result and result.get("success"):
                    break
                logger.warning(
                    "[M5Scalp:%s] Order attempt %d failed: %s", self.display_name, attempt, result.get("error", "unknown") if result else "no result",
                )
                if attempt < 3:
                    time.sleep(1)

            ticket = result.get("ticket") if result and result.get("success") else None

            ts_str = (
                m5.index[-1].isoformat()
                if hasattr(m5.index[-1], "isoformat")
                else str(m5.index[-1])
            )

            # Build indicator scores JSON for debugging
            # Include h4_trend so it's available in features_json during backfill
            import json as _json
            if signal.indicators:
                _scores = dict(signal.indicators)
                if h4_trend and h4_trend != "unknown":
                    _scores["h4_trend"] = h4_trend
                indicator_scores_json = _json.dumps(_scores)
            else:
                indicator_scores_json = None

            insert_live_trade(
                account_id=self.account_id,
                timestamp=ts_str,
                direction=direction,
                entry_price=signal.price,
                stop_loss=stop_loss,
                take_profit=take_profit,
                lot_size=lot_size,
                confidence=signal.confidence,
                regime=signal.regime or "unknown",
                session=session,
                d1_trend=d1_trend,
                reason=signal.reason,
                ticket=ticket,
                trading_mode=TradingMode.M5_SCALP.value,
                strategy_id=self.strategy_id,
                signal_id=ref_signal_id,
                atr_at_entry=float(latest_atr) if latest_atr else None,
                indicator_scores_json=indicator_scores_json,
                spread_at_entry=spread if spread and spread > 0 else None,
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

            if result and result.get("success"):
                log_trade(logger, "FILLED", account=self.account, direction=direction,
                         price=signal.price, lots=lot_size, sl=stop_loss, tp=take_profit,
                         ticket=ticket)
                if self.event_bus:
                    self.event_bus.publish(Event(
                        type=EventType.TRADE_OPENED,
                        data={
                            "direction": direction, "symbol": "XAUUSD", "price": signal.price,
                            "sl": stop_loss, "tp": take_profit, "lots": lot_size,
                            "confidence": signal.confidence,
                            "regime": signal.regime or "unknown", "reason": signal.reason,
                            "account": self.account, "trading_mode": "m5_scalp", "ticket": ticket,
                            "tp_levels": tp_levels,
                        },
                    ))
            else:
                error = result.get("error", "unknown") if result else "bridge connection failed"
                logger.error("[M5Scalp:%s] ORDER FAILED: %s — %s", self.display_name, direction, error)

            return {
                "action": "executed" if (result and result.get("success")) else "order_failed",
                "direction": direction,
                "price": signal.price,
                "sl": stop_loss,
                "tp": take_profit,
                "lots": lot_size,
                "confidence": signal.confidence,
                "regime": signal.regime,
                "ticket": ticket,
                "strategy_id": self.strategy_id,
            }
        except Exception as e:
            logger.error("[M5Scalp:%s] Live execution error: %s", self.display_name, e)
            return {"action": "error", "reason": str(e)}

    def _get_balance(self) -> float:
        """Get account balance from DB or bridge."""
        conn = None
        try:
            from metty.core.db import get_connection
            conn = get_connection(self.db_path)
            row = conn.execute(
                "SELECT balance FROM accounts WHERE name = ?",
                (self.account,),
            ).fetchone()
            if row:
                return float(row[0])
        except Exception as e:
            logger.warning("[M5Scalp:%s] Balance fetch failed: %s", self.display_name, e)
        finally:
            if conn:
                conn.close()
        return 0.0

    def _get_sentiment(self) -> dict:
        """Get sentiment data with 15-minute cache."""
        now = time.time()
        if now - self._sentiment_cache_time > 900:
            try:
                from metty.execution.live_collector import fetch_live_sentiment
                self._sentiment_cache = fetch_live_sentiment()
            except Exception as e:
                logger.warning("[M5Scalp:%s] Sentiment fetch failed: %s", self.display_name, e)
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
                trading_mode=TradingMode.M5_SCALP.value,
                strategy_id=self.strategy_id,
                regime=signal.regime if signal else "unknown",
                session=session,
                d1_trend=d1_trend,
                db_path=self.db_path,
            )
        except Exception as e:
            logger.warning("[M5Scalp:%s] Rejected signal recording failed: %s", self.display_name, e)

    def _monitor_positions(self, candles: dict[str, pd.DataFrame]) -> list[dict]:
        """Check open M5 scalp trades for exit conditions and close them in DB."""
        open_trades = get_open_trades(self.account_id, self.db_path)
        closed = []

        if not open_trades or "M5" not in candles:
            return closed

        m5 = candles["M5"]
        current_price = float(m5["close"].iloc[-1])
        current_high = float(m5["high"].iloc[-1]) if "high" in m5.columns else current_price
        current_low = float(m5["low"].iloc[-1]) if "low" in m5.columns else current_price
        now_str = datetime.now(timezone.utc).isoformat()

        for trade in open_trades:
            # Only manage M5 scalp trades
            strategy = trade.get("strategy_id", "") if "strategy_id" in trade else ""
            mode = trade.get("trading_mode", "") if "trading_mode" in trade else ""
            if strategy != self.strategy_id and mode != "m5_scalp":
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

            # Check max holding time (12 M5 bars = 1 hour)
            if exit_reason is None:
                try:
                    entry_time = pd.Timestamp(trade["timestamp"])
                    if hasattr(m5.index[-1], "to_pydatetime"):
                        now_ts = m5.index[-1].to_pydatetime().replace(tzinfo=None)
                    else:
                        now_ts = m5.index[-1]
                    bars_held = 0
                    try:
                        entry_naive = entry_time.tz_localize(None) if hasattr(entry_time, "tz_localize") else entry_time
                        bars_held = len(m5[m5.index > entry_naive])
                    except Exception:
                        pass
                    if self.risk.max_holding_bars > 0 and bars_held >= self.risk.max_holding_bars:
                        exit_reason = "max_holding"
                except Exception:
                    pass

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
                    "trade_id": trade_id,
                    "direction": direction,
                    "exit_reason": exit_reason,
                    "exit_price": exit_price,
                    "pnl": round(pnl, 2),
                    "pnl_pct": round(pnl_pct, 4),
                })
                logger.info(
                    "[M5Scalp:%s] Trade #%d closed: %s @ %.2f → %.2f (%s, PnL=%.2f)",
                    self.account, trade_id, direction, entry_price, exit_price, exit_reason, pnl,
                )

                if self.event_bus:
                    self.event_bus.publish(Event(
                        type=EventType.TRADE_CLOSED,
                        data={
                            "direction": direction, "symbol": "XAUUSD",
                            "entry_price": entry_price, "exit_price": exit_price,
                            "pnl": round(pnl, 2), "pnl_pct": round(pnl_pct, 4),
                            "exit_reason": exit_reason, "account": self.account,
                            "trading_mode": "m5_scalp",
                        },
                    ))

        return closed