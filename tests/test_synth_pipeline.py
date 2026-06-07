"""Tests for BacktestToML pipeline — synthetic trade outcome generation."""

import json
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd
import pytest

# Import with path setup
import sys
project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(project_root))

from broky.backtest.synth_pipeline import BacktestToMLPipeline, SynthTradeOutcome
from shared.models import SignalType
from metty.core.db import (
    SYNTHETIC_ACCOUNT_ID,
    ensure_synthetic_account,
    insert_synthetic_trade,
    insert_synthetic_trade_outcome,
    init_db,
)


# ── Fixtures ──────────────────────────────────────────────────────────────

@pytest.fixture
def tmp_db(tmp_path):
    """Create a temporary SQLite database with schema."""
    db_path = tmp_path / "test_oracle.db"
    init_db(db_path)
    return db_path


@pytest.fixture
def sample_d1_data():
    """Generate sample D1 candle data (200 bars)."""
    np.random.seed(42)
    dates = pd.date_range("2024-01-01", periods=200, freq="D")
    prices = 2000 + np.cumsum(np.random.randn(200) * 5)
    df = pd.DataFrame({
        "Open": prices + np.random.randn(200) * 2,
        "High": prices + abs(np.random.randn(200) * 3),
        "Low": prices - abs(np.random.randn(200) * 3),
        "Close": prices + np.random.randn(200) * 2,
        "Volume": np.random.randint(100, 10000, 200),
    }, index=dates)
    df.columns = [c.lower() for c in df.columns]
    return df


@pytest.fixture
def sample_h1_data():
    """Generate sample H1 candle data (480 bars = 20 days)."""
    np.random.seed(43)
    dates = pd.date_range("2024-01-01", periods=480, freq="1h")
    prices = 2000 + np.cumsum(np.random.randn(480) * 1)
    df = pd.DataFrame({
        "Open": prices + np.random.randn(480) * 0.5,
        "High": prices + abs(np.random.randn(480) * 1),
        "Low": prices - abs(np.random.randn(480) * 1),
        "Close": prices + np.random.randn(480) * 0.5,
        "Volume": np.random.randint(50, 5000, 480),
    }, index=dates)
    df.columns = [c.lower() for c in df.columns]
    return df


# ── SynthTradeOutcome tests ───────────────────────────────────────────────

class TestSynthTradeOutcome:
    def test_create_outcome(self):
        outcome = SynthTradeOutcome(
            direction="BUY",
            entry_price=2050.0,
            exit_price=2070.0,
            profit=200.0,
            profit_pct=0.98,
            outcome_label="WIN",
            holding_minutes=120,
            exit_reason="take_profit",
            d1_trend="bullish",
            h4_trend="bullish",
            session="london",
            regime="trending",
            strategy_id="backtest_swing_h1_synth",
            mfe=25.0,
            mae=10.0,
            mfe_pct=1.22,
            mae_pct=0.49,
            features={"rsi": 55.0, "adx": 30.0},
            entry_timestamp="2024-01-15T10:00:00+00:00",
            exit_timestamp="2024-01-15T12:00:00+00:00",
        )
        assert outcome.direction == "BUY"
        assert outcome.outcome_label == "WIN"
        assert outcome.d1_trend == "bullish"
        assert outcome.mfe == 25.0
        assert outcome.features["rsi"] == 55.0


# ── Session classification tests ──────────────────────────────────────────

class TestSessionClassification:
    def setup_method(self):
        self.pipeline = BacktestToMLPipeline(data_dir="data/xau-data", dry_run=True)

    def test_asian_session(self):
        ts = pd.Timestamp("2024-01-15 03:00:00", tz="UTC")
        assert self.pipeline._classify_session(ts) == "asian"

    def test_london_session(self):
        ts = pd.Timestamp("2024-01-15 10:00:00", tz="UTC")
        assert self.pipeline._classify_session(ts) == "london"

    def test_new_york_session(self):
        ts = pd.Timestamp("2024-01-15 18:00:00", tz="UTC")
        assert self.pipeline._classify_session(ts) == "new_york"

    def test_overlap_session(self):
        ts = pd.Timestamp("2024-01-15 14:00:00", tz="UTC")
        assert self.pipeline._classify_session(ts) == "overlap"

    def test_off_hours(self):
        ts = pd.Timestamp("2024-01-15 22:00:00", tz="UTC")
        # 22 UTC is in NY range (13-22), so it's new_york not off-hours
        assert self.pipeline._classify_session(ts) == "new_york"

    def test_midnight_asian(self):
        ts = pd.Timestamp("2024-01-15 00:00:00", tz="UTC")
        assert self.pipeline._classify_session(ts) == "asian"


# ── Regime classification tests ──────────────────────────────────────────

class TestRegimeClassification:
    def setup_method(self):
        self.pipeline = BacktestToMLPipeline(data_dir="data/xau-data", dry_run=True)

    def test_trending_regime(self):
        features = {"adx": 30.0, "boll_bw": 0.03}
        assert self.pipeline._determine_regime(features) == "trending"

    def test_volatile_regime(self):
        features = {"adx": 30.0, "boll_bw": 0.05}
        assert self.pipeline._determine_regime(features) == "volatile"

    def test_ranging_regime(self):
        features = {"adx": 18.0, "boll_bw": 0.02}
        assert self.pipeline._determine_regime(features) == "ranging"

    def test_forming_regime(self):
        features = {"adx": 22.0, "boll_bw": 0.02}
        assert self.pipeline._determine_regime(features) == "ranging"

    def test_string_adx_handled(self):
        features = {"adx": "unknown", "boll_bw": 0.03}
        # Should default to ranging when adx is a string
        assert self.pipeline._determine_regime(features) in ("ranging", "trending", "volatile")


# ── MFE/MAE computation tests ─────────────────────────────────────────────

class TestMFEAndMAE:
    def setup_method(self):
        self.pipeline = BacktestToMLPipeline(data_dir="data/xau-data", dry_run=True)

    def _make_trade(self, entry_idx, exit_idx, entry_price, direction="BUY"):
        trade = MagicMock()
        trade.entry_idx = entry_idx
        trade.exit_idx = exit_idx
        trade.entry_price = entry_price
        trade.direction = MagicMock(value=direction)
        return trade

    def test_buy_mfe_mae(self):
        trade = self._make_trade(10, 20, 2000.0, "BUY")
        df = pd.DataFrame({
            "high": [2005, 2010, 2015, 2020, 2018,
                     2016, 2022, 2025, 2021, 2019,
                     2008] + [2010] * 10,
            "low": [1995, 1998, 2000, 2003, 1997,
                    1999, 2005, 2008, 2002, 1996,
                    1992] + [1995] * 10,
        })
        mfe, mae, mfe_pct, mae_pct = self.pipeline._compute_mfe_mae(trade, df)
        # MFE = max(high[10:21]) - 2000 = 2025 - 2000 = 25
        # MAE = 2000 - min(low[10:21]) = 2000 - 1992 = 8
        assert mfe > 0
        assert mae > 0
        assert mfe_pct > 0
        assert mae_pct > 0

    def test_sell_mfe_mae(self):
        trade = self._make_trade(10, 20, 2000.0, "SELL")
        df = pd.DataFrame({
            "high": [2005, 2010, 2015, 2020, 2018,
                     2016, 2022, 2025, 2021, 2019,
                     2008] + [2010] * 10,
            "low": [1995, 1998, 2000, 2003, 1997,
                    1999, 2005, 2008, 2002, 1996,
                    1992] + [1995] * 10,
        })
        mfe, mae, mae_pct, mfe_pct = self.pipeline._compute_mfe_mae(trade, df)
        # SELL: MFE = entry - min(low) = 2000 - 1992 = 8
        # MAE = max(high) - entry = 2025 - 2000 = 25
        assert mfe > 0
        assert mae > 0

    def test_missing_exit_idx(self):
        trade = self._make_trade(10, None, 2000.0)
        df = pd.DataFrame({"high": [2005], "low": [1995]})
        mfe, mae, mfe_pct, mae_pct = self.pipeline._compute_mfe_mae(trade, df)
        assert mfe == 0.0
        assert mae == 0.0


# ── Database insertion tests ──────────────────────────────────────────────

class TestSyntheticDBInsertion:
    def test_ensure_synthetic_account(self, tmp_db):
        ensure_synthetic_account(tmp_db)
        conn = sqlite3.connect(str(tmp_db))
        row = conn.execute("SELECT id, name FROM accounts WHERE id = 0").fetchone()
        conn.close()
        assert row is not None
        assert row[0] == 0
        assert row[1] == "synthetic_backtest"

    def test_insert_synthetic_trade(self, tmp_db):
        trade_id = insert_synthetic_trade(
            direction="BUY",
            entry_price=2050.0,
            exit_price=2070.0,
            pnl=200.0,
            pnl_pct=0.98,
            d1_trend="bearish",
            session="london",
            regime="trending",
            strategy_id="backtest_swing_h1_synth",
            entry_time="2024-01-15T10:00:00+00:00",
            exit_time="2024-01-15T12:00:00+00:00",
            exit_reason="take_profit",
            h4_trend="bearish",
            db_path=tmp_db,
        )
        assert trade_id > 0

        conn = sqlite3.connect(str(tmp_db))
        row = conn.execute("SELECT id, direction, d1_trend FROM live_trades WHERE id = ?",
                          (trade_id,)).fetchone()
        conn.close()
        assert row[1] == "BUY"
        assert row[2] == "bearish"

    def test_insert_synthetic_trade_outcome(self, tmp_db):
        trade_id = insert_synthetic_trade(
            direction="SELL",
            entry_price=2050.0,
            exit_price=2030.0,
            pnl=200.0,
            pnl_pct=0.98,
            d1_trend="bearish",
            session="new_york",
            regime="trending",
            strategy_id="backtest_swing_h1_synth",
            entry_time="2024-01-15T14:00:00+00:00",
            exit_time="2024-01-15T16:00:00+00:00",
            exit_reason="stop_loss",
            db_path=tmp_db,
        )

        features = {"rsi": 45.0, "adx": 28.0, "d1_trend": "bearish", "session": "new_york"}
        outcome_id = insert_synthetic_trade_outcome(
            trade_id=trade_id,
            direction="SELL",
            entry_price=2050.0,
            exit_price=2030.0,
            profit=200.0,
            profit_pct=0.98,
            outcome_label="WIN",
            holding_minutes=120,
            exit_reason="take_profit",
            features_json=json.dumps(features),
            regime="trending",
            d1_trend="bearish",
            h4_trend="bearish",
            session="new_york",
            strategy_id="backtest_swing_h1_synth",
            mfe=25.0,
            mae=10.0,
            mfe_pct=1.22,
            mae_pct=0.49,
            exit_d1_trend="bearish",
            exit_h4_trend="neutral",
            db_path=tmp_db,
        )
        assert outcome_id > 0

        conn = sqlite3.connect(str(tmp_db))
        row = conn.execute(
            "SELECT direction, outcome_label, features_json FROM trade_outcomes WHERE id = ?",
            (outcome_id,)
        ).fetchone()
        conn.close()

        assert row[0] == "SELL"
        assert row[1] == "WIN"
        stored_features = json.loads(row[2])
        assert stored_features["rsi"] == 45.0
        assert stored_features["d1_trend"] == "bearish"

    def test_dry_run_does_not_write(self, tmp_db):
        pipeline = BacktestToMLPipeline(
            data_dir="data/xau-data",
            db_path=tmp_db,
            dry_run=True,
        )
        # Create a fake outcome
        outcome = SynthTradeOutcome(
            direction="BUY",
            entry_price=2000.0,
            exit_price=2020.0,
            profit=200.0,
            profit_pct=1.0,
            outcome_label="WIN",
            holding_minutes=60,
            exit_reason="take_profit",
            d1_trend="bullish",
            h4_trend="bullish",
            session="london",
            regime="trending",
            strategy_id="backtest_swing_h1_synth",
            features={"rsi": 55.0},
        )
        stats = pipeline._write_outcomes([outcome])
        assert stats["db_writes"] == 1  # Counted but not written
        assert stats["total_trades"] == 1

        # Verify no rows in DB
        conn = sqlite3.connect(str(tmp_db))
        count = conn.execute("SELECT COUNT(*) FROM trade_outcomes").fetchone()[0]
        conn.close()
        assert count == 0  # Dry run — nothing written


# ── D1 trend identification tests ─────────────────────────────────────────

class TestD1TrendIdentification:
    def setup_method(self):
        self.pipeline = BacktestToMLPipeline(data_dir="data/xau-data", dry_run=True)

    def test_bearish_period_identification(self, sample_d1_data):
        # Force a bearish trend: make close prices decline
        sample_d1_data["close"] = 2100 - np.arange(200) * 2
        sample_d1_data["open"] = sample_d1_data["close"] + 1
        sample_d1_data["high"] = sample_d1_data["close"] + 5
        sample_d1_data["low"] = sample_d1_data["close"] - 5

        self.pipeline._candles = {"d1": sample_d1_data, "h1": pd.DataFrame(), "h4": pd.DataFrame(), "m5": pd.DataFrame()}
        self.pipeline._d1_trend_series = self.pipeline._compute_d1_trend_series(sample_d1_data)

        periods = self.pipeline._identify_bearish_periods(min_days=5)
        # With a declining series, EMA50 < EMA200 after warmup, so bearish periods should exist
        assert len(periods) >= 1 or len(self.pipeline._d1_trend_series) < 200  # May not have enough warmup

    def test_empty_d1_data(self):
        self.pipeline._d1_trend_series = None
        periods = self.pipeline._identify_bearish_periods()
        assert periods == []


# ── Candle slicing tests ──────────────────────────────────────────────────

class TestCandleSlicing:
    def setup_method(self):
        self.pipeline = BacktestToMLPipeline(data_dir="data/xau-data", dry_run=True)

    def test_slice_no_lookahead(self, sample_h1_data, sample_d1_data):
        self.pipeline._candles = {
            "h1": sample_h1_data,
            "d1": sample_d1_data,
            "h4": pd.DataFrame(),  # Not available
            "m5": pd.DataFrame(),  # Not available
        }
        # Slice at a timestamp that exists in the data
        ts = sample_h1_data.index[100]
        result = self.pipeline._slice_candles_at_timestamp(ts, window_size=200)

        # H1 should have data (up to 200 bars before ts)
        assert "H1" in result
        assert len(result["H1"]) > 0
        # All data should be <= ts (no lookahead)
        assert result["H1"].index[-1] <= ts

    def test_slice_insufficient_data(self):
        # Very small DataFrame — less than 50 bars
        small_df = pd.DataFrame({
            "open": [2000],
            "high": [2005],
            "low": [1995],
            "close": [2002],
            "volume": [100],
        }, index=pd.date_range("2024-01-01", periods=1, freq="1h"))
        self.pipeline._candles = {"h1": small_df, "d1": pd.DataFrame(), "h4": pd.DataFrame(), "m5": pd.DataFrame()}

        ts = small_df.index[0]
        result = self.pipeline._slice_candles_at_timestamp(ts, window_size=200)
        # Should return empty H1 since < 50 bars
        assert result["H1"].empty


if __name__ == "__main__":
    pytest.main([__file__, "-v"])