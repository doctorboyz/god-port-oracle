"""Historical data collector — replays CSV data through GroupCoordinator for ML training.

Loads XAUUSD Premium Data CSVs, resamples to multi-timeframe, computes all
indicators at regular intervals, and writes feature snapshots to SQLite.
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

from broky.data.loader import load_timeframe
from broky.data.resampler import resample_timeframe
from broky.indicators.macd import calculate_macd
from broky.indicators.adx import calculate_adx
from broky.indicators.bollinger import calculate_bollinger
from broky.indicators.atr import calculate_atr
from broky.signals.group_engine import GroupCoordinator
from metty.core.db import (
    get_connection,
    init_db,
    insert_feature_snapshot,
    insert_signal,
)
from shared.models import SignalGroup

logger = logging.getLogger(__name__)

# Account ID for historical data (created in accounts table)
HISTORICAL_ACCOUNT_ID = 1

# Default sampling: capture snapshot every N M5 bars
DEFAULT_SAMPLE_EVERY = 12  # every hour on M5


def _normalize_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Ensure OHLCV columns are lowercase.

    Since load_timeframe() now returns lowercase columns by default,
    this is a pass-through. Kept for backward compatibility with any
    code that still calls it.
    """
    rename_map = {}
    for col in df.columns:
        lower = col.lower()
        if lower in ("open", "high", "low", "close", "volume") and col != lower:
            rename_map[col] = lower
    if rename_map:
        df = df.rename(columns=rename_map)
    return df


def _classify_session(timestamp: pd.Timestamp) -> str:
    """Classify trading session from UTC timestamp."""
    hour = timestamp.hour
    if 13 <= hour < 16:
        return "overlap"
    if 8 <= hour < 16:
        return "london"
    if 13 <= hour < 22:
        return "ny"
    return "asian"


def _determine_d1_trend(d1: pd.DataFrame) -> str:
    """Determine D1 trend from EMA 50/200 on daily data."""
    if d1 is None or len(d1) < 200:
        return "unknown"
    close = d1["close"]
    ema50 = close.ewm(span=50, adjust=False).mean()
    ema200 = close.ewm(span=200, adjust=False).mean()
    if pd.isna(ema200.iloc[-1]):
        return "unknown"
    if ema50.iloc[-1] > ema200.iloc[-1]:
        return "bullish"
    return "bearish"


def _compute_h4_trend(h4: pd.DataFrame) -> str:
    """Compute H4 trend using EMA 10/50 crossover (faster than D1 EMA 50/200)."""
    if h4 is None or len(h4) < 50:
        return "unknown"
    try:
        close = h4["close"]
        ema10 = close.ewm(span=10, adjust=False).mean()
        ema50 = close.ewm(span=50, adjust=False).mean()
        if pd.isna(ema10.iloc[-1]) or pd.isna(ema50.iloc[-1]):
            return "unknown"
        return "bullish" if ema10.iloc[-1] > ema50.iloc[-1] else "bearish"
    except Exception:
        return "unknown"


def compute_broky_indicators(m5: pd.DataFrame) -> dict[str, float | None]:
    """Compute Broky original indicators not covered by signal groups.

    Returns dict with: macd_hist, adx, plus_di, minus_di,
    boll_pct_b, boll_bw, atr, atr_to_price.
    """
    close = m5["close"]
    high = m5["high"]
    low = m5["low"]

    result: dict[str, float | None] = {}

    # MACD
    macd = calculate_macd(close)
    hist = macd.histogram.iloc[-1] if len(macd.histogram) > 0 else float("nan")
    result["macd_hist"] = float(hist) if not pd.isna(hist) else None

    # ADX
    adx, plus_di, minus_di = calculate_adx(high, low, close, period=14)
    result["adx"] = float(adx.iloc[-1]) if not pd.isna(adx.iloc[-1]) else None
    result["plus_di"] = float(plus_di.iloc[-1]) if not pd.isna(plus_di.iloc[-1]) else None
    result["minus_di"] = float(minus_di.iloc[-1]) if not pd.isna(minus_di.iloc[-1]) else None

    # Bollinger
    boll = calculate_bollinger(close, period=20, std_dev=2.0)
    latest_close = close.iloc[-1]
    if not pd.isna(boll.upper.iloc[-1]) and not pd.isna(boll.middle.iloc[-1]):
        middle = boll.middle.iloc[-1]
        result["boll_pct_b"] = (
            (latest_close - boll.lower.iloc[-1]) / (boll.upper.iloc[-1] - boll.lower.iloc[-1])
            if (boll.upper.iloc[-1] - boll.lower.iloc[-1]) != 0
            else None
        )
        result["boll_bw"] = (
            (boll.upper.iloc[-1] - boll.lower.iloc[-1]) / middle
            if middle != 0
            else None
        )
    else:
        result["boll_pct_b"] = None
        result["boll_bw"] = None

    # ATR
    atr = calculate_atr(high, low, close, period=14)
    atr_val = atr.iloc[-1] if len(atr) > 0 else float("nan")
    result["atr"] = float(atr_val) if not pd.isna(atr_val) else None
    result["atr_to_price"] = (
        float(atr_val) / latest_close if not pd.isna(atr_val) and latest_close > 0 else None
    )

    return result


# Fixed window size for indicator computation.
# EMA 200 needs ~400 bars to stabilize; 500 gives margin.
WINDOW_SIZE = 500


class HistoricalCollector:
    """Replays historical CSV data through GroupCoordinator for ML training.

    Loads M5 data, resamples to H1/D1, computes indicators at regular
    intervals, and writes feature snapshots to SQLite.
    """

    def __init__(
        self,
        data_dir: str | Path,
        db_path: Optional[Path] = None,
        warmup: int = 200,
        sample_every: int = DEFAULT_SAMPLE_EVERY,
    ):
        self.data_dir = Path(data_dir)
        self.db_path = db_path
        self.warmup = warmup
        self.sample_every = sample_every
        self.coordinator = GroupCoordinator()

    def collect(self) -> dict[str, int]:
        """Run the full historical collection pipeline.

        Returns dict with stats: total_bars, sampled_bars, snapshots_written, errors.
        """
        init_db(self.db_path)

        logger.info("Loading M5 data from %s", self.data_dir)
        m5_raw = load_timeframe(self.data_dir, "M5")
        logger.info("Loaded %d M5 bars", len(m5_raw))

        # Resample to higher timeframes
        logger.info("Resampling to H1, H4, D1...")
        h1_raw = resample_timeframe(m5_raw, "H1")
        h4_raw = resample_timeframe(m5_raw, "H4")
        d1_raw = resample_timeframe(m5_raw, "D1")

        # Normalize to lowercase columns
        m5 = _normalize_columns(m5_raw)
        h1 = _normalize_columns(h1_raw)
        h4 = _normalize_columns(h4_raw)
        d1 = _normalize_columns(d1_raw)

        total_bars = len(m5)
        start_idx = max(self.warmup, 1)
        sampled_count = 0
        snapshot_count = 0
        error_count = 0

        logger.info(
            "Starting collection: %d total bars, warmup=%d, sample_every=%d",
            total_bars, self.warmup, self.sample_every,
        )

        for i in range(start_idx, total_bars):
            if (i - start_idx) % self.sample_every != 0:
                continue

            timestamp = m5.index[i]

            # Build multi-timeframe windows with fixed size cap
            m5_start = max(0, i + 1 - WINDOW_SIZE)
            m5_window = m5.iloc[m5_start:i + 1]
            h1_window = h1[h1.index <= timestamp].tail(WINDOW_SIZE)
            h4_window = h4[h4.index <= timestamp].tail(WINDOW_SIZE)
            d1_window = d1[d1.index <= timestamp].tail(WINDOW_SIZE)

            candles = {"M5": m5_window, "H1": h1_window, "H4": h4_window, "D1": d1_window}

            try:
                snapshot = self._collect_snapshot(candles, timestamp)
                if snapshot is not None:
                    snapshot_count += 1
                sampled_count += 1
            except Exception as e:
                logger.warning("Error at bar %d (%s): %s", i, timestamp, e)
                error_count += 1

            if snapshot_count > 0 and snapshot_count % 500 == 0:
                logger.info("Progress: %d snapshots written (bar %d/%d)", snapshot_count, i, total_bars)

        logger.info(
            "Collection complete: %d sampled, %d snapshots, %d errors",
            sampled_count, snapshot_count, error_count,
        )
        return {
            "total_bars": total_bars,
            "sampled_bars": sampled_count,
            "snapshots_written": snapshot_count,
            "errors": error_count,
        }

    def _collect_snapshot(
        self, candles: dict[str, pd.DataFrame], timestamp: pd.Timestamp
    ) -> Optional[int]:
        """Compute all indicators and write a feature snapshot to the DB.

        Returns snapshot ID or None if no valid data.
        """
        m5 = candles["M5"]
        d1 = candles.get("D1")

        # Compute all indicators from 4 groups
        from broky.signals.groups.base import compute_all_indicators
        full_snapshot = compute_all_indicators(candles)

        # Add price
        if m5 is not None and not m5.empty:
            full_snapshot["price"] = float(m5["close"].iloc[-1])

        # Add Broky original indicators
        broky = compute_broky_indicators(m5)
        full_snapshot.update(broky)

        # Session, D1 trend, and H4 trend
        session = _classify_session(timestamp)
        d1_trend = _determine_d1_trend(d1) if d1 is not None else "unknown"
        h4 = candles.get("H4")
        h4_trend = _compute_h4_trend(h4) if h4 is not None else "unknown"
        full_snapshot["session"] = session
        full_snapshot["d1_trend"] = d1_trend
        full_snapshot["h4_trend"] = h4_trend

        # Multi-timeframe price context for ML features
        h1 = candles.get("H1")
        full_snapshot["h1_close"] = float(h1["close"].iloc[-1]) if h1 is not None and not h1.empty else None
        full_snapshot["h4_close"] = float(h4["close"].iloc[-1]) if h4 is not None and not h4.empty else None
        full_snapshot["d1_close"] = float(d1["close"].iloc[-1]) if d1 is not None and not d1.empty else None
        full_snapshot["m5_high"] = float(m5["high"].iloc[-1]) if m5 is not None and not m5.empty else None
        full_snapshot["m5_low"] = float(m5["low"].iloc[-1]) if m5 is not None and not m5.empty else None

        price = full_snapshot.get("price", 0.0)
        if price <= 0:
            return None

        ts_str = timestamp.isoformat()

        # Insert a signal record for the periodic snapshot
        signal_id = insert_signal(
            timestamp=ts_str,
            group_name="ml_periodic",
            direction="HOLD",
            confidence=0.0,
            triggering_indicators="",
            price=price,
            account_id=HISTORICAL_ACCOUNT_ID,
            db_path=self.db_path,
        )

        # Separate indicator values from metadata
        metadata_keys = {"price", "session", "d1_trend", "h4_trend"}
        indicator_values = {k: v for k, v in full_snapshot.items() if k not in metadata_keys}

        # Convert NaN to None for SQLite
        clean_values: dict[str, float | str | int | None] = {}
        for k, v in indicator_values.items():
            if isinstance(v, float) and (pd.isna(v) or np.isnan(v)):
                clean_values[k] = None
            else:
                clean_values[k] = v

        snapshot_id = insert_feature_snapshot(
            signal_id=signal_id,
            timestamp=ts_str,
            price=price,
            timeframe="M5",
            session=session,
            d1_trend=d1_trend,
            h4_trend=h4_trend,
            db_path=self.db_path,
            **clean_values,
        )

        return snapshot_id