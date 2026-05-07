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
from broky.risk.position_sizing import calculate_position_size
from broky.risk.sizing import SIZING_METHODS, fixed_fraction_size, kelly_size, risk_per_trade_size, volatility_adjusted_size
from broky.risk.spread_filter import check_spread
from broky.signals.m5_scalp_generator import (
    generate_m5_scalp_signal,
    M5_SCALP_SPREAD_MAX,
)
from metty.bridge.client import MT5Bridge
from metty.core.db import (
    close_live_trade,
    get_open_trades,
    init_db,
    insert_live_trade,
)
from shared.events import Event, EventBus, EventType
from shared.logging_utils import log_trade, log_signal, log_position, log_circuit_break
from shared.models import SignalType, TradingMode

logger = logging.getLogger(__name__)

ACCOUNT_IDS = {"A": 1, "B": 2, "C": 3}
CONTRACT_SIZE = 100.0  # 1 lot XAUUSD = 100 oz


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
        self.db_path = db_path
        self.data_dir = data_dir or Path("data/xau-data")
        self.dry_run = dry_run
        self.learning_mode = os.environ.get("LEARNING_MODE", "0") == "1"
        self.max_positions = int(os.environ.get("MAX_POSITIONS_PER_ACCOUNT", "5"))
        self.account_id = ACCOUNT_IDS.get(self.account, 1)
        self.risk = risk_config or M5ScalpRiskConfig()
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
        self._last_exit_time: Optional[datetime] = None
        self._cycle_count: int = 0
        self.event_bus = event_bus

    def _get_account_config(self):
        """Get account config for MT5Bridge (same pattern as LiveCollector)."""
        from metty.core.models import AccountConfig, AccountName

        account_configs = {
            "A": AccountConfig(
                name=AccountName.A,
                broker_login=os.environ.get("MT5_LOGIN_A", ""),
                broker_server=os.environ.get("MT5_SERVER_A", "Exness-MT5Trial17"),
                balance=100.0, leverage=2000,
                bridge_host=os.environ.get("MT5_BRIDGE_A_HOST", "mt5a"),
                bridge_port=int(os.environ.get("MT5_BRIDGE_A_PORT", "8001")),
                signal_group="volume",
            ),
            "B": AccountConfig(
                name=AccountName.B,
                broker_login=os.environ.get("MT5_LOGIN_B", ""),
                broker_server=os.environ.get("MT5_SERVER_B", "Exness-MT5Trial17"),
                balance=500.0, leverage=500,
                bridge_host=os.environ.get("MT5_BRIDGE_B_HOST", "mt5b"),
                bridge_port=int(os.environ.get("MT5_BRIDGE_B_PORT", "8001")),
                signal_group="ob_os",
            ),
            "C": AccountConfig(
                name=AccountName.C,
                broker_login=os.environ.get("MT5_LOGIN_C", ""),
                broker_server=os.environ.get("MT5_SERVER_C", "Exness-MT5Trial7"),
                balance=1000.0, leverage=500,
                bridge_host=os.environ.get("MT5_BRIDGE_C_HOST", "mt5c"),
                bridge_port=int(os.environ.get("MT5_BRIDGE_C_PORT", "8001")),
                signal_group="ma",
            ),
        }
        return account_configs.get(self.account, account_configs["A"])

    def _fetch_candles(self, bridge: MT5Bridge = None) -> Optional[dict[str, pd.DataFrame]]:
        """Fetch M5 candles from MT5Bridge. Reuses bridge if provided."""
        try:
            symbol_map = {"A": "XAUUSDm", "B": "XAUUSD", "C": "XAUUSD"}
            symbol = symbol_map.get(self.account, "XAUUSD")

            if bridge:
                m5 = bridge.fetch_candles_sync(symbol, "M5", 500)
            else:
                config = self._get_account_config()
                bridge = MT5Bridge(config)
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
                logger.info("[M5Scalp:%s] Fetched M5: %d bars from MT5", self.account, len(m5))
                return result
        except Exception as e:
            logger.warning("[M5Scalp:%s] Bridge candle fetch failed: %s", self.account, e)

        return None

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

    def _get_spread(self, bridge: MT5Bridge = None) -> Optional[float]:
        """Get current spread from MT5Bridge symbol info. Reuses bridge if provided."""
        try:
            symbol_map = {"A": "XAUUSDm", "B": "XAUUSD", "C": "XAUUSD"}
            symbol = symbol_map.get(self.account, "XAUUSD")

            if bridge:
                info = bridge.get_symbol_info_sync(symbol)
            else:
                config = self._get_account_config()
                bridge = MT5Bridge(config)
                info = bridge.get_symbol_info_sync(symbol)

            if info and "spread" in info:
                spread_val = float(info["spread"])
                if spread_val > 0:
                    return spread_val
        except Exception as e:
            logger.debug("[M5Scalp:%s] Spread fetch failed: %s", self.account, e)
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
        """Record a trade result for circuit breaker tracking."""
        if is_win:
            self.circuit_breaker.record_win(pnl)
        else:
            self.circuit_breaker.record_loss(pnl, equity)
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
        """Check if there's already an open M5 scalp position for this account."""
        try:
            conn = None
            try:
                from metty.core.db import get_connection
                conn = get_connection(self.db_path)
                rows = conn.execute(
                    """SELECT id, strategy_id, trading_mode FROM live_trades
                       WHERE is_open = 1 AND account_id = ?""",
                    (self.account_id,),
                ).fetchall()
                for row in rows:
                    strategy = row[1] if len(row) > 1 else ""
                    mode = row[2] if len(row) > 2 else ""
                    if strategy == self.strategy_id or mode == "m5_scalp":
                        return True
            finally:
                if conn:
                    conn.close()
        except Exception as e:
            logger.error("[M5Scalp:%s] Position check error: %s", self.account, e)
        return False

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

    def run_once(self) -> dict:
        """Run a single M5 scalping cycle."""
        self._cycle_count += 1
        logger.info("[M5Scalp:%s] Cycle #%d starting", self.account, self._cycle_count)

        # Create one bridge per cycle (reuse across fetch, spread, and execution)
        bridge = MT5Bridge(self._get_account_config())

        # 1. Fetch candles
        candles = self._fetch_candles(bridge)
        if not candles or "M5" not in candles:
            return {"action": "skip", "reason": "no M5 candle data"}

        m5 = candles["M5"]
        if len(m5) < 200:
            return {"action": "skip", "reason": f"M5 data too short ({len(m5)} bars)"}

        # Stale data check — skip if last candle is > 30 min old
        if hasattr(m5.index[-1], "to_pydatetime"):
            last_time = m5.index[-1].to_pydatetime().replace(tzinfo=timezone.utc)
        else:
            last_time = m5.index[-1]
        if hasattr(last_time, "timestamp"):
            age_seconds = (datetime.now(timezone.utc) - last_time).total_seconds()
            if age_seconds > 1800:  # 30 minutes
                return {"action": "skip", "reason": f"stale data ({age_seconds:.0f}s old)"}

        # 2. Check for existing position (learning mode: skip, allow multiple positions)
        if self._check_existing_m5_scalp_position() and not self.learning_mode:
            return {"action": "hold", "reason": "existing M5 scalp position open"}

        # 2b. Position limit check (always enforced, even in learning mode)
        open_trades = get_open_trades(self.account_id, self.db_path)
        if len(open_trades) >= self.max_positions:
            return {
                "action": "hold",
                "reason": f"position limit ({len(open_trades)}/{self.max_positions})",
            }

        # 3. Cooldown check (learning mode: skip cooldown)
        if self._check_cooldown() and not self.learning_mode:
            return {"action": "hold", "reason": "cooldown after exit"}

        # 4. Spread check (learning mode: skip spread filter, pass spread to generator)
        spread = self._get_spread(bridge)
        if not self.learning_mode:
            if spread is not None and not check_spread(spread, self.risk.max_spread_points):
                return {
                    "action": "hold",
                    "reason": f"spread {spread:.0f} > max {self.risk.max_spread_points:.0f}",
                    "spread": spread,
                }
        spread_for_signal = spread

        # 5. Session gate (learning mode: skip, trade all sessions for data)
        timestamp = m5.index[-1]
        if hasattr(timestamp, "to_pydatetime"):
            timestamp = timestamp.to_pydatetime().replace(tzinfo=timezone.utc)
        session = self._classify_session(timestamp)
        if session not in ("london", "overlap", "ny") and not self.learning_mode:
            return {"action": "hold", "reason": f"m5 scalp blocked: {session} session"}

        # 6. Compute HTF trends
        d1_trend = self._compute_d1_trend(candles)
        h4_trend = self._compute_h4_trend(candles)

        # 7. Generate M5 scalp signal (learning_mode passed to bypass generator filters)
        signal = generate_m5_scalp_signal(
            close=m5["close"],
            high=m5["high"],
            low=m5["low"],
            volume=m5["volume"],
            current_price=float(m5["close"].iloc[-1]),
            timestamp=timestamp,
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

        # 8. Circuit breaker check (learning mode: skip, trade anyway for data)
        if not self.learning_mode:
            balance = self._get_balance()
            can_trade, cb_reason = self.circuit_breaker.can_open_trade(equity=balance)
            if not can_trade:
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

        # 9. Calculate ATR for SL and TP levels
        atr_series = calculate_atr(m5["high"], m5["low"], m5["close"], period=10)
        latest_atr = atr_series.iloc[-1] if pd.notna(atr_series.iloc[-1]) else None
        if latest_atr is None or latest_atr <= 0:
            return {"action": "hold", "reason": "ATR not available or zero"}

        # 10. Calculate position size and SL
        sl_distance = latest_atr * self.risk.atr_multiplier
        if signal.signal_type == SignalType.BUY:
            stop_loss = signal.price - sl_distance
            take_profit = signal.price + sl_distance * self.risk.risk_reward_ratio
        else:
            stop_loss = signal.price + sl_distance
            take_profit = signal.price - sl_distance * self.risk.risk_reward_ratio

        # Position sizing
        balance = self._get_balance()
        lot_size = self._calculate_lots(balance, signal.price, stop_loss, latest_atr)

        # 4-level TP calculation
        tp_levels = self._compute_tp_levels(signal.price, latest_atr, signal.signal_type.value)

        # 11. Execute or dry-run
        direction = signal.signal_type.value  # "BUY" or "SELL"

        if self.dry_run:
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
        log_trade(logger, "EXECUTING", account=self.account, direction=direction,
                 price=signal.price, lots=lot_size, sl=stop_loss, tp=take_profit,
                 confidence=signal.confidence)
        try:
            # Retry order up to 3 times (bridge may need reconnect)
            result = None
            for attempt in range(1, 4):
                result = bridge.send_order_sync("XAUUSD", direction, lot_size, stop_loss, take_profit)
                if result and result.get("success"):
                    break
                logger.warning(
                    "[M5Scalp:%s] Order attempt %d failed: %s",
                    self.account, attempt, result.get("error", "unknown") if result else "no result",
                )
                if attempt < 3:
                    time.sleep(1)

            ticket = result.get("ticket") if result and result.get("success") else None

            ts_str = (
                m5.index[-1].isoformat()
                if hasattr(m5.index[-1], "isoformat")
                else str(m5.index[-1])
            )

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
                            "direction": direction, "symbol": symbol, "price": signal.price,
                            "sl": stop_loss, "tp": take_profit, "lots": lot_size,
                            "confidence": signal.confidence,
                            "regime": signal.regime or "unknown", "reason": signal.reason,
                            "account": self.account, "trading_mode": "m5_scalp", "ticket": ticket,
                            "tp_levels": tp_levels,
                        },
                    ))
            else:
                error = result.get("error", "unknown") if result else "bridge connection failed"
                logger.error("[M5Scalp:%s] ORDER FAILED: %s — %s", self.account, direction, error)

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
            logger.error("[M5Scalp:%s] Live execution error: %s", self.account, e)
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
            logger.warning("[M5Scalp:%s] Balance fetch failed: %s", self.account, e)
        finally:
            if conn:
                conn.close()
        return 0.0