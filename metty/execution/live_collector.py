"""Live data collector — collects feature snapshots from live MT5 data.

Runs in a loop: fetch candles → compute indicators → enrich with
sentiment → write to SQLite. Designed for Phase 5 ML data collection.
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

from broky.data.calendar import fetch_calendar, should_avoid_trading
from broky.data.sentiment import get_sentiment_snapshot
from broky.data.news import fetch_market_news, news_to_sentiment_score
from broky.signals.group_engine import GroupCoordinator
from broky.signals.groups.base import compute_all_indicators
from metty.core.account_registry import get_display_name
from metty.core.db import (
    get_connection,
    init_db,
    insert_feature_snapshot,
    insert_signal,
)

logger = logging.getLogger(__name__)

# Account IDs for live data collection
# These correspond to accounts in the 'accounts' table
LIVE_ACCOUNT_IDS = {"A": 1, "B": 2, "C": 3, "D": 4}


def _classify_session(timestamp: pd.Timestamp | datetime) -> str:
    """Classify trading session from UTC timestamp."""
    if isinstance(timestamp, datetime):
        hour = timestamp.hour
    else:
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


def fetch_live_sentiment() -> dict:
    """Fetch all sentiment data and return a flat dict for feature columns.

    Returns dict with: fear_greed_value, gold_bias_strength, news_sentiment
    """
    result = {
        "fear_greed_value": None,
        "gold_bias_strength": None,
        "news_sentiment": None,
    }

    try:
        snap = get_sentiment_snapshot()
        if snap.get("fear_greed_value") is not None:
            result["fear_greed_value"] = snap["fear_greed_value"]
        if snap.get("gold_bias_strength") is not None:
            result["gold_bias_strength"] = snap["gold_bias_strength"]
    except Exception as e:
        logger.warning("Fear & Greed fetch failed: %s", e)

    try:
        news = fetch_market_news()
        if news:
            result["news_sentiment"] = news_to_sentiment_score(news)
    except Exception as e:
        logger.warning("News sentiment fetch failed: %s", e)

    return result


def fetch_calendar_events() -> list:
    """Fetch upcoming economic calendar events."""
    try:
        return fetch_calendar(days_ahead=2, filter_currencies={"USD"})
    except Exception as e:
        logger.warning("Calendar fetch failed: %s", e)
        return []


class LiveCollector:
    """Collects feature snapshots from live MT5 bridge data.

    Each cycle:
    1. Fetch latest M5 candles from MT5 bridge
    2. Compute all indicator groups
    3. Enrich with sentiment data (Fear & Greed, news)
    4. Check economic calendar for news avoidance
    5. Write feature snapshot to SQLite

    Usage:
        collector = LiveCollector(account="A", db_path="data/oracle.db")
        collector.run_once()  # Single collection cycle
        collector.run(interval=300, max_cycles=0)  # Continuous loop
    """

    def __init__(
        self,
        account: str = "A",
        db_path: Optional[Path] = None,
        data_dir: Optional[Path] = None,
    ):
        self.account = account.upper()
        self.display_name = get_display_name(self.account)
        self.db_path = db_path
        self.data_dir = data_dir or Path("data/xau-data")
        self.account_id = LIVE_ACCOUNT_IDS.get(self.account, 2)
        self.coordinator = GroupCoordinator()
        self._calendar_cache: list = []
        self._calendar_cache_time: float = 0
        self._sentiment_cache: dict = {}
        self._sentiment_cache_time: float = 0

    def _get_calendar(self) -> list:
        """Get calendar events with 1-hour cache."""
        now = time.time()
        if now - self._calendar_cache_time > 3600:
            self._calendar_cache = fetch_calendar_events()
            self._calendar_cache_time = now
        return self._calendar_cache

    def _get_sentiment(self) -> dict:
        """Get sentiment data with 15-minute cache."""
        now = time.time()
        if now - self._sentiment_cache_time > 900:
            self._sentiment_cache = fetch_live_sentiment()
            self._sentiment_cache_time = now
        return self._sentiment_cache

    def _load_candles_from_csv(self) -> Optional[dict[str, pd.DataFrame]]:
        """Load latest candle data from CSV as fallback when MT5 bridge is unavailable.

        Uses the last WINDOW_SIZE bars from the CSV to compute indicators,
        matching the historical collector approach.
        """
        from broky.data.loader import load_timeframe
        from broky.data.resampler import resample_timeframe
        from metty.execution.historical_collector import _normalize_columns, WINDOW_SIZE

        try:
            m5_raw = load_timeframe(self.data_dir, "M5")
            if m5_raw.empty:
                return None

            # Use last WINDOW_SIZE bars for indicator computation
            m5_raw = m5_raw.tail(WINDOW_SIZE)

            m5 = _normalize_columns(m5_raw)
            h1 = _normalize_columns(resample_timeframe(m5_raw, "H1"))
            h4 = _normalize_columns(resample_timeframe(m5_raw, "H4"))
            d1 = _normalize_columns(resample_timeframe(m5_raw, "D1"))

            return {"M5": m5, "H1": h1, "H4": h4, "D1": d1}
        except Exception as e:
            logger.error("Failed to load candles from CSV: %s", e)
            return None

    def _load_candles_from_bridge(self) -> Optional[dict[str, pd.DataFrame]]:
        """Load candle data from MT5 bridge using synchronous wrapper methods.

        Connects to the VPS bridge and fetches live candles from MT5.
        Falls back to CSV if bridge is unavailable.
        """
        try:
            from metty.bridge.client import MT5Bridge
            from metty.core.models import AccountConfig, AccountName
            from broky.data.resampler import resample_timeframe
            from metty.execution.historical_collector import _normalize_columns

            # Map account letter to config
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
                logger.warning("Unknown account: %s", self.display_name)
                return None

            bridge = MT5Bridge(config)

            # Fetch each timeframe in a separate asyncio.run() call
            # (each creates its own event loop + connection)
            logger.info("Connecting to MT5 bridge for account %s...", self.display_name)

            candles = {}
            for tf in ["M5", "H1", "H4", "D1"]:
                df = bridge.fetch_candles_sync("XAUUSD", tf, 500)
                if not df.empty:
                    candles[tf] = _normalize_columns(df)
                    logger.info("  %s: %d bars from MT5", tf, len(df))

            if not candles:
                logger.warning("No data from MT5 bridge for account %s", self.display_name)
                return None

            # Resample M5 if higher TFs not available
            if "M5" in candles:
                m5 = candles["M5"]
                if "H1" not in candles:
                    candles["H1"] = _normalize_columns(resample_timeframe(m5.reset_index(), "H1"))
                if "D1" not in candles:
                    candles["D1"] = _normalize_columns(resample_timeframe(m5.reset_index(), "D1"))
                if "H4" not in candles:
                    candles["H4"] = _normalize_columns(resample_timeframe(m5.reset_index(), "H4"))

            return candles

        except ImportError:
            logger.info("MT5 bridge not available, using CSV fallback")
            return None
        except Exception as e:
            logger.warning("MT5 bridge fetch failed: %s", e)
            return None

    def _compute_snapshot(
        self,
        candles: dict[str, pd.DataFrame],
        sentiment: dict,
    ) -> Optional[dict]:
        """Compute a full feature snapshot from candle data + sentiment."""
        m5 = candles.get("M5")
        d1 = candles.get("D1")
        if m5 is None or m5.empty:
            return None

        # Skip if M5 data is too short for indicators
        if len(m5) < 50:
            logger.warning("M5 data too short (%d bars), skipping", len(m5))
            return None

        try:
            # Compute all indicator groups
            full_snapshot = compute_all_indicators(candles)
        except Exception as e:
            logger.error("Indicator computation failed: %s", e)
            return None

        # Add price
        full_snapshot["price"] = float(m5["close"].iloc[-1])

        # Add Broky original indicators
        try:
            from metty.execution.historical_collector import compute_broky_indicators
            broky = compute_broky_indicators(m5)
            full_snapshot.update(broky)
        except Exception as e:
            logger.warning("Broky indicator computation failed: %s", e)

        # Session, D1 trend, and H4 trend
        timestamp = m5.index[-1]
        full_snapshot["session"] = _classify_session(timestamp)
        full_snapshot["d1_trend"] = _determine_d1_trend(d1) if d1 is not None else "unknown"
        h4 = candles.get("H4")
        full_snapshot["h4_trend"] = _compute_h4_trend(h4) if h4 is not None else "unknown"

        # Merge sentiment data
        full_snapshot["fear_greed_value"] = sentiment.get("fear_greed_value")
        full_snapshot["gold_bias_strength"] = sentiment.get("gold_bias_strength")
        full_snapshot["news_sentiment"] = sentiment.get("news_sentiment")

        # Multi-timeframe price context
        h1 = candles.get("H1")
        full_snapshot["h1_close"] = float(h1["close"].iloc[-1]) if h1 is not None and not h1.empty else None
        full_snapshot["h4_close"] = float(h4["close"].iloc[-1]) if h4 is not None and not h4.empty else None
        full_snapshot["d1_close"] = float(d1["close"].iloc[-1]) if d1 is not None and not d1.empty else None
        full_snapshot["m5_high"] = float(m5["high"].iloc[-1]) if m5 is not None and not m5.empty else None
        full_snapshot["m5_low"] = float(m5["low"].iloc[-1]) if m5 is not None and not m5.empty else None

        # Clean NaN values
        clean = {}
        for k, v in full_snapshot.items():
            if isinstance(v, float) and (pd.isna(v) or np.isnan(v)):
                clean[k] = None
            else:
                clean[k] = v

        return clean

    def run_once(self) -> Optional[int]:
        """Run a single collection cycle.

        Returns snapshot ID if successful, None if skipped.
        """
        init_db(self.db_path)

        # Try bridge first, fall back to CSV
        candles = self._load_candles_from_bridge()
        source = "bridge"
        if candles is None:
            candles = self._load_candles_from_csv()
            source = "csv"
        if candles is None:
            logger.error("No candle data available")
            return None

        # Fetch sentiment (cached)
        sentiment = self._get_sentiment()

        # Check calendar for news avoidance
        calendar = self._get_calendar()
        if should_avoid_trading(calendar):
            logger.info("High-impact news event nearby — still collecting snapshot")

        # Compute snapshot
        snapshot = self._compute_snapshot(candles, sentiment)
        if snapshot is None:
            logger.error("Failed to compute snapshot")
            return None

        price = snapshot.get("price", 0)
        if price <= 0:
            return None

        timestamp = candles["M5"].index[-1]
        ts_str = timestamp.isoformat() if hasattr(timestamp, "isoformat") else str(timestamp)

        session = snapshot.get("session", "unknown")
        d1_trend = snapshot.get("d1_trend", "unknown")
        h4_trend = snapshot.get("h4_trend", "unknown")

        # Insert signal record
        signal_id = insert_signal(
            timestamp=ts_str,
            group_name=f"live_{source}",
            direction="HOLD",
            confidence=0.0,
            triggering_indicators="",
            price=price,
            account_id=self.account_id,
            trading_mode="swing",
            strategy_id=f"collect-{self.account}",
            db_path=self.db_path,
        )

        # Separate indicator values from metadata
        metadata_keys = {"price", "session", "d1_trend", "h4_trend"}
        indicator_values = {k: v for k, v in snapshot.items() if k not in metadata_keys}

        # Insert feature snapshot
        snapshot_id = insert_feature_snapshot(
            signal_id=signal_id,
            timestamp=ts_str,
            price=price,
            timeframe="M5",
            session=session,
            d1_trend=d1_trend,
            h4_trend=h4_trend,
            trading_mode="swing",
            strategy_id=f"collect-{self.account}",
            db_path=self.db_path,
            **indicator_values,
        )

        logger.info(
            "Snapshot #%d collected (source=%s, price=%.2f, session=%s, sentiment=%s)",
            snapshot_id, source, price, session,
            f"fg={sentiment.get('fear_greed_value')}" if sentiment.get("fear_greed_value") else "none",
        )

        return snapshot_id

    def run(self, interval: int = 300, max_cycles: int = 0) -> dict:
        """Run continuous collection loop.

        Args:
            interval: Seconds between collection cycles (default 300 = 5min for M5).
            max_cycles: Max number of cycles (0 = infinite).

        Returns:
            Dict with stats: cycles, snapshots, errors.
        """
        cycle = 0
        snapshots = 0
        errors = 0

        logger.info("Starting live collection (interval=%ds, account=%s)", interval, self.display_name)

        while max_cycles == 0 or cycle < max_cycles:
            cycle += 1
            try:
                result = self.run_once()
                if result is not None:
                    snapshots += 1
                else:
                    errors += 1
            except Exception as e:
                logger.error("Collection cycle %d failed: %s", cycle, e)
                errors += 1

            logger.info(
                "Cycle %d complete (snapshots=%d, errors=%d)",
                cycle, snapshots, errors,
            )

            if max_cycles > 0 and cycle >= max_cycles:
                break

            logger.info("Sleeping %d seconds until next cycle...", interval)
            time.sleep(interval)

        return {"cycles": cycle, "snapshots": snapshots, "errors": errors}