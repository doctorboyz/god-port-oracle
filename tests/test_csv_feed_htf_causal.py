"""Causal proof test: CSV fallback must load H1/D1 directly when feed files exist.

Hypothesis
----------
LiveTrader._fetch_candles_csv resamples ALL higher timeframes from
tail(500) of M5 — 500 M5 bars ≈ 2 days → D1 has ~2 bars, H4 has ~12.
_determine_d1_trend needs D1 ≥ 200 bars (EMA 50/200) and _compute_h4_trend
needs H4 ≥ 50 bars (EMA 10/50), so with no MT5 bridge the paper farm trades
blind: d1_trend="unknown", h4_trend=None on every cycle — exactly the
signals the confidence model needs most.

The paper farm fetcher writes XAUUSD_M5.csv + XAUUSD_H1.csv + XAUUSD_D1.csv
into the feed dir. _fetch_candles_csv must load H1/D1 directly (D1 → tail,
H4 → resample from H1) instead of resampling from the 2-day M5 tail.

Causal proof
------------
Feed dir with M5+D1 CSVs (D1 built to be EMA-bullish, 250 bars) →
_fetch_candles_csv returns D1 with ≥200 bars and _determine_d1_trend
returns "bullish". Control: dir with only M5 → legacy resample path,
D1 ≈ 2 bars, trend "unknown" (unchanged behavior).

This test FAILS (RED) before the fix, PASSES (GREEN) after.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

_M5_CSV = "XAUUSD_M5.csv"
_H1_CSV = "XAUUSD_H1.csv"
_D1_CSV = "XAUUSD_D1.csv"


def _write_candles_csv(path: Path, name: str, df: pd.DataFrame) -> None:
    """Write premium-format CSV: no header, date,open,high,low,close,volume."""
    out = df.copy()
    out.index.name = "date"
    out.to_csv(path / name, header=False, float_format="%.3f")


def _make_closes(n: int, base: float, step: float) -> pd.DataFrame:
    closes = pd.Series([base + i * step for i in range(n)])
    return pd.DataFrame({
        "open": closes,
        "high": closes + 1.0,
        "low": closes - 1.0,
        "close": closes,
        "volume": [100.0] * n,
    })


def _trader(data_dir: Path):
    from metty.execution.live_trader import LiveTrader
    return LiveTrader(account="B", dry_run=True, data_dir=data_dir)


class TestCsvFeedHtfCausal:
    """H1/D1 feed files must reach the trader as real HTF data."""

    def test_d1_csv_direct_load_gives_real_trend(self, tmp_path):
        # Arrange: M5 + D1 in feed dir; D1 rising 250 bars → EMA50>EMA200
        m5 = _make_closes(120, 4000.0, 0.5)
        m5.index = pd.date_range("2026-09-01", periods=120, freq="5min")
        d1 = _make_closes(250, 3000.0, 2.0)
        d1.index = pd.date_range("2026-01-01", periods=250, freq="D")
        _write_candles_csv(tmp_path, _M5_CSV, m5)
        _write_candles_csv(tmp_path, _D1_CSV, d1)

        # Act
        t = _trader(tmp_path)
        candles = t._fetch_candles_csv()

        # Assert: D1 is the real 250-bar history, trend computable → bullish
        assert candles is not None
        assert len(candles["D1"]) >= 200, (
            f"D1 must load directly from feed file, got {len(candles['D1'])} bars"
        )
        assert t._determine_d1_trend(candles["D1"]) == "bullish"

    def test_d1_bearish_direction_respected(self, tmp_path):
        # Arrange: falling D1 → EMA50<EMA200 → bearish (proves real data flows,
        # not a hardcoded label)
        m5 = _make_closes(120, 4000.0, 0.5)
        m5.index = pd.date_range("2026-09-01", periods=120, freq="5min")
        d1 = _make_closes(250, 5000.0, -2.0)
        d1.index = pd.date_range("2026-01-01", periods=250, freq="D")
        _write_candles_csv(tmp_path, _M5_CSV, m5)
        _write_candles_csv(tmp_path, _D1_CSV, d1)

        t = _trader(tmp_path)
        candles = t._fetch_candles_csv()
        assert t._determine_d1_trend(candles["D1"]) == "bearish"

    def test_h1_csv_gives_computable_h4_trend(self, tmp_path):
        # Arrange: M5 + H1 (rising 600 bars → H4 resample ≈ 100 bars)
        m5 = _make_closes(120, 4000.0, 0.5)
        m5.index = pd.date_range("2026-09-01", periods=120, freq="5min")
        h1 = _make_closes(600, 3000.0, 1.0)
        h1.index = pd.date_range("2026-06-01", periods=600, freq="h")
        _write_candles_csv(tmp_path, _M5_CSV, m5)
        _write_candles_csv(tmp_path, _H1_CSV, h1)

        t = _trader(tmp_path)
        candles = t._fetch_candles_csv()

        assert len(candles["H4"]) >= 50, (
            f"H4 resampled from H1 feed must reach EMA-window size, got {len(candles['H4'])}"
        )
        assert t._compute_h4_trend(candles["H4"]) == "bullish"

    def test_m5_only_keeps_legacy_resample_behavior(self, tmp_path):
        """Control: without H1/D1 feed files the old resample-from-M5 path stands."""
        m5 = _make_closes(500, 4000.0, 0.2)
        m5.index = pd.date_range("2026-09-01", periods=500, freq="5min")
        _write_candles_csv(tmp_path, _M5_CSV, m5)

        t = _trader(tmp_path)
        candles = t._fetch_candles_csv()

        assert candles is not None
        # Legacy: D1 resampled from 2 days of M5 → few bars, trend unknown
        assert len(candles["D1"]) < 200
        assert t._determine_d1_trend(candles["D1"]) == "unknown"

    def test_all_timeframes_present_and_m5_window_kept(self, tmp_path):
        """M5 still tail(WINDOW_SIZE); all four TFs present in the result."""
        m5 = _make_closes(800, 4000.0, 0.2)
        m5.index = pd.date_range("2026-09-01", periods=800, freq="5min")
        h1 = _make_closes(600, 3000.0, 1.0)
        h1.index = pd.date_range("2026-06-01", periods=600, freq="h")
        d1 = _make_closes(250, 3000.0, 2.0)
        d1.index = pd.date_range("2026-01-01", periods=250, freq="D")
        _write_candles_csv(tmp_path, _M5_CSV, m5)
        _write_candles_csv(tmp_path, _H1_CSV, h1)
        _write_candles_csv(tmp_path, _D1_CSV, d1)

        t = _trader(tmp_path)
        candles = t._fetch_candles_csv()

        from metty.execution.historical_collector import WINDOW_SIZE
        assert set(candles.keys()) == {"M5", "H1", "H4", "D1"}
        assert len(candles["M5"]) <= WINDOW_SIZE
        for tf, df in candles.items():
            assert not df.empty, f"{tf} empty"
            assert {"open", "high", "low", "close", "volume"} <= set(df.columns)