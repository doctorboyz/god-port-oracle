"""Backtest engine — runs trading strategy on historical data with performance metrics."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

import numpy as np
import pandas as pd

from shared.models import SignalType
from broky.signals.generator import generate_signal
from broky.risk.circuit_breaker import CircuitBreaker
from broky.risk.position_sizing import calculate_stop_loss, calculate_take_profit, calculate_position_size
from broky.indicators.ema import calculate_ema
from broky.data.resampler import resample_timeframe


@dataclass
class BacktestTrade:
    """A single trade in a backtest."""
    entry_idx: int
    entry_price: float
    direction: SignalType
    lot_size: float
    stop_loss: float
    take_profit: float
    exit_idx: Optional[int] = None
    exit_price: Optional[float] = None
    pnl: float = 0.0
    pnl_pct: float = 0.0
    exit_reason: str = ""


@dataclass
class BacktestResult:
    """Results of a backtest run."""
    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    win_rate: float = 0.0
    total_pnl: float = 0.0
    total_pnl_pct: float = 0.0
    max_drawdown_pct: float = 0.0
    profit_factor: float = 0.0
    sharpe_ratio: float = 0.0
    avg_trade_pnl: float = 0.0
    max_consecutive_wins: int = 0
    max_consecutive_losses: int = 0
    trades: list[BacktestTrade] = field(default_factory=list)
    equity_curve: list[float] = field(default_factory=list)


class BacktestEngine:
    """Simple backtest engine that simulates trades on historical data.

    Runs the signal generator on each candle and simulates trade execution
    with ATR-based stop loss and take profit.

    Example:
        >>> engine = BacktestEngine(initial_equity=1000, risk_per_trade=0.02)
        >>> result = engine.run(df)
        >>> print(f"Win rate: {result.win_rate:.1%}")
    """

    def __init__(
        self,
        initial_equity: float = 1000.0,
        risk_per_trade: float = 0.02,
        atr_multiplier: float = 1.5,
        risk_reward_ratio: float = 2.0,
        spread_buffer: float = 2.0,
        min_confidence: float = 0.55,
        contract_size: float = 100.0,
        max_holding_bars: int = 48,
        cooldown_bars: int = 12,
    ):
        self.initial_equity = initial_equity
        self.risk_per_trade = risk_per_trade
        self.atr_multiplier = atr_multiplier
        self.risk_reward_ratio = risk_reward_ratio
        self.spread_buffer = spread_buffer
        self.min_confidence = min_confidence
        self.contract_size = contract_size  # oz per lot for XAUUSD
        self.max_holding_bars = max_holding_bars  # Max bars before forced exit (48 H1 bars = 48h)
        self.cooldown_bars = cooldown_bars  # Min bars between trades (12h on H1)

    def run(
        self,
        df: pd.DataFrame,
        warmup: int = 50,
        d1_df: Optional[pd.DataFrame] = None,
    ) -> BacktestResult:
        """Run backtest on a DataFrame with OHLCV data.

        Args:
            df: DataFrame with Open, High, Low, Close, Volume columns and DatetimeIndex.
            warmup: Number of initial candles to skip for indicator calculation.
            d1_df: Optional D1 DataFrame for multi-timeframe trend filter.
                If not provided, will be resampled from df (requires lower-TF data).

        Returns:
            BacktestResult with performance metrics and trade list.
        """
        from broky.indicators.atr import calculate_atr

        equity = self.initial_equity
        equity_curve = [equity]
        trades: list[BacktestTrade] = []
        circuit_breaker = CircuitBreaker()

        atr = calculate_atr(df["High"], df["Low"], df["Close"], period=14)

        # Compute D1 trend series if D1 data available
        d1_trend_series = self._compute_d1_trend(df, d1_df)

        position: Optional[BacktestTrade] = None
        consecutive_wins = 0
        consecutive_losses = 0
        max_consecutive_wins = 0
        max_consecutive_losses = 0
        gross_profit = 0.0
        gross_loss = 0.0
        peak_equity = equity
        last_trade_date = None  # Track daily reset for circuit breaker
        last_exit_idx = -self.cooldown_bars - 1  # Allow first trade immediately

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
                    circuit_breaker, consecutive_losses,
                )
                if closed:
                    trades.append(position)
                    last_exit_idx = i
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
                    position = None

            # Open new position if none and circuit breaker allows
            if position is None:
                # Cooldown: wait N bars after last exit before re-entering
                if i - last_exit_idx < self.cooldown_bars:
                    equity_curve.append(equity)
                    continue

                # Update circuit breaker's simulated time for cooldown checks
                candle_ts = df.index[i].to_pydatetime().replace(tzinfo=timezone.utc) if hasattr(df.index[i], 'to_pydatetime') else datetime.now(timezone.utc)
                circuit_breaker.set_time(candle_ts)

                # Reset daily PnL tracking at the start of each new day
                candle_date = df.index[i].date() if hasattr(df.index[i], 'date') else df.index[i].normalize()
                if last_trade_date is None or candle_date != last_trade_date:
                    circuit_breaker.reset_daily()
                    last_trade_date = candle_date

                can_trade, reason = circuit_breaker.can_open_trade(equity)
                if can_trade and len(close_slice) >= warmup:

                    # Get D1 trend for this candle
                    d1_trend = None
                    if d1_trend_series is not None:
                        candle_date = df.index[i]
                        # Find the most recent D1 trend value on or before this candle
                        valid_d1 = d1_trend_series[d1_trend_series.index <= candle_date]
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
                        position = BacktestTrade(
                            entry_idx=i,
                            entry_price=current_price,
                            direction=direction,
                            lot_size=lots,
                            stop_loss=sl,
                            take_profit=tp,
                        )

            equity_curve.append(equity)

        # Calculate performance metrics
        if trades:
            winning_trades = [t for t in trades if t.pnl > 0]
            losing_trades = [t for t in trades if t.pnl <= 0]
            total_pnl = sum(t.pnl for t in trades)
            total_pnl_pct = (equity - self.initial_equity) / self.initial_equity * 100

            # Max drawdown
            equity_series = pd.Series(equity_curve)
            running_max = equity_series.cummax()
            drawdown = (equity_series - running_max) / running_max * 100
            max_drawdown = abs(drawdown.min()) if len(drawdown) > 0 else 0

            # Profit factor
            profit_factor = gross_profit / gross_loss if gross_loss > 0 else float("inf") if gross_profit > 0 else 0

            # Sharpe ratio (simplified)
            daily_returns = equity_series.pct_change().dropna()
            sharpe = (daily_returns.mean() / daily_returns.std() * np.sqrt(252)) if len(daily_returns) > 1 and daily_returns.std() > 0 else 0

            return BacktestResult(
                total_trades=len(trades),
                winning_trades=len(winning_trades),
                losing_trades=len(losing_trades),
                win_rate=len(winning_trades) / len(trades),
                total_pnl=total_pnl,
                total_pnl_pct=total_pnl_pct,
                max_drawdown_pct=max_drawdown,
                profit_factor=profit_factor,
                sharpe_ratio=sharpe,
                avg_trade_pnl=total_pnl / len(trades),
                max_consecutive_wins=max_consecutive_wins,
                max_consecutive_losses=max_consecutive_losses,
                trades=trades,
                equity_curve=equity_curve,
            )

        return BacktestResult(equity_curve=equity_curve)

    def _compute_d1_trend(
        self,
        df: pd.DataFrame,
        d1_df: Optional[pd.DataFrame] = None,
    ) -> Optional[pd.Series]:
        """Compute D1 trend (bullish/bearish) based on EMA 50/200.

        Args:
            df: Primary timeframe DataFrame with DatetimeIndex.
            d1_df: Optional pre-loaded D1 DataFrame. If None, resamples from df.

        Returns:
            Series with 'bullish'/'bearish' values indexed by D1 dates, or None if
            insufficient data.
        """
        try:
            if d1_df is None:
                # Resample from primary TF — only works if TF < D1
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

            # Drop None values (where EMA hasn't warmed up)
            trend = trend.dropna()
            return trend if len(trend) > 0 else None

        except Exception:
            return None

    def _check_exit(
        self,
        position: BacktestTrade,
        df: pd.DataFrame,
        idx: int,
        current_price: float,
        equity: float,
        circuit_breaker: CircuitBreaker,
        consecutive_losses: int,
    ) -> tuple[Optional[BacktestTrade], float, bool]:
        """Check if position should be exited at current candle.

        Returns (position, equity, closed) tuple.
        """
        high = float(df["High"].iloc[idx])
        low = float(df["Low"].iloc[idx])
        closed = False

        # Time-based exit: force close if held too long
        bars_held = idx - position.entry_idx
        if self.max_holding_bars > 0 and bars_held >= self.max_holding_bars:
            position.exit_idx = idx
            position.exit_price = current_price
            position.exit_reason = "max_holding"
            if position.direction == SignalType.BUY:
                position.pnl = (current_price - position.entry_price) * position.lot_size * self.contract_size
                position.pnl_pct = (current_price - position.entry_price) / position.entry_price * 100
            else:
                position.pnl = (position.entry_price - current_price) * position.lot_size * self.contract_size
                position.pnl_pct = (position.entry_price - current_price) / position.entry_price * 100
            equity += position.pnl
            closed = True
            return position, equity, closed

        if position.direction == SignalType.BUY:
            # Stop loss hit
            if low <= position.stop_loss:
                position.exit_idx = idx
                position.exit_price = position.stop_loss
                position.exit_reason = "stop_loss"
                position.pnl = (position.stop_loss - position.entry_price) * position.lot_size * self.contract_size
                position.pnl_pct = (position.stop_loss - position.entry_price) / position.entry_price * 100
                equity += position.pnl
                closed = True
            # Take profit hit
            elif high >= position.take_profit:
                position.exit_idx = idx
                position.exit_price = position.take_profit
                position.exit_reason = "take_profit"
                position.pnl = (position.take_profit - position.entry_price) * position.lot_size * self.contract_size
                position.pnl_pct = (position.take_profit - position.entry_price) / position.entry_price * 100
                equity += position.pnl
                closed = True
        elif position.direction == SignalType.SELL:
            # Stop loss hit
            if high >= position.stop_loss:
                position.exit_idx = idx
                position.exit_price = position.stop_loss
                position.exit_reason = "stop_loss"
                position.pnl = (position.entry_price - position.stop_loss) * position.lot_size * self.contract_size
                position.pnl_pct = (position.entry_price - position.stop_loss) / position.entry_price * 100
                equity += position.pnl
                closed = True
            # Take profit hit
            elif low <= position.take_profit:
                position.exit_idx = idx
                position.exit_price = position.take_profit
                position.exit_reason = "take_profit"
                position.pnl = (position.entry_price - position.take_profit) * position.lot_size * self.contract_size
                position.pnl_pct = (position.entry_price - position.take_profit) / position.entry_price * 100
                equity += position.pnl
                closed = True

        return position, equity, closed