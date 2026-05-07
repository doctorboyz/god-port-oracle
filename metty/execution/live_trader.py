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
    get_open_trades,
    init_db,
    insert_live_trade,
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
    ):
        self.account = account.upper()
        self.db_path = db_path
        self.data_dir = data_dir or Path("data/xau-data")
        self.dry_run = dry_run
        self.learning_mode = os.environ.get("LEARNING_MODE", "0") == "1"
        self.max_positions = int(os.environ.get("MAX_POSITIONS_PER_ACCOUNT", "5"))
        self.account_id = ACCOUNT_IDS.get(self.account, 3)
        self.risk = risk_config or RiskConfig(
            risk_per_trade=ACCOUNT_RISK.get(self.account, 0.02),
        )
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
        self._last_exit_time: Optional[datetime] = None
        self._cycle_count: int = 0
        self.strategy_id = f"swing-{self.account}"
        self.event_bus = event_bus

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

            for tf in ["M5", "H1", "D1"]:
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

                # Close in DB
                close_live_trade(
                    trade_id=trade["id"],
                    exit_price=exit_price,
                    exit_time=now_str,
                    pnl=round(pnl, 2),
                    pnl_pct=round(pnl_pct, 4),
                    exit_reason=exit_reason,
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

        # 4. Risk checks (learning mode: bypass all blockers for data collection)
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
            return {
                "action": "hold",
                "reason": f"position limit ({len(open_trades)}/{self.max_positions})",
                "signal": signal,
            }

        if not self.learning_mode:
            can_trade, cb_reason = self.circuit_breaker.can_open_trade()
            if not can_trade:
                log_circuit_break(logger, "BLOCKED", account=self.account, reason=cb_reason)
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
                    "reason": "cooldown after last exit",
                    "signal": signal,
                }

            calendar = self._get_calendar()
            if should_avoid_trading(calendar):
                return {
                    "action": "hold",
                    "reason": "high-impact news nearby",
                    "signal": signal,
                }

            if self._check_existing_position():
                return {
                    "action": "hold",
                    "reason": "position already open",
                    "signal": signal,
                }

        # 5. Calculate SL/TP/lots
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

        equity = self._get_equity()
        lots = self._calculate_lots(equity, price, sl, atr_val)

        # 6. Execute or dry-run
        ts_str = (
            m5.index[-1].isoformat()
            if hasattr(m5.index[-1], "isoformat")
            else str(m5.index[-1])
        )

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
                db_path=self.db_path,
            )
            log_trade(logger, "OPENED", account=self.account, direction=direction,
                     price=price, lots=lots, sl=sl, tp=tp,
                     confidence=signal.confidence, reason=signal.reason)
            if self.event_bus:
                self.event_bus.publish(Event(
                    type=EventType.TRADE_OPENED,
                    data={
                        "direction": direction, "symbol": signal.symbol, "price": price,
                        "sl": sl, "tp": tp, "lots": lots, "confidence": signal.confidence,
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