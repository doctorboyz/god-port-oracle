"""Tests for data pipeline — loader and resampler."""

import pytest
import pandas as pd
import numpy as np
from pathlib import Path
from datetime import datetime, timedelta

from broky.data.loader import load_csv, load_timeframe
from broky.data.resampler import resample_timeframe


def _create_test_csv(tmp_path: Path, n_rows: int = 200, freq: str = "5min") -> Path:
    """Create a test CSV file with synthetic OHLCV data."""
    start = datetime(2026, 1, 1, 0, 0)
    dates = pd.date_range(start=start, periods=n_rows, freq=freq)
    np.random.seed(42)

    base_price = 1900.0
    changes = np.random.normal(0.1, 1.5, n_rows)
    closes = base_price + np.cumsum(changes)
    opens = closes - np.random.uniform(0.5, 2, n_rows)
    highs = np.maximum(opens, closes) + np.random.uniform(0.5, 3, n_rows)
    lows = np.minimum(opens, closes) - np.random.uniform(0.5, 3, n_rows)
    volumes = np.random.uniform(500, 5000, n_rows)

    df = pd.DataFrame({
        "Date": dates,
        "Open": opens,
        "High": highs,
        "Low": lows,
        "Close": closes,
        "Volume": volumes,
    })

    filepath = tmp_path / "XAUUSD_M5.csv"
    df.to_csv(filepath, index=False, header=False)
    return filepath


class TestLoadCSV:
    def test_load_valid_csv(self, tmp_path):
        filepath = _create_test_csv(tmp_path, n_rows=100)
        df = load_csv(filepath)
        assert len(df) == 100
        assert set(df.columns) == {"Open", "High", "Low", "Close", "Volume"}

    def test_load_csv_has_datetime_index(self, tmp_path):
        filepath = _create_test_csv(tmp_path, n_rows=50)
        df = load_csv(filepath)
        assert isinstance(df.index, pd.DatetimeIndex)

    def test_load_csv_sorted_by_date(self, tmp_path):
        filepath = _create_test_csv(tmp_path, n_rows=50)
        df = load_csv(filepath)
        assert df.index.is_monotonic_increasing

    def test_load_nonexistent_file_raises(self):
        with pytest.raises(FileNotFoundError):
            load_csv("/nonexistent/path.csv")

    def test_load_csv_no_nulls_in_ohlcv(self, tmp_path):
        filepath = _create_test_csv(tmp_path, n_rows=50)
        df = load_csv(filepath)
        for col in ["Open", "High", "Low", "Close"]:
            assert df[col].notna().all()


class TestLoadTimeframe:
    def test_load_from_directory(self, tmp_path):
        filepath = _create_test_csv(tmp_path, n_rows=50)
        df = load_timeframe(tmp_path, timeframe="M5")
        assert len(df) == 50

    def test_load_nonexistent_directory_raises(self):
        with pytest.raises(FileNotFoundError):
            load_timeframe("/nonexistent/dir", timeframe="M5")

    def test_load_missing_timeframe_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            load_timeframe(tmp_path, timeframe="H1")


class TestResampleTimeframe:
    def test_resample_m5_to_h1(self, tmp_path):
        filepath = _create_test_csv(tmp_path, n_rows=300, freq="5min")
        df = load_csv(filepath)
        h1 = resample_timeframe(df, "H1")
        # 300 * 5min = 1500min = 25 hours → at most 25 hourly candles
        assert len(h1) <= 25
        assert len(h1) > 0

    def test_resample_m5_to_d1(self, tmp_path):
        filepath = _create_test_csv(tmp_path, n_rows=3000, freq="5min")
        df = load_csv(filepath)
        d1 = resample_timeframe(df, "D1")
        # Should produce daily candles
        assert len(d1) > 0
        assert len(d1) < len(df)

    def test_resample_preserves_ohlv_relationships(self, tmp_path):
        """After resampling: High >= max(Open,Close), Low <= min(Open,Close)."""
        filepath = _create_test_csv(tmp_path, n_rows=500, freq="5min")
        df = load_csv(filepath)
        h1 = resample_timeframe(df, "H1")
        assert (h1["High"] >= h1[["Open", "Close"]].max(axis=1)).all()
        assert (h1["Low"] <= h1[["Open", "Close"]].min(axis=1)).all()

    def test_resample_unknown_timeframe_raises(self, tmp_path):
        filepath = _create_test_csv(tmp_path, n_rows=50)
        df = load_csv(filepath)
        with pytest.raises(ValueError, match="Unknown timeframe"):
            resample_timeframe(df, "W1")

    def test_resample_volume_is_sum(self, tmp_path):
        filepath = _create_test_csv(tmp_path, n_rows=100, freq="5min")
        df = load_csv(filepath)
        h1 = resample_timeframe(df, "H1")
        # Volume in resampled should be sum of source volumes
        for idx in h1.index:
            source_mask = (df.index >= idx) & (df.index < idx + pd.Timedelta(hours=1))
            if source_mask.any():
                assert abs(h1.loc[idx, "Volume"] - df.loc[source_mask, "Volume"].sum()) < 1e-6