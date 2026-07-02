"""Causal proof tests for m5_scalp_trader._compute_d1_trend NULL bug (2026-07-02).

Hypothesis
----------
On demo B/C/D, 66 m5_scalp trades had d1_trend=NULL in DB (22+24+20 per account).
Swing trader's 42 trades all had d1_trend populated; scalp_trader uses _d1_proxy
defaulting to "unknown". Only m5_scalp_trader had NULL.

Root cause: `m5_scalp_trader._compute_d1_trend` returns `Optional[str]` and
returns `None` when H1 data is missing or insufficient. The None is passed
to `insert_live_trade(d1_trend=None)` which stores NULL in the DB.

Compare with `live_trader._determine_d1_trend` which always returns a string
("bullish"/"bearish"/"unknown") — never None. And scalp_trader's _d1_proxy
which defaults to "unknown".

Impact:
  - DB records NULL d1_trend for 66 m5_scalp trades
  - Reversal gate (which reads d1_trend) cannot classify these trades
  - Stats/ML cannot group m5_scalp trades by D1 trend
  - Counter-trend detection on m5_scalp is broken

Fix: m5_scalp_trader._compute_d1_trend must return "unknown" instead of None
when H1 data is unavailable. This matches the contract of every other trend
computation function in the codebase.

References
----------
- Bug found during counter-trend audit on 2026-07-02
- Function: metty/execution/m5_scalp_trader.py:_compute_d1_trend
- Production data: 66/66 NULL d1_trend trades are m5_scalp (22 B + 24 C + 20 D)
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _make_trader():
    """Construct an M5ScalpTrader instance without running it."""
    from metty.execution.m5_scalp_trader import M5ScalpTrader
    return M5ScalpTrader(account="C", dry_run=True)


class TestComputeD1TrendReturnsUnknown:
    """_compute_d1_trend must NEVER return None — return "unknown" instead."""

    def test_empty_candles_returns_unknown_not_none(self):
        """When candles dict has no H1 data, must return "unknown"."""
        t = _make_trader()
        result = t._compute_d1_trend({})
        assert result is not None, (
            "_compute_d1_trend must return 'unknown' not None — "
            "None causes DB to store NULL d1_trend, breaking reversal gate "
            "and trend-grouped stats"
        )
        assert result == "unknown", (
            f"expected 'unknown', got {result!r}"
        )

    def test_h1_none_returns_unknown_not_none(self):
        """When H1 key exists but value is None, must return "unknown"."""
        t = _make_trader()
        result = t._compute_d1_trend({"H1": None})
        assert result is not None
        assert result == "unknown"

    def test_h1_short_returns_unknown_not_none(self):
        """When H1 has < 50 bars (insufficient for EMA50), must return "unknown"."""
        import pandas as pd
        t = _make_trader()
        # 10 bars — far below the 50-bar threshold
        short_h1 = pd.DataFrame({
            "close": [4100.0 + i for i in range(10)],
            "high": [4105.0 + i for i in range(10)],
            "low": [4095.0 + i for i in range(10)],
            "volume": [100.0] * 10,
        })
        result = t._compute_d1_trend({"H1": short_h1})
        assert result is not None
        assert result == "unknown", (
            f"insufficient H1 data must return 'unknown', got {result!r}"
        )

    def test_h1_exception_returns_unknown_not_none(self):
        """When H1 computation raises, must return "unknown" (currently returns None)."""
        t = _make_trader()
        # Pass a malformed H1 that will trigger an exception in EMA calc
        bad_h1 = object()  # not a DataFrame — will fail on .ewm()
        result = t._compute_d1_trend({"H1": bad_h1})
        assert result is not None
        assert result == "unknown"

    def test_returns_string_for_valid_h1(self):
        """Sanity: with valid H1 data, must return 'bullish' or 'bearish' (string)."""
        import pandas as pd
        t = _make_trader()
        # 60 bars with rising prices → bullish
        rising = pd.DataFrame({
            "close": [4100.0 + i * 2 for i in range(60)],
            "high": [4105.0 + i * 2 for i in range(60)],
            "low": [4095.0 + i * 2 for i in range(60)],
            "volume": [100.0] * 60,
        })
        result = t._compute_d1_trend({"H1": rising})
        assert result is not None
        assert isinstance(result, str)
        assert result in ("bullish", "bearish"), (
            f"valid H1 must return bullish/bearish, got {result!r}"
        )

    def test_contract_matches_swing_trader(self):
        """Contract must match live_trader._determine_d1_trend — always returns
        a string, never None. Swing trader is the reference implementation."""
        from metty.execution.live_trader import LiveTrader
        swing = LiveTrader(account="A", dry_run=True)
        m5 = _make_trader()

        # Both must return "unknown" for empty/None input — same contract
        assert swing._determine_d1_trend(None) == "unknown"
        assert m5._compute_d1_trend({}) == "unknown", (
            "m5_scalp must match swing trader contract: return 'unknown' for "
            "missing data, not None"
        )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])