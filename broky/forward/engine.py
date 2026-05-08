"""Forward test engine — simulates real-time trading on historical data with walk-forward logic.

Uses the same signal generation as backtest but processes one candle at a time,
tracking equity, positions, and generating a trade log for weekly reporting.

Best config (Phase 1.5):
    min_confidence=0.55, atr_multiplier=2.0, risk_reward_ratio=2.5,
    max_holding_bars=24, MTF=hard, Asian=0.70, Overlap=1.10
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import pandas as pd

from shared.models import Signal, SignalType, MarketRegime
from broky.signals.generator import generate_signal
from broky.risk.circuit_breaker import CircuitBreaker
from broky.risk.position_sizing import calculate_stop_loss, calculate_take_profit, calculate_position_size
from broky.indicators.atr import calculate_atr
from broky.data.loader import load_timeframe
from broky.data.resampler import resample_timeframe
from broky.indicators.ema import calculate_ema

logger = logging.getLogger(__name__)

# Best config from Phase 1.5 optimization
BEST_CONFIG = {
    "initial_equity": 1000.0,
    "risk_per_trade": 0.02,
    "atr_multiplier": 2.0,
    "risk_reward_ratio": 2.5,
    "min_confidence": 0.55,
    "max_holding_bars": 24,
    "cooldown_bars": 12,
    "contract_size": 100.0,
}


@dataclass
class ForwardTrade:
    """A single trade in a forward test."""
    trade_id: int
    entry_time: datetime
    entry_price: float
    direction: SignalType
    lot_size: float
    stop_loss: float
    take_profit: float
    confidence: float
    regime: str
    session: str
    exit_time: Optional[datetime] = None
    exit_price: Optional[float] = None
    pnl: float = 0.0
    pnl_pct: float = 0.0
    exit_reason: str = ""
    holding_bars: int = 0


@dataclass
class ForwardResult:
    """Results of a forward test run."""
    symbol: str = "XAUUSD"
    timeframe: str = "H1"
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    initial_equity: float = 1000.0
    final_equity: float = 0.0
    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    win_rate: float = 0.0
    total_pnl: float = 0.0
    total_pnl_pct: float = 0.0
    profit_factor: float = 0.0
    max_drawdown_pct: float = 0.0
    sharpe_ratio: float = 0.0
    avg_trade_pnl: float = 0.0
    max_consecutive_wins: int = 0
    max_consecutive_losses: int = 0
    trades: list[ForwardTrade] = field(default_factory=list)
    equity_curve: list[float] = field(default_factory=list)
    weekly_reports: list[dict] = field(default_factory=list)


class ForwardEngine:
    """Walk-forward paper trading engine.

    Processes historical data candle-by-candle, generating signals and
    managing positions as if in real-time. Designed for 4-week forward
    testing before live trading.

    Example:
        >>> engine = ForwardEngine(initial_equity=1000, risk_per_trade=0.02)
        >>> result = engine.run(df_h1, df_d1=df_d1)
        >>> print(f"Win rate: {result.win_rate:.1%}")
    """

    def __init__(
        self,
        initial_equity: float = BEST_CONFIG["initial_equity"],
        risk_per_trade: float = BEST_CONFIG["risk_per_trade"],
        atr_multiplier: float = BEST_CONFIG["atr_multiplier"],
        risk_reward_ratio: float = BEST_CONFIG["risk_reward_ratio"],
        min_confidence: float = BEST_CONFIG["min_confidence"],
        max_holding_bars: int = BEST_CONFIG["max_holding_bars"],
        cooldown_bars: int = BEST_CONFIG["cooldown_bars"],
        contract_size: float = BEST_CONFIG["contract_size"],
        spread_buffer: float = 2.0,
    ):
        self.initial_equity = initial_equity
        self.risk_per_trade = risk_per_trade
        self.atr_multiplier = atr_multiplier
        self.risk_reward_ratio = risk_reward_ratio
        self.min_confidence = min_confidence
        self.max_holding_bars = max_holding_bars
        self.cooldown_bars = cooldown_bars
        self.contract_size = contract_size
        self.spread_buffer = spread_buffer

    def run(
        self,
        df: pd.DataFrame,
        warmup: int = 50,
        d1_df: Optional[pd.DataFrame] = None,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
    ) -> ForwardResult:
        """Run forward test on historical data.

        Args:
            df: H1 DataFrame with OHLCV data and DatetimeIndex.
            warmup: Number of initial candles to skip for indicator calculation.
            d1_df: Optional D1 DataFrame for multi-timeframe trend filter.
            start_date: Optional start date (ISO format) for the test period.
            end_date: Optional end date (ISO format) for the test period.

        Returns:
            ForwardResult with performance metrics and trade list.
        """
        from broky.indicators.atr import calculate_atr

        # Filter date range
        if start_date:
            df = df[df.index >= pd.Timestamp(start_date)]
        if end_date:
            df = df[df.index <= pd.Timestamp(end_date)]

        if len(df) < warmup + 10:
            logger.warning("Insufficient data after date filtering")
            return ForwardResult()

        equity = self.initial_equity
        equity_curve = [equity]
        trades: list[ForwardTrade] = []
        circuit_breaker = CircuitBreaker()
        trade_id = 0

        atr = calculate_atr(df["High"], df["Low"], df["Close"], period=14)
        d1_trend_series = self._compute_d1_trend(df, d1_df)

        position: Optional[ForwardTrade] = None
        consecutive_wins = 0
        consecutive_losses = 0
        max_consecutive_wins = 0
        max_consecutive_losses = 0
        gross_profit = 0.0
        gross_loss = 0.0
        peak_equity = equity
        last_exit_idx = -self.cooldown_bars - 1

        for i in range(warmup, len(df)):
            close_slice = df["Close"].iloc[:i+1]
            high_slice = df["High"].iloc[:i+1]
            low_slice = df["Low"].iloc[:i+1]
            volume_slice = df["Volume"].iloc[:i+1]

            current_price = float(df["Close"].iloc[i])
            current_atr = float(atr.iloc[i]) if pd.notna(atr.iloc[i]) else 5.0

            # Check exit on existing position
            if position is not None:
                position, equity, closed = self._check_exit(
                    position, df, i, current_price, equity,
                )
                if closed:
                    trade_id += 1
                    position.trade_id = trade_id
                    position.exit_time = df.index[i].to_pydatetime().replace(tzinfo=timezone.utc)
                    position.exit_price = position.exit_price or current_price
                    trades.append(position)

                    if position.pnl > 0:
                        gross_profit += position.pnl
                        consecutive_wins += 1
                        consecutive_losses = 0
                        circuit_breaker.record_win(position.pnl)
                    else:
                        gross_loss += abs(position.pnl)
                        consecutive_losses += 1
                        consecutive_wins = 0
                        circuit_breaker.record_loss(position.pnl, equity)

                    max_consecutive_wins = max(max_consecutive_wins, consecutive_wins)
                    max_consecutive_losses = max(max_consecutive_losses, consecutive_losses)
                    peak_equity = max(peak_equity, equity)
                    last_exit_idx = i
                    position = None

            # Open new position if none and circuit breaker allows
            if position is None:
                if i - last_exit_idx < self.cooldown_bars:
                    equity_curve.append(equity)
                    continue

                candle_ts = df.index[i].to_pydatetime().replace(tzinfo=timezone.utc) if hasattr(df.index[i], 'to_pydatetime') else datetime.now(timezone.utc)
                circuit_breaker.set_time(candle_ts)

                candle_date = df.index[i].date() if hasattr(df.index[i], 'date') else df.index[i].normalize()
                if not hasattr(self, '_last_date') or candle_date != self._last_date:
                    circuit_breaker.reset_daily()
                    self._last_date = candle_date

                can_trade, reason = circuit_breaker.can_open_trade(equity)
                if can_trade and len(close_slice) >= warmup:
                    d1_trend = None
                    if d1_trend_series is not None:
                        candle_date_idx = df.index[i]
                        valid_d1 = d1_trend_series[d1_trend_series.index <= candle_date_idx]
                        if len(valid_d1) > 0:
                            d1_trend = valid_d1.iloc[-1]

                    signal = generate_signal(
                        close=close_slice,
                        high=high_slice,
                        low=low_slice,
                        volume=volume_slice,
                        current_price=current_price,
                        timestamp=candle_ts,
                        d1_trend=d1_trend,
                        min_confidence=self.min_confidence,
                    )

                    if signal.signal_type != SignalType.HOLD and signal.confidence >= self.min_confidence:
                        direction = signal.signal_type
                        sl = calculate_stop_loss(
                            current_price, current_atr, direction.value,
                            self.atr_multiplier, self.spread_buffer,
                        )
                        tp = calculate_take_profit(
                            current_price, sl, direction.value,
                            self.risk_reward_ratio,
                        )
                        lots = calculate_position_size(
                            equity, self.risk_per_trade,
                            current_price, sl, self.contract_size,
                        )
                        position = ForwardTrade(
                            trade_id=0,
                            entry_time=candle_ts,
                            entry_price=current_price,
                            direction=direction,
                            lot_size=lots,
                            stop_loss=sl,
                            take_profit=tp,
                            confidence=signal.confidence,
                            regime=signal.regime or "unknown",
                            session=signal.indicators.get("_session", "unknown"),
                        )

            equity_curve.append(equity)

        # Calculate final metrics
        return self._calculate_result(
            trades, equity_curve, equity, gross_profit, gross_loss,
            max_consecutive_wins, max_consecutive_losses, df, start_date, end_date,
        )

    def _compute_d1_trend(
        self,
        df: pd.DataFrame,
        d1_df: Optional[pd.DataFrame] = None,
    ) -> Optional[pd.Series]:
        """Compute D1 trend (bullish/bearish) from EMA 50/200 crossover."""
        try:
            if d1_df is None:
                d1_df = resample_timeframe(df, "D1")

            if len(d1_df) < 200:
                return None

            ema50 = calculate_ema(d1_df["Close"], 50)
            ema200 = calculate_ema(d1_df["Close"], 200)

            trend = pd.Series(index=d1_df.index, dtype=object)
            for i in range(len(d1_df)):
                if pd.notna(ema50.iloc[i]) and pd.notna(ema200.iloc[i]):
                    trend.iloc[i] = "bullish" if ema50.iloc[i] > ema200.iloc[i] else "bearish"
                else:
                    trend.iloc[i] = None

            trend = trend.dropna()
            return trend if len(trend) > 0 else None
        except Exception:
            return None

    def _check_exit(
        self,
        position: ForwardTrade,
        df: pd.DataFrame,
        idx: int,
        current_price: float,
        equity: float,
    ) -> tuple[Optional[ForwardTrade], float, bool]:
        """Check if position should be exited at current candle."""
        high = float(df["High"].iloc[idx])
        low = float(df["Low"].iloc[idx])
        closed = False

        # Time-based exit
        bars_held = idx - df.index.get_loc(position.entry_time) if position.entry_time in df.index else 0

        if self.max_holding_bars > 0 and bars_held >= self.max_holding_bars:
            position.exit_price = current_price
            position.exit_reason = "max_holding"
            if position.direction == SignalType.BUY:
                position.pnl = (current_price - position.entry_price) * position.lot_size * self.contract_size
                position.pnl_pct = (current_price - position.entry_price) / position.entry_price * 100
            else:
                position.pnl = (position.entry_price - current_price) * position.lot_size * self.contract_size
                position.pnl_pct = (position.entry_price - current_price) / position.entry_price * 100
            equity += position.pnl
            position.holding_bars = bars_held
            closed = True
            return position, equity, closed

        if position.direction == SignalType.BUY:
            if low <= position.stop_loss:
                position.exit_price = position.stop_loss
                position.exit_reason = "stop_loss"
                position.pnl = (position.stop_loss - position.entry_price) * position.lot_size * self.contract_size
                position.pnl_pct = (position.stop_loss - position.entry_price) / position.entry_price * 100
                equity += position.pnl
                closed = True
            elif high >= position.take_profit:
                position.exit_price = position.take_profit
                position.exit_reason = "take_profit"
                position.pnl = (position.take_profit - position.entry_price) * position.lot_size * self.contract_size
                position.pnl_pct = (position.take_profit - position.entry_price) / position.entry_price * 100
                equity += position.pnl
                closed = True
        elif position.direction == SignalType.SELL:
            if high >= position.stop_loss:
                position.exit_price = position.stop_loss
                position.exit_reason = "stop_loss"
                position.pnl = (position.entry_price - position.stop_loss) * position.lot_size * self.contract_size
                position.pnl_pct = (position.entry_price - position.stop_loss) / position.entry_price * 100
                equity += position.pnl
                closed = True
            elif low <= position.take_profit:
                position.exit_price = position.take_profit
                position.exit_reason = "take_profit"
                position.pnl = (position.entry_price - position.take_profit) * position.lot_size * self.contract_size
                position.pnl_pct = (position.entry_price - position.take_profit) / position.entry_price * 100
                equity += position.pnl
                closed = True

        if closed:
            position.holding_bars = bars_held

        return position, equity, closed

    def _calculate_result(
        self,
        trades: list[ForwardTrade],
        equity_curve: list[float],
        final_equity: float,
        gross_profit: float,
        gross_loss: float,
        max_consecutive_wins: int,
        max_consecutive_losses: int,
        df: pd.DataFrame,
        start_date: Optional[str],
        end_date: Optional[str],
    ) -> ForwardResult:
        """Calculate performance metrics from trade list."""
        if not trades:
            return ForwardResult(
                start_date=start_date or str(df.index[0].date()),
                end_date=end_date or str(df.index[-1].date()),
                initial_equity=self.initial_equity,
                final_equity=final_equity,
                equity_curve=equity_curve,
            )

        winning = [t for t in trades if t.pnl > 0]
        losing = [t for t in trades if t.pnl <= 0]
        total_pnl = sum(t.pnl for t in trades)
        total_pnl_pct = (final_equity - self.initial_equity) / self.initial_equity * 100

        equity_series = pd.Series(equity_curve)
        running_max = equity_series.cummax()
        drawdown = (equity_series - running_max) / running_max * 100
        max_dd = abs(drawdown.min()) if len(drawdown) > 0 else 0

        profit_factor = gross_profit / gross_loss if gross_loss > 0 else (float("inf") if gross_profit > 0 else 0)

        daily_returns = equity_series.pct_change().dropna()
        sharpe = (daily_returns.mean() / daily_returns.std() * (252 ** 0.5)) if len(daily_returns) > 1 and daily_returns.std() > 0 else 0

        # Weekly reports
        weekly_reports = self._generate_weekly_reports(trades)

        return ForwardResult(
            start_date=start_date or str(df.index[0].date()),
            end_date=end_date or str(df.index[-1].date()),
            initial_equity=self.initial_equity,
            final_equity=final_equity,
            total_trades=len(trades),
            winning_trades=len(winning),
            losing_trades=len(losing),
            win_rate=len(winning) / len(trades),
            total_pnl=total_pnl,
            total_pnl_pct=total_pnl_pct,
            profit_factor=profit_factor,
            max_drawdown_pct=max_dd,
            sharpe_ratio=sharpe,
            avg_trade_pnl=total_pnl / len(trades),
            max_consecutive_wins=max_consecutive_wins,
            max_consecutive_losses=max_consecutive_losses,
            trades=trades,
            equity_curve=equity_curve,
            weekly_reports=weekly_reports,
        )

    def _generate_weekly_reports(self, trades: list[ForwardTrade]) -> list[dict]:
        """Generate weekly performance reports."""
        if not trades:
            return []

        reports = []
        current_week = None
        week_trades: list[ForwardTrade] = []

        for trade in trades:
            week = trade.entry_time.isocalendar()[1]
            year = trade.entry_time.year

            if current_week is None:
                current_week = (year, week)

            if (year, week) != current_week:
                # Generate report for previous week
                if week_trades:
                    wins = [t for t in week_trades if t.pnl > 0]
                    losses = [t for t in week_trades if t.pnl <= 0]
                    reports.append({
                        "week": f"{current_week[0]}-W{current_week[1]:02d}",
                        "trades": len(week_trades),
                        "wins": len(wins),
                        "losses": len(losses),
                        "win_rate": len(wins) / len(week_trades),
                        "pnl": sum(t.pnl for t in week_trades),
                        "pnl_pct": sum(t.pnl_pct for t in week_trades),
                        "avg_holding_bars": sum(t.holding_bars for t in week_trades) / len(week_trades),
                    })
                current_week = (year, week)
                week_trades = [trade]
            else:
                week_trades.append(trade)

        # Last week
        if week_trades:
            wins = [t for t in week_trades if t.pnl > 0]
            losses = [t for t in week_trades if t.pnl <= 0]
            reports.append({
                "week": f"{current_week[0]}-W{current_week[1]:02d}",
                "trades": len(week_trades),
                "wins": len(wins),
                "losses": len(losses),
                "win_rate": len(wins) / len(week_trades),
                "pnl": sum(t.pnl for t in week_trades),
                "pnl_pct": sum(t.pnl_pct for t in week_trades),
                "avg_holding_bars": sum(t.holding_bars for t in week_trades) / len(week_trades),
            })

        return reports


def save_forward_result(result: ForwardResult, output_path: str | Path) -> None:
    """Save forward test results to JSON file."""
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    data = {
        "symbol": result.symbol,
        "timeframe": result.timeframe,
        "start_date": result.start_date,
        "end_date": result.end_date,
        "initial_equity": result.initial_equity,
        "final_equity": result.final_equity,
        "total_trades": result.total_trades,
        "winning_trades": result.winning_trades,
        "losing_trades": result.losing_trades,
        "win_rate": result.win_rate,
        "total_pnl": result.total_pnl,
        "total_pnl_pct": result.total_pnl_pct,
        "profit_factor": result.profit_factor,
        "max_drawdown_pct": result.max_drawdown_pct,
        "sharpe_ratio": result.sharpe_ratio,
        "avg_trade_pnl": result.avg_trade_pnl,
        "max_consecutive_wins": result.max_consecutive_wins,
        "max_consecutive_losses": result.max_consecutive_losses,
        "weekly_reports": result.weekly_reports,
        "trades": [
            {
                "trade_id": t.trade_id,
                "entry_time": t.entry_time.isoformat(),
                "entry_price": t.entry_price,
                "direction": t.direction.value,
                "lot_size": t.lot_size,
                "stop_loss": t.stop_loss,
                "take_profit": t.take_profit,
                "confidence": t.confidence,
                "regime": t.regime,
                "exit_time": t.exit_time.isoformat() if t.exit_time else None,
                "exit_price": t.exit_price,
                "pnl": t.pnl,
                "pnl_pct": t.pnl_pct,
                "exit_reason": t.exit_reason,
                "holding_bars": t.holding_bars,
            }
            for t in result.trades
        ],
        "config": BEST_CONFIG,
    }

    output_path.write_text(json.dumps(data, indent=2))
    logger.info(f"Forward test results saved to {output_path}")


def run_forward_test(
    start_date: str = "2025-12-01",
    end_date: str = "2026-04-15",
    output: str | None = None,
) -> ForwardResult:
    """Run forward test with the best Phase 1.5 config.

    Args:
        start_date: Start date for the test (ISO format).
        end_date: End date for the test (ISO format).
        output: Optional path to save results JSON.

    Returns:
        ForwardResult with performance metrics.
    """
    data_dir = Path("data/xau-data")

    df_h1 = load_timeframe(data_dir, "H1")
    df_d1 = load_timeframe(data_dir, "D1")

    # Pre-filter for date range
    df_d1_filtered = df_d1[df_d1.index >= pd.Timestamp(start_date) - pd.Timedelta(days=400)]

    engine = ForwardEngine(**BEST_CONFIG)
    result = engine.run(
        df_h1,
        warmup=50,
        d1_df=df_d1_filtered,
        start_date=start_date,
        end_date=end_date,
    )

    if output:
        save_forward_result(result, output)

    return result