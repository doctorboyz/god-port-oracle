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
    get_open_trades,
    init_db,
    insert_live_trade,
)
from shared.events import Event, EventBus, EventType
from shared.models import SignalType, TradingMode

logger = logging.getLogger(__name__)

# Account IDs in the database
ACCOUNT_IDS = {"A": 1, "B": 2, "C": 3}

# Contract size: 1 lot XAUUSD = 100 oz
CONTRACT_SIZE = 100.0


@dataclass
class ScalpRiskConfig:
    """Risk configuration for M1 scalping."""
    risk_per_trade: float = 0.01       # 1% (conservative for high freq)
    atr_multiplier: float = 1.0        # Tight SL: 1x ATR (M1 ATR ~1-3 pts)
    risk_reward_ratio: float = 1.5     # Smaller TP target
    min_confidence: float = 0.55       # Lower threshold (M1 is noisier)
    max_holding_bars: int = 20         # 20 min max hold
    cooldown_bars: int = 3             # 3 min cooldown
    spread_buffer: float = 1.5          # Tighter buffer
    consecutive_loss_limit: int = 5    # Same as swing
    daily_loss_limit_pct: float = 0.03  # 3% (tighter than 5% for scalping)
    max_spread_points: float = 30      # Skip if spread > 30 pts
    bar_seconds: int = 60              # M1 = 60s


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
        self.strategy_id = f"scalp-{self.account}"
        self.circuit_breaker = CircuitBreaker(
            consecutive_loss_limit=self.risk.consecutive_loss_limit,
            daily_loss_limit_pct=self.risk.daily_loss_limit_pct,
        )
        self._calendar_cache: list = []
        self._calendar_cache_time: float = 0
        self._last_exit_time: Optional[datetime] = None
        self._cycle_count: int = 0
        self._bridge = None  # PersistentMT5Bridge, initialized lazily
        self.event_bus = event_bus

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

    def _fetch_candles(self) -> Optional[dict[str, pd.DataFrame]]:
        """Fetch M1 candle data from persistent bridge, fall back to CSV."""
        bridge = self._get_bridge()
        if bridge is not None:
            try:
                from broky.data.resampler import resample_timeframe
                from metty.execution.historical_collector import _normalize_columns

                if bridge.ensure_connected_sync():
                    # Fetch M1 candles (primary timeframe for scalping)
                    m1 = bridge.fetch_candles_persistent_sync("XAUUSD", "M1", 500)
                    if m1 is not None and not m1.empty:
                        m1 = _normalize_columns(m1)
                        candles = {"M1": m1}
                        # Resample for higher TFs
                        for tf in ["M5", "M15", "H1"]:
                            try:
                                candles[tf] = _normalize_columns(
                                    resample_timeframe(m1.reset_index(), tf)
                                )
                            except Exception:
                                pass
                        logger.info("Fetched M1 candles from bridge: %d bars", len(m1))
                        return candles
            except Exception as e:
                logger.warning("Bridge M1 fetch failed: %s", e)

        # Fallback to CSV
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
        """Get current spread from bridge symbol info."""
        bridge = self._get_bridge()
        if bridge is None:
            return 0.0

        try:
            if bridge.ensure_connected_sync():
                info = bridge.get_symbol_info_sync("XAUUSD")
                if info and "spread" in info:
                    return float(info["spread"])
        except Exception as e:
            logger.warning("Spread fetch failed: %s", e)

        # Fallback: estimate from last candle high-low
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
        now_str = datetime.now(timezone.utc).isoformat()

        for trade in open_trades:
            # Only manage scalp trades
            strategy = trade.get("strategy_id", "") if "strategy_id" in trade else ""
            mode = trade.get("trading_mode", "") if "trading_mode" in trade else ""
            if strategy != self.strategy_id and mode != "scalp":
                continue

            direction = trade["direction"]
            entry_price = trade["entry_price"]
            sl = trade["stop_loss"]
            tp = trade["take_profit"]
            lot_size = trade["lot_size"]

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

                close_live_trade(
                    trade_id=trade["id"],
                    exit_price=exit_price,
                    exit_time=now_str,
                    pnl=round(pnl, 2),
                    pnl_pct=round(pnl_pct, 4),
                    exit_reason=exit_reason,
                    db_path=self.db_path,
                )

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

        # Session gate: only trade London + Overlap
        if session not in ("london", "overlap"):
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
            return {
                "action": "hold",
                "reason": "cooldown after last scalp exit",
                "signal": signal,
            }

        if self._check_existing_scalp_position():
            return {
                "action": "hold",
                "reason": "scalp position already open",
                "signal": signal,
            }

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

        equity = self._get_equity()
        lots = calculate_position_size(
            equity, self.risk.risk_per_trade, signal.price, sl, CONTRACT_SIZE,
        )

        # 8. Execute or dry-run
        ts_str = (
            m1.index[-1].isoformat()
            if hasattr(m1.index[-1], "isoformat")
            else str(m1.index[-1])
        )
        m1_trend = self._determine_m1_trend(m1)

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
                d1_trend=m1_trend,
                reason=signal.reason,
                ticket=None,
                trading_mode=TradingMode.SCALP.value,
                strategy_id=self.strategy_id,
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
                d1_trend=m1_trend,
                reason=signal.reason,
                ticket=ticket,
                trading_mode=TradingMode.SCALP.value,
                strategy_id=self.strategy_id,
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