"""Causal proof tests for bridge fetch retry in live_collector (ISSUE-088/072).

Hypothesis
----------
`LiveCollector._load_candles_from_bridge` fetches each timeframe exactly
ONCE with no retry:

  for tf in ["M5", "H1", "H4", "D1"]:
      df = bridge.fetch_candles_sync("XAUUSD", tf, 500)
      if not df.empty:
          candles[tf] = _normalize_columns(df)

so a single transient bridge hiccup silently drops that timeframe for the
whole 5-minute cycle:

- M5 empty (ISSUE-088, ~2-4% of cycles under thread contention) →
  `_compute_snapshot` gets m5=None → snapshot skipped → open trade's
  time-stop drifts (trade #2 exited after 2h05m instead of 1h).
- D1 fetch raising (ISSUE-072, bridge disconnect mid-cycle) → the outer
  `except Exception` aborts ALL remaining fetches → snapshot skipped with
  "No numeric types to aggregate" / "Indicator computation failed" in logs.

Both self-recover by the NEXT cycle, but every dropped cycle is a missing
feature_snapshot + a time-stop tick lost for open trades.

Fix under test: per-timeframe retry with backoff — an empty/None/raising
fetch is retried (bounded, default 3 attempts) before the timeframe is
given up on, and one timeframe failing no longer aborts the rest.

References
----------
- metty/execution/live_collector.py:_load_candles_from_bridge
- ISSUE-088 (M5 flakiness, time-stop drift), ISSUE-072 (D1 disconnect,
  'No numeric types to aggregate')
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _make_collector():
    from metty.execution.live_collector import LiveCollector

    os.environ.setdefault("MT5_BRIDGE_B_HOST", "localhost")
    os.environ.setdefault("MT5_BRIDGE_B_PORT", "8001")
    os.environ.setdefault("MT5_LOGIN_B", "1")
    os.environ.setdefault("MT5_PASSWORD_B", "x")
    return LiveCollector(account="B", db_path=Path("/tmp/test-collector.db"))


def _good_df(bars: int = 60, start_hour: int = 0) -> pd.DataFrame:
    """Realistic M5 candle frame (UTC datetime index, lowercase OHLCV)."""
    idx = pd.date_range("2026-09-23", periods=bars, freq="5min", tz="UTC")
    # named index — matches real bridge data so resample fallback works
    idx.name = "timestamp"
    base = 4000.0
    return pd.DataFrame(
        {
            "open": [base + i * 0.5 for i in range(bars)],
            "high": [base + i * 0.5 + 1.0 for i in range(bars)],
            "low": [base + i * 0.5 - 1.0 for i in range(bars)],
            "close": [base + i * 0.5 + 0.5 for i in range(bars)],
            "volume": [100.0 + i for i in range(bars)],
        },
        index=idx,
    )


class _FlakyBridge:
    """Bridge stub whose fetch_candles_sync fails transiently, then recovers.

    fail_first[tf] = how many leading calls for that timeframe fail
    (with empty DataFrame or an exception, per raise_mode).
    """

    def __init__(self, config, fail_first=None, raise_mode=False):
        self.config = config
        self.fail_first = dict(fail_first or {})
        self.raise_mode = raise_mode
        self.calls: dict[str, int] = {}

    def fetch_candles_sync(self, symbol, tf, count):
        self.calls[tf] = self.calls.get(tf, 0) + 1
        if self.calls[tf] <= self.fail_first.get(tf, 0):
            if self.raise_mode:
                raise ConnectionError("bridge connection lost")
            return pd.DataFrame()
        return _good_df(500 if tf == "D1" else 100)


class _DeadBridge:
    """Bridge stub that never returns data (persistent outage)."""

    def __init__(self, config):
        self.config = config

    def fetch_candles_sync(self, symbol, tf, count):
        raise ConnectionError("bridge down for good")


def _install_bridge(monkeypatch, bridge_cls):
    import metty.bridge.client as client_mod

    monkeypatch.setattr(client_mod, "MT5Bridge", bridge_cls)
    # backoff must not slow the test suite
    monkeypatch.setattr(time, "sleep", lambda s: None)


class TestBridgeFetchRetry:
    def test_empty_m5_first_call_recovers_via_retry(self, monkeypatch):
        """ISSUE-088 causal: M5 empty on first call must NOT drop M5 for the
        cycle — a retry must fetch real bars. Pre-fix: single fetch, M5
        missing → _load_candles_from_bridge returns None."""
        collector = _make_collector()
        _install_bridge(
            monkeypatch,
            lambda config: _FlakyBridge(config, fail_first={"M5": 1}),
        )
        candles = collector._load_candles_from_bridge()
        assert candles is not None, "M5 hiccup must not fail the whole cycle"
        assert "M5" in candles and len(candles["M5"]) == 100

    def test_d1_disconnect_first_call_recovers_via_retry(self, monkeypatch):
        """ISSUE-072 causal: D1 fetch raising must be retried, not abort all
        remaining fetches via the outer except. Pre-fix: exception →
        returns None, no candles at all."""
        collector = _make_collector()
        _install_bridge(
            monkeypatch,
            lambda config: _FlakyBridge(config, fail_first={"D1": 1}, raise_mode=True),
        )
        candles = collector._load_candles_from_bridge()
        assert candles is not None, "D1 disconnect must not fail the whole cycle"
        assert "D1" in candles and len(candles["D1"]) == 500

    def test_one_timeframe_failing_does_not_abort_others(self, monkeypatch):
        """A timeframe that fails on every attempt must not prevent the
        healthy timeframes from being returned. (H4 itself falls back to
        the existing M5-resample path — that fallback is correct behavior.)"""
        collector = _make_collector()
        _install_bridge(monkeypatch, lambda config:
                        _FlakyBridge(config, fail_first={"H4": 99}, raise_mode=True))
        candles = collector._load_candles_from_bridge()
        assert candles is not None
        assert "M5" in candles and "H1" in candles and "D1" in candles

    def test_persistent_outage_still_returns_none_bounded(self, monkeypatch):
        """Control/guard: persistent outage must not hang — bounded retries,
        then the documented None fallback (caller falls back to CSV)."""
        collector = _make_collector()
        _install_bridge(monkeypatch, lambda config: _DeadBridge(config))
        assert collector._load_candles_from_bridge() is None