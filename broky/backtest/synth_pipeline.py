"""Backtest-to-ML pipeline — generate synthetic trade_outcomes from historical data.

Runs BacktestEngine on historical candle data, computes full feature snapshots
at each signal point via compute_features_from_candles(), and writes synthetic
trade_outcomes rows to the database for ML training.

Primary use case: produce D1 bearish training data that is missing from live trading
(the system has only 1 bearish SELL trade in 2,070 live trades).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

from broky.backtest.engine import BacktestEngine, BacktestResult, BacktestTrade
from broky.data.loader import load_timeframe
from broky.data.resampler import resample_timeframe
from broky.indicators.ema import calculate_ema
from broky.ml.trade_outcome_predictor import compute_features_from_candles
from metty.core.db import (
    SYNTHETIC_ACCOUNT_ID,
    insert_synthetic_trade,
    insert_synthetic_trade_outcome,
)

logger = logging.getLogger(__name__)

# Timeframe to primary-TF mapping for backtesting
DEFAULT_PRIMARY_TF = "H1"


@dataclass
class SynthTradeOutcome:
    """Complete synthetic trade outcome with features."""

    # Trade metadata
    direction: str  # "BUY" or "SELL"
    entry_price: float
    exit_price: float
    profit: float
    profit_pct: float
    outcome_label: str  # "WIN" or "LOSS"
    holding_minutes: int
    exit_reason: str

    # Context at entry
    d1_trend: str  # "bullish" or "bearish"
    h4_trend: str
    session: str  # "london", "new_york", "asian", "overlap"
    regime: str  # "trending", "ranging", "volatile"
    strategy_id: str

    # MFE/MAE
    mfe: float = 0.0
    mae: float = 0.0
    mfe_pct: float = 0.0
    mae_pct: float = 0.0

    # Exit context
    exit_d1_trend: str = "unknown"
    exit_h4_trend: str = "unknown"
    exit_regime: str = "unknown"

    # Trading parameters used for this trade
    atr_multiplier: float = 1.5
    rr_ratio: float = 2.0
    min_confidence_threshold: float = 0.30

    # Full feature snapshot (for features_json)
    features: dict = field(default_factory=dict)

    # Timestamps
    entry_timestamp: str = ""
    exit_timestamp: str = ""


class BacktestToMLPipeline:
    """Generate synthetic ML training data from historical backtests.

    Loads multi-timeframe candle data, runs BacktestEngine to generate signals,
    computes feature snapshots at each signal point, and writes synthetic
    trade_outcomes rows to the database.

    Designed to fill the D1 bearish data gap: only 1 bearish SELL trade exists
    in 2,070 live trades. This pipeline can produce hundreds of bearish SELL
    trades from historical data spanning 2009-2026.

    Usage:
        pipeline = BacktestToMLPipeline(data_dir="data/xau-data")
        stats = pipeline.run(target_regimes=["bearish"])
    """

    def __init__(
        self,
        data_dir: str | Path = "data/xau-data",
        db_path: str | Path | None = None,
        strategy: str = "swing",
        primary_tf: str = DEFAULT_PRIMARY_TF,
        min_confidence: float = 0.30,
        learning_mode: bool = True,
        risk_per_trade: float = 0.02,
        atr_multiplier: float = 1.5,
        risk_reward_ratio: float = 2.0,
        initial_equity: float = 10000,
        max_holding_bars: int = 48,
        cooldown_bars: int = 12,
        slippage_bps: float = 0.0,
        spread_buffer: float = 2.0,
        contract_size: float = 100.0,
        target_regimes: list[str] | None = None,
        use_h1_fallback: bool = False,
        dry_run: bool = False,
        verbose: bool = False,
    ):
        self.data_dir = Path(data_dir)
        self.db_path = Path(db_path) if db_path else None
        self.strategy = strategy
        self.primary_tf = primary_tf
        self.min_confidence = min_confidence
        self.learning_mode = learning_mode
        self.risk_per_trade = risk_per_trade
        self.atr_multiplier = atr_multiplier
        self.risk_reward_ratio = risk_reward_ratio
        self.initial_equity = initial_equity
        self.max_holding_bars = max_holding_bars
        self.cooldown_bars = cooldown_bars
        self.slippage_bps = slippage_bps
        self.spread_buffer = spread_buffer
        self.contract_size = contract_size
        self.target_regimes = target_regimes  # e.g. ["bearish"] for targeted
        self.use_h1_fallback = use_h1_fallback
        self.dry_run = dry_run
        self.verbose = verbose

        # Loaded data (populated by _load_multi_tf_data)
        self._candles: dict[str, pd.DataFrame] = {}
        self._d1_trend_series: pd.Series | None = None

    def run(self) -> dict:
        """Execute the full pipeline: load data, backtest, compute features, write DB.

        Returns:
            Stats dict with total_trades, wins, losses, bearish_sell, etc.
        """
        self._log("Loading multi-TF data...")
        self._load_multi_tf_data()

        primary = self._candles.get(self.primary_tf.lower())
        if primary is None or primary.empty:
            raise ValueError(f"Primary TF data ({self.primary_tf}) not loaded")

        # Compute D1 trend series
        d1 = self._candles.get("d1")
        if d1 is not None and len(d1) >= 200:
            self._d1_trend_series = self._compute_d1_trend_series(d1)
            self._log(f"Computed D1 trend series: {len(self._d1_trend_series)} bars")
        else:
            self._log("WARNING: D1 data insufficient for trend series (< 200 bars)")

        # Identify bearish periods if targeting
        if self.target_regimes and "bearish" in [r.lower() for r in self.target_regimes]:
            periods = self._identify_bearish_periods()
            self._log(f"Found {len(periods)} bearish D1 period(s)")
        else:
            # Run on full date range
            primary = self._candles[self.primary_tf.lower()]
            start = primary.index[0]
            end = primary.index[-1]
            periods = [(start, end)]
            self._log(f"Running on full range: {start} to {end}")

        # Run pipeline for each period
        all_outcomes: list[SynthTradeOutcome] = []
        for i, (start, end) in enumerate(periods):
            self._log(f"Period {i+1}/{len(periods)}: {start.date()} to {end.date()}")
            outcomes = self._run_pipeline_for_period(start, end)
            all_outcomes.extend(outcomes)
            self._log(f"  → {len(outcomes)} trades generated")

        # Write to database
        stats = self._write_outcomes(all_outcomes)

        self._log(f"\nPipeline complete:")
        self._log(f"  Total trades: {stats['total_trades']}")
        self._log(f"  BUY trades: {stats['buy_trades']}")
        self._log(f"  SELL trades: {stats['sell_trades']}")
        self._log(f"  WIN: {stats['wins']}, LOSS: {stats['losses']}")
        self._log(f"  Bearish SELL: {stats['bearish_sell']}")
        self._log(f"  Bullish BUY: {stats['bullish_buy']}")
        self._log(f"  DB rows written: {stats['db_writes']}")

        return stats

    def _log(self, msg: str) -> None:
        """Log message if verbose."""
        if self.verbose:
            print(f"[synth_pipeline] {msg}")
        logger.info(msg)

    def _load_multi_tf_data(self) -> None:
        """Load candle data for all required timeframes.

        DataFrames are stored with Title Case columns (as loaded by loader.py)
        since BacktestEngine expects Title Case. Lowercase copies are created
        on-the-fly in _slice_candles_at_timestamp() for compute_features_from_candles().
        """
        tf_map = {
            "d1": "D1",
            "h4": "H4",
            "h1": "H1",
            "m5": "M5",
        }

        for key, tf_name in tf_map.items():
            try:
                df = load_timeframe(self.data_dir, tf_name)
                # Keep Title Case columns (BacktestEngine needs them)
                # compute_features_from_candles() needs lowercase — handled in _slice_candles_at_timestamp()
                self._candles[key] = df
                self._log(f"Loaded {tf_name}: {len(df)} rows ({df.index[0]} to {df.index[-1]})")
            except FileNotFoundError:
                if key == "m5" and self.use_h1_fallback:
                    self._log(f"M5 not found, will use H1 fallback")
                    self._candles[key] = pd.DataFrame()
                elif key in ("d1", "h4", "h1"):
                    self._log(f"WARNING: {tf_name} data not found, some features will be missing")
                    self._candles[key] = pd.DataFrame()
                elif key == "m5":
                    self._log(f"WARNING: M5 data not found, indicator features will be missing")
                    self._candles[key] = pd.DataFrame()
                else:
                    raise

    def _compute_d1_trend_series(self, d1: pd.DataFrame) -> pd.Series:
        """Compute D1 trend series (bullish/bearish) based on EMA 50/200."""
        # DataFrames use lowercase columns (since loader standardization)
        ema50 = calculate_ema(d1["close"], 50)
        ema200 = calculate_ema(d1["close"], 200)

        trend = pd.Series(index=d1.index, dtype=object)
        for i in range(len(d1)):
            if pd.notna(ema50.iloc[i]) and pd.notna(ema200.iloc[i]):
                trend.iloc[i] = "bullish" if ema50.iloc[i] > ema200.iloc[i] else "bearish"
            else:
                trend.iloc[i] = None

        return trend.dropna()

    def _identify_bearish_periods(self, min_days: int = 5) -> list[tuple[pd.Timestamp, pd.Timestamp]]:
        """Find D1 periods where EMA50 < EMA200 (bearish) for at least min_days consecutive days."""
        if self._d1_trend_series is None or len(self._d1_trend_series) == 0:
            return []

        bearish_mask = self._d1_trend_series == "bearish"
        periods = []
        start = None

        for i, (ts, is_bearish) in enumerate(bearish_mask.items()):
            if is_bearish:
                if start is None:
                    start = ts
            else:
                if start is not None:
                    end_ts = bearish_mask.index[i - 1]
                    duration = (end_ts - start).days + 1
                    if duration >= min_days:
                        periods.append((start, end_ts))
                    start = None

        # Handle trailing bearish period
        if start is not None:
            end_ts = bearish_mask.index[-1]
            duration = (end_ts - start).days + 1
            if duration >= min_days:
                periods.append((start, end_ts))

        return periods

    def _compute_h4_trend_at(self, ts: pd.Timestamp) -> str:
        """Compute H4 trend at a given timestamp."""
        h4 = self._candles.get("h4")
        if h4 is None or h4.empty:
            return "unknown"

        valid = h4[h4.index <= ts]
        if len(valid) < 200:
            return "unknown"

        # DataFrames use lowercase columns (since loader standardization)
        ema50 = calculate_ema(valid["close"], 50)
        ema200 = calculate_ema(valid["close"], 200)

        last_ema50 = ema50.iloc[-1]
        last_ema200 = ema200.iloc[-1]

        if pd.notna(last_ema50) and pd.notna(last_ema200):
            return "bullish" if last_ema50 > last_ema200 else "bearish"
        return "unknown"

    def _classify_session(self, ts: pd.Timestamp) -> str:
        """Classify trading session from UTC hour.

        Matches compute_features_from_candles() session_strength logic:
          13-16 UTC → overlap (London/NY)
          8-16 UTC  → london
          13-22 UTC → new_york
          0-8 UTC   → asian
          else       → overlap (off-hours)
        """
        hour = ts.hour if hasattr(ts, "hour") else 0
        if 13 <= hour <= 16:
            return "overlap"
        elif 8 <= hour <= 16:
            return "london"
        elif 13 <= hour <= 22:
            return "new_york"
        elif 0 <= hour <= 8:
            return "asian"
        else:
            return "overlap"  # off-hours fall to overlap for session_strength purposes

    def _determine_regime(self, features: dict) -> str:
        """Classify market regime from ADX and Bollinger bandwidth in features.

        Matches the regime classification used in signal generation.
        """
        adx = features.get("adx", 20.0)
        boll_bw = features.get("boll_bw", 0.02)

        if isinstance(adx, str):
            adx = 20.0
        if isinstance(boll_bw, str):
            boll_bw = 0.02

        try:
            adx = float(adx)
            boll_bw = float(boll_bw)
        except (ValueError, TypeError):
            adx = 20.0
            boll_bw = 0.02

        if adx >= 25 and boll_bw > 0.035:
            return "volatile"
        elif adx >= 25:
            return "trending"
        elif adx >= 20:
            return "ranging"
        else:
            return "ranging"

    def _slice_candles_at_timestamp(
        self,
        ts: pd.Timestamp,
        window_size: int = 500,
    ) -> dict[str, pd.DataFrame]:
        """Slice candle data up to timestamp for each TF (strict no-lookahead).

        Returns a dict matching compute_features_from_candles() input format:
        keys "M5", "H1", "H4", "D1" with **lowercase** column names,
        since compute_features_from_candles() expects lowercase (close, high, low, volume).
        """
        result = {}
        tf_key_map = {
            "m5": "M5",
            "h1": "H1",
            "h4": "H4",
            "d1": "D1",
        }

        def _to_lowercase(df: pd.DataFrame) -> pd.DataFrame:
            """Convert Title Case columns to lowercase for compute_features_from_candles()."""
            renamed = df.copy()
            renamed.columns = [c.lower() for c in renamed.columns]
            return renamed

        for key, tf_name in tf_key_map.items():
            df = self._candles.get(key)
            if df is None or df.empty:
                # For M5 with H1 fallback, use H1 data as M5 substitute
                if key == "m5" and self.use_h1_fallback:
                    h1 = self._candles.get("h1")
                    if h1 is not None and not h1.empty:
                        valid = h1[h1.index <= ts].tail(window_size)
                        if len(valid) >= 50:
                            result[tf_name] = _to_lowercase(valid)
                            continue
                result[tf_name] = pd.DataFrame()
                continue

            valid = df[df.index <= ts].tail(window_size)
            if len(valid) >= 50:
                result[tf_name] = _to_lowercase(valid)
            elif key == "m5" and self.use_h1_fallback:
                # M5 data exists but doesn't cover this timestamp — try H1 fallback
                h1 = self._candles.get("h1")
                if h1 is not None and not h1.empty:
                    h1_valid = h1[h1.index <= ts].tail(window_size)
                    if len(h1_valid) >= 50:
                        result[tf_name] = _to_lowercase(h1_valid)
                        continue
                result[tf_name] = pd.DataFrame()
            else:
                result[tf_name] = pd.DataFrame()

        return result

    def _compute_mfe_mae(
        self,
        trade: BacktestTrade,
        df: pd.DataFrame,
    ) -> tuple[float, float, float, float]:
        """Compute Maximum Favorable/Adverse Excursion for a trade.

        For BUY: MFE = max(high) - entry_price, MAE = entry_price - min(low)
        For SELL: MFE = entry_price - min(low), MAE = max(high) - entry_price

        Returns (mfe, mae, mfe_pct, mae_pct).
        """
        if trade.exit_idx is None or trade.entry_idx is None:
            return 0.0, 0.0, 0.0, 0.0

        start = trade.entry_idx
        end = trade.exit_idx

        if end <= start:
            return 0.0, 0.0, 0.0, 0.0

        try:
            high_slice = df["high"].iloc[start:end + 1]
            low_slice = df["low"].iloc[start:end + 1]
        except (IndexError, KeyError):
            return 0.0, 0.0, 0.0, 0.0

        max_high = float(high_slice.max())
        min_low = float(low_slice.min())
        entry = trade.entry_price

        if trade.direction.value == "BUY":
            mfe = max_high - entry
            mae = entry - min_low
        else:
            mfe = entry - min_low
            mae = max_high - entry

        mfe_pct = (mfe / entry * 100) if entry > 0 else 0.0
        mae_pct = (mae / entry * 100) if entry > 0 else 0.0

        return round(mfe, 2), round(mae, 2), round(mfe_pct, 2), round(mae_pct, 2)

    def _run_pipeline_for_period(
        self,
        start: pd.Timestamp,
        end: pd.Timestamp,
    ) -> list[SynthTradeOutcome]:
        """Run the complete pipeline for a date range.

        1. Slice primary TF data to period
        2. Run BacktestEngine with low confidence to capture more signals
        3. For each trade, compute features and build SynthTradeOutcome
        """
        primary = self._candles[self.primary_tf.lower()]
        period_mask = (primary.index >= start) & (primary.index <= end)
        period_df = primary[period_mask].copy()

        if len(period_df) < 100:
            self._log(f"  Skipping period: only {len(period_df)} bars")
            return []

        # Get D1 data for the engine
        d1 = self._candles.get("d1")
        d1_period = None
        if d1 is not None and not d1.empty:
            # Filter D1 to overlap with period (with some lookback for warmup)
            d1_start = start - pd.Timedelta(days=300)
            d1_mask = (d1.index >= d1_start) & (d1.index <= end)
            d1_period = d1[d1_mask].copy()

        # Run backtest
        engine = BacktestEngine(
            initial_equity=self.initial_equity,
            risk_per_trade=self.risk_per_trade,
            atr_multiplier=self.atr_multiplier,
            risk_reward_ratio=self.risk_reward_ratio,
            spread_buffer=self.spread_buffer,
            min_confidence=self.min_confidence,
            contract_size=self.contract_size,
            max_holding_bars=self.max_holding_bars,
            cooldown_bars=self.cooldown_bars,
            slippage_bps=self.slippage_bps,
            strategy=self.strategy,
            learning_mode=self.learning_mode,
        )

        result = engine.run(period_df, warmup=50, d1_df=d1_period)
        self._log(f"  Backtest: {result.total_trades} trades, WR={result.win_rate:.1%}, PF={result.profit_factor:.2f}")

        # Convert trades to SynthTradeOutcomes with features
        outcomes: list[SynthTradeOutcome] = []
        for trade in result.trades:
            outcome = self._trade_to_outcome(trade, period_df)
            if outcome is not None:
                outcomes.append(outcome)

        return outcomes

    def _trade_to_outcome(
        self,
        trade: BacktestTrade,
        df: pd.DataFrame,
    ) -> SynthTradeOutcome | None:
        """Convert a BacktestTrade to a SynthTradeOutcome with full features."""
        if trade.exit_idx is None or trade.entry_idx is None:
            return None

        # Get entry and exit timestamps
        try:
            entry_ts = df.index[trade.entry_idx]
            exit_ts = df.index[trade.exit_idx]
        except (IndexError, KeyError):
            return None

        # Determine D1 trend at entry
        d1_trend = "neutral"
        if self._d1_trend_series is not None:
            valid = self._d1_trend_series[self._d1_trend_series.index <= entry_ts]
            if len(valid) > 0:
                d1_trend = valid.iloc[-1]

        # Determine H4 trend at entry
        h4_trend = self._compute_h4_trend_at(entry_ts)

        # Classify session
        session = self._classify_session(entry_ts)

        # Slice candles for feature computation (no lookahead)
        candle_slices = self._slice_candles_at_timestamp(entry_ts)

        # Compute features
        direction = trade.direction.value  # "BUY" or "SELL"
        features = compute_features_from_candles(
            candles=candle_slices,
            direction=direction,
            spread=0,
            d1_trend=d1_trend,
            h4_trend=h4_trend,
            session=session,
        )

        if not features:
            self._log(f"  Skipping trade at {entry_ts}: empty features")
            return None

        # Determine regime from features
        regime = self._determine_regime(features)

        # Compute MFE/MAE
        mfe, mae, mfe_pct, mae_pct = self._compute_mfe_mae(trade, df)

        # Compute exit context
        exit_d1_trend = d1_trend
        if self._d1_trend_series is not None:
            valid = self._d1_trend_series[self._d1_trend_series.index <= exit_ts]
            if len(valid) > 0:
                exit_d1_trend = valid.iloc[-1]

        exit_h4_trend = self._compute_h4_trend_at(exit_ts)

        # Determine outcome
        profit = trade.pnl
        profit_pct = trade.pnl_pct
        outcome_label = "WIN" if profit > 0 else ("LOSS" if profit < 0 else "BREAKEVEN")

        # Compute holding time in minutes
        # Primary TF bar duration (H1=60min, M5=5min, etc.)
        tf_minutes = {"M1": 1, "M5": 5, "M15": 15, "M30": 30, "H1": 60, "H4": 240, "D1": 1440}
        bar_minutes = tf_minutes.get(self.primary_tf.upper(), 60)
        holding_minutes = (trade.exit_idx - trade.entry_idx) * bar_minutes

        # Strategy ID for synthetic data
        tf_suffix = "h1" if self.use_h1_fallback else self.primary_tf.lower()
        strategy_id = f"backtest_{self.strategy}_{tf_suffix}_synth"

        # Compute exit regime
        exit_regime = self._determine_regime(features)  # Approximate with entry features

        return SynthTradeOutcome(
            direction=direction,
            entry_price=trade.entry_price,
            exit_price=trade.exit_price or 0.0,
            profit=profit,
            profit_pct=profit_pct,
            outcome_label=outcome_label,
            holding_minutes=holding_minutes,
            exit_reason=trade.exit_reason,
            d1_trend=d1_trend,
            h4_trend=h4_trend,
            session=session,
            regime=regime,
            strategy_id=strategy_id,
            mfe=mfe,
            mae=mae,
            mfe_pct=mfe_pct,
            mae_pct=mae_pct,
            exit_d1_trend=exit_d1_trend,
            exit_h4_trend=exit_h4_trend,
            exit_regime=exit_regime,
            atr_multiplier=self.atr_multiplier,
            rr_ratio=self.risk_reward_ratio,
            min_confidence_threshold=self.min_confidence,
            features=features,
            entry_timestamp=entry_ts.isoformat(),
            exit_timestamp=exit_ts.isoformat(),
        )

    def _write_outcomes(self, outcomes: list[SynthTradeOutcome]) -> dict:
        """Write synthetic trade outcomes to the database.

        Returns stats dict.
        """
        stats = {
            "total_trades": len(outcomes),
            "wins": 0,
            "losses": 0,
            "buy_trades": 0,
            "sell_trades": 0,
            "bearish_sell": 0,
            "bullish_buy": 0,
            "db_writes": 0,
        }

        for outcome in outcomes:
            # Count stats
            if outcome.outcome_label == "WIN":
                stats["wins"] += 1
            else:
                stats["losses"] += 1

            if outcome.direction == "BUY":
                stats["buy_trades"] += 1
                if outcome.d1_trend == "bullish":
                    stats["bullish_buy"] += 1
            else:
                stats["sell_trades"] += 1
                if outcome.d1_trend == "bearish":
                    stats["bearish_sell"] += 1

            if self.dry_run:
                stats["db_writes"] += 1
                continue

            # Write to database
            try:
                # Enrich features with regime and exit context (not computed by
                # compute_features_from_candles, but needed for ML training)
                enriched_features = {**outcome.features, "regime": outcome.regime}
                if outcome.exit_d1_trend:
                    enriched_features["exit_d1_trend"] = outcome.exit_d1_trend
                if outcome.exit_h4_trend:
                    enriched_features["exit_h4_trend"] = outcome.exit_h4_trend
                features_json = json.dumps(enriched_features, separators=(",", ":"))

                trade_id = insert_synthetic_trade(
                    direction=outcome.direction,
                    entry_price=outcome.entry_price,
                    exit_price=outcome.exit_price,
                    pnl=outcome.profit,
                    pnl_pct=outcome.profit_pct,
                    d1_trend=outcome.d1_trend,
                    session=outcome.session,
                    regime=outcome.regime,
                    strategy_id=outcome.strategy_id,
                    entry_time=outcome.entry_timestamp,
                    exit_time=outcome.exit_timestamp,
                    exit_reason=outcome.exit_reason,
                    trading_mode="backtest",
                    h4_trend=outcome.h4_trend,
                    atr_multiplier=outcome.atr_multiplier,
                    rr_ratio=outcome.rr_ratio,
                    min_confidence_threshold=outcome.min_confidence_threshold,
                    db_path=self.db_path,
                )

                insert_synthetic_trade_outcome(
                    trade_id=trade_id,
                    direction=outcome.direction,
                    entry_price=outcome.entry_price,
                    exit_price=outcome.exit_price,
                    profit=outcome.profit,
                    profit_pct=outcome.profit_pct,
                    outcome_label=outcome.outcome_label,
                    holding_minutes=outcome.holding_minutes,
                    exit_reason=outcome.exit_reason,
                    features_json=features_json,
                    regime=outcome.regime,
                    d1_trend=outcome.d1_trend,
                    h4_trend=outcome.h4_trend,
                    session=outcome.session,
                    strategy_id=outcome.strategy_id,
                    trading_mode="backtest",
                    mfe=outcome.mfe,
                    mae=outcome.mae,
                    mfe_pct=outcome.mfe_pct,
                    mae_pct=outcome.mae_pct,
                    exit_regime=outcome.exit_regime,
                    exit_d1_trend=outcome.exit_d1_trend,
                    exit_h4_trend=outcome.exit_h4_trend,
                    atr_multiplier=outcome.atr_multiplier,
                    rr_ratio=outcome.rr_ratio,
                    min_confidence_threshold=outcome.min_confidence_threshold,
                    db_path=self.db_path,
                )
                stats["db_writes"] += 1

            except Exception as e:
                logger.error(f"Failed to write trade to DB: {e}")
                self._log(f"  ERROR writing trade: {e}")

        return stats