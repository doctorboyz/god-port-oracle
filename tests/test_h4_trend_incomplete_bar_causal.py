"""Causal proof test: H4 trend label noise from incomplete resample bar (Fix #2, 2026-07-12).

Hypothesis
----------
Real-A 07-10: 2 SELL trades (swing + m5_scalp) entered because M5 regime=trending
passed the ranging hard-block, BUT H4 was whipsawing (19+ flips in 8h per the
post-deploy doc). Both trades hit SL (-$12.16). The H4 trend label oscillated
intra-bar because:

  `broky/data/resampler.py:64` uses `df.resample(freq).agg(...)` which by default
  includes the current INCOMPLETE bin — the last row of the resampled H4 frame
  is the in-progress H4 bar whose `close` is the latest M5 close (not a closed
  H4 bar close). `_compute_h4_trend` then does `h4["close"].ewm(span=10/50)`
  and compares `ema10.iloc[-1] > ema50.iloc[-1]` — this uses the incomplete bar,
  so as M5 price oscillates around the EMA crossover, the H4 trend label flips
  on every cycle. Multi-TF confirmation becomes as noisy as M5 → false H4
  agreement → bad entry.

Causal proof
------------
Build an H4 close series where the first N-1 bars are a stable bullish trend
(EMA10 well above EMA50) and ONLY the last (incomplete) bar's close oscillates
around the EMA50 level. Then:

  - WITH incomplete bar (current production): label flips between bullish/bearish
    as the last close moves above/below EMA50 — this is the noise that produced
    19+ flips in 8h.
  - WITHOUT incomplete bar (drop last row before EMA): label stays bullish
    regardless of the last close, because the last CLOSED bar is bullish.

If dropping the last row yields a stable label while keeping it yields a
flipping label on the identical series, the cause is proven: the incomplete
resample bar injects intra-bar noise into the H4 trend label.

The env-var override test (TestH4ClosedBarOnlyEnvOverride) verifies the fix
mechanism: H4_USE_CLOSED_BAR_ONLY=1 (default) drops the last row; =0 keeps
legacy behavior for rollback safety. FAILS before the env hook is added,
PASSES after.

References
----------
- Learning: ψ/memory/learnings/2026-07-12_real-a-post-deploy-3fixes-check.md (concern #3)
- Production: broky/data/resampler.py:64 (resample includes incomplete bin)
- Production: metty/execution/live_trader.py:598 (_compute_h4_trend uses iloc[-1])
- Production: metty/execution/m5_scalp_trader.py:458 (same)
- Production: metty/execution/live_collector.py:69 (same)
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from broky.indicators.ema import calculate_ema  # noqa: E402


def _make_h4_with_incomplete_last_bar(
    n_closed: int = 60,
    closed_trend: str = "bullish",
    last_close_override: float | None = None,
) -> pd.DataFrame:
    """Build an H4 dataframe whose first `n_closed` bars sit near an EMA10/50
    crossover (choppy, like Real-A 07-10 H4 whipsaw) and whose last bar (the
    incomplete one) has a close we control.

    Pattern: 50 bars at 100, then 10 bars at 110 → EMA10 ≈ 108.7, EMA50 ≈ 103.3
    on closed bars → mildly bullish. A low incomplete-bar close (~70) drags
    EMA10 below EMA50 and flips the label to bearish; a high close (~150) keeps
    it bullish. This mirrors the post-deploy '19+ flips in 8h' scenario where
    H4 EMA10/50 were close and the in-progress close oscillated around the
    crossover.

    Bars are 4h apart starting 2026-01-01 00:00 UTC. The incomplete bar is the
    final row.
    """
    closes = [100.0] * 50 + [110.0] * (n_closed - 50)
    if last_close_override is not None:
        closes.append(last_close_override)  # the incomplete bar
    else:
        closes.append(closes[-1])  # neutral default
    idx = pd.date_range("2026-01-01 00:00", periods=len(closes), freq="4h", tz="UTC")
    return pd.DataFrame({"close": closes}, index=idx)


def _h4_trend_with_incomplete(h4: pd.DataFrame) -> str | None:
    """Mirror current production: EMA10/50 on full series, compare iloc[-1]."""
    if len(h4) < 50:
        return None
    ema10 = calculate_ema(h4["close"], 10).iloc[-1]
    ema50 = calculate_ema(h4["close"], 50).iloc[-1]
    if pd.isna(ema10) or pd.isna(ema50):
        return None
    if ema10 > ema50:
        return "bullish"
    if ema10 < ema50:
        return "bearish"
    return None


def _h4_trend_closed_only(h4: pd.DataFrame) -> str | None:
    """Fix: drop the last (incomplete) bar before EMA, then compare iloc[-1]."""
    if len(h4) < 51:  # need ≥50 closed bars after dropping the last
        return None
    closed = h4.iloc[:-1]
    ema10 = calculate_ema(closed["close"], 10).iloc[-1]
    ema50 = calculate_ema(closed["close"], 50).iloc[-1]
    if pd.isna(ema10) or pd.isna(ema50):
        return None
    if ema10 > ema50:
        return "bullish"
    if ema10 < ema50:
        return "bearish"
    return None


class TestH4IncompleteBarNoiseCausalProof:
    """Causal proof: incomplete resample bar injects noise into H4 trend label."""

    def test_with_incomplete_bar_label_flips_with_last_close(self):
        """With the incomplete bar, the SAME closed-bar history produces
        DIFFERENT labels depending on where the last (in-progress) close sits
        relative to EMA50. This is the intra-bar noise."""
        # Last close well above EMA50 → label bullish
        h4_up = _make_h4_with_incomplete_last_bar(last_close_override=210.0)
        # Last close well below EMA50 → label bearish (same closed history!)
        h4_down = _make_h4_with_incomplete_last_bar(last_close_override=50.0)
        label_up = _h4_trend_with_incomplete(h4_up)
        label_down = _h4_trend_with_incomplete(h4_down)
        assert label_up != label_down, (
            f"Incomplete bar should make label depend on last close: "
            f"up={label_up}, down={label_down}"
        )

    def test_closed_only_label_stable_across_same_history(self):
        """Dropping the incomplete bar, both series share the same 60 closed
        bars → identical label regardless of the (now-ignored) last close."""
        h4_up = _make_h4_with_incomplete_last_bar(last_close_override=210.0)
        h4_down = _make_h4_with_incomplete_last_bar(last_close_override=50.0)
        label_up = _h4_trend_closed_only(h4_up)
        label_down = _h4_trend_closed_only(h4_down)
        assert label_up == label_down == "bullish", (
            f"Closed-bar-only should be stable bullish on both, "
            f"got up={label_up}, down={label_down}"
        )

    def test_incomplete_bar_can_flip_label_against_closed_trend(self):
        """Direct A/B on the SAME series: closed bars are bullish, but a low
        incomplete-bar close flips the production label to bearish — a false
        H4 trend that would have green-lit a counter-trend entry. The fix
        keeps the label bullish."""
        h4 = _make_h4_with_incomplete_last_bar(last_close_override=50.0)
        with_incomplete = _h4_trend_with_incomplete(h4)
        closed_only = _h4_trend_closed_only(h4)
        # Production (with incomplete) reads the noise → not bullish
        assert with_incomplete != "bullish", (
            f"Low incomplete close should flip label away from bullish, "
            f"got {with_incomplete}"
        )
        # Fix (closed only) reads the trend → bullish
        assert closed_only == "bullish", (
            f"Closed-bar-only should be bullish on a bullish closed history, "
            f"got {closed_only}"
        )

    def test_simulated_intra_bar_oscillation_produces_many_flips(self):
        """Simulate 96 cycles (8h / 5min) of M5 price oscillating around the
        EMA10/50 crossover on the incomplete H4 bar. Production label flips
        many times; the fix never flips. This is the '19+ flips in 8h' from
        the post-deploy doc."""
        # Closed history: 50@100 + 10@110 → EMA10≈108.7, EMA50≈103.3, gap≈5.4.
        # The crossover zone is ~106. We oscillate the last close between 70
        # (drags EMA10 below EMA50 → bearish) and 150 (keeps bullish).
        flips_with_incomplete = 0
        flips_closed_only = 0
        prev_with = None
        prev_closed = None
        for i in range(96):
            # Square wave: 70 (bearish) on odd i, 150 (bullish) on even i
            last_close = 150.0 if (i % 2 == 0) else 70.0
            h4 = _make_h4_with_incomplete_last_bar(last_close_override=last_close)
            with_lbl = _h4_trend_with_incomplete(h4)
            closed_lbl = _h4_trend_closed_only(h4)
            if prev_with is not None and with_lbl != prev_with:
                flips_with_incomplete += 1
            if prev_closed is not None and closed_lbl != prev_closed:
                flips_closed_only += 1
            prev_with = with_lbl
            prev_closed = closed_lbl
        # Production flips on every transition (square wave) → ~95 flips
        assert flips_with_incomplete >= 3, (
            f"Production label should flip multiple times under oscillation, "
            f"got {flips_with_incomplete}"
        )
        # Fix never flips — closed bars don't change
        assert flips_closed_only == 0, (
            f"Closed-bar-only should never flip under incomplete-bar oscillation, "
            f"got {flips_closed_only}"
        )


class TestH4ClosedBarOnlyEnvOverride:
    """GREEN test for the fix: H4_USE_CLOSED_BAR_ONLY env var toggles the
    drop-last-row behavior in all three production _compute_h4_trend functions.
    FAILS before the env hook is added, PASSES after."""

    def test_live_trader_drops_last_bar_by_default(self, monkeypatch):
        monkeypatch.setenv("H4_USE_CLOSED_BAR_ONLY", "1")
        from metty.execution.live_trader import LiveTrader
        t = LiveTrader(account="A", dry_run=True)
        # Build a bullish closed history with a low incomplete last close
        h4 = _make_h4_with_incomplete_last_bar(last_close_override=50.0)
        label = t._compute_h4_trend(h4)
        assert label == "bullish", (
            f"With H4_USE_CLOSED_BAR_ONLY=1, low incomplete close must be ignored; "
            f"got {label}"
        )

    def test_live_trader_legacy_when_disabled(self, monkeypatch):
        """H4_USE_CLOSED_BAR_ONLY=0 keeps legacy behavior (last bar included)."""
        monkeypatch.setenv("H4_USE_CLOSED_BAR_ONLY", "0")
        from metty.execution.live_trader import LiveTrader
        t = LiveTrader(account="A", dry_run=True)
        h4 = _make_h4_with_incomplete_last_bar(last_close_override=50.0)
        label = t._compute_h4_trend(h4)
        # Legacy: low last close drags EMA10 below EMA50 → not bullish
        assert label != "bullish", (
            f"Legacy mode should read incomplete bar (not bullish on low close), "
            f"got {label}"
        )

    def test_m5_scalp_trader_drops_last_bar_by_default(self, monkeypatch):
        monkeypatch.setenv("H4_USE_CLOSED_BAR_ONLY", "1")
        from metty.execution.m5_scalp_trader import M5ScalpTrader
        t = M5ScalpTrader(account="A", dry_run=True)
        h4 = _make_h4_with_incomplete_last_bar(last_close_override=50.0)
        label = t._compute_h4_trend({"H4": h4})
        assert label == "bullish", (
            f"M5ScalpTrader should drop incomplete bar by default; got {label}"
        )

    def test_live_collector_drops_last_bar_by_default(self, monkeypatch):
        monkeypatch.setenv("H4_USE_CLOSED_BAR_ONLY", "1")
        from metty.execution.live_collector import _compute_h4_trend
        h4 = _make_h4_with_incomplete_last_bar(last_close_override=50.0)
        label = _compute_h4_trend(h4)
        assert label == "bullish", (
            f"live_collector._compute_h4_trend should drop incomplete bar; got {label}"
        )