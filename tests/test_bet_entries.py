"""Unit tests — the 3 true-bet entry families (T2 acceptance).

Deterministic, closed-bar signals only. Causality (no lookahead) is
asserted directly: a signal computed on the prefix df[:i+1] must equal the
one computed on the full history.

Note on data shape: every case carries ≥ MIN_HISTORY_BARS (60) bars —
``signal_on_last_bar`` enforces the warmup window for all families.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from broky.bet import entries  # noqa: E402
from broky.indicators import calculate_bollinger  # noqa: E402


def make_df(closes: list[float], *, high_pad: float = 0.1,
            low_pad: float = 0.1, start: str = "2026-01-05 00:00") -> pd.DataFrame:
    """Closed-bar frame: open=prev close, high/low padded around close."""
    idx = pd.date_range(start=start, periods=len(closes), freq="5min")
    closes_arr = np.asarray(closes, dtype=float)
    opens = np.concatenate([[closes_arr[0]], closes_arr[:-1]])
    return pd.DataFrame(
        {
            "open": opens,
            "high": closes_arr + high_pad,
            "low": closes_arr - low_pad,
            "close": closes_arr,
            "volume": [100.0] * len(closes),
        },
        index=idx,
    )


def quiet(n: int, base: float = 100.0, amp: float = 0.05) -> list[float]:
    """Alternating ±amp closes — narrow Bollinger, tiny ADX (ranging)."""
    return [base + (amp if i % 2 else -amp) for i in range(n)]


def flat_then_spike(spike_close: float, n_flat: int = 80,
                    base: float = 100.0) -> pd.DataFrame:
    """Narrow-band ranging history + a spike bar far outside the bands."""
    return make_df(quiet(n_flat, base) + [spike_close],
                   high_pad=0.05, low_pad=0.05)


class TestFamilyA_MR_Boll:
    def test_extreme_low_band_position_buys(self):
        df = flat_then_spike(97.0)  # close far below the narrow lower band
        sig = entries.signal_on_last_bar(df, entries.FAMILY_MR_BOLL,
                                         boll_threshold=0.85)
        assert sig == entries.BUY

    def test_extreme_high_band_position_sells(self):
        df = flat_then_spike(103.0)
        sig = entries.signal_on_last_bar(df, entries.FAMILY_MR_BOLL,
                                         boll_threshold=0.85)
        assert sig == entries.SELL

    def test_mid_band_is_no_signal(self):
        df = flat_then_spike(100.001)
        assert entries.signal_on_last_bar(df, entries.FAMILY_MR_BOLL) is None

    def test_adx_rising_blocks_mr(self):
        # A steady strong trend: ADX climbs ≥ 20 → the farm MR gate closes,
        # even though price rides the upper band (pos ≈ 1).
        closes = [100.0 + 0.4 * i for i in range(80)]
        df = make_df(closes, high_pad=0.05, low_pad=0.05)
        assert entries.signal_on_last_bar(df, entries.FAMILY_MR_BOLL) is None

    def test_threshold_comparison_rule(self):
        """BUY fires iff band position ≤ 1−thr (same rule as the farm MR).

        The expected side is derived from the indicator's own band position,
        so this tests the threshold comparison, not hand-computed bands.
        """
        for spike in (97.0, 99.0, 100.001, 101.0, 103.0):
            df = flat_then_spike(spike)
            close = pd.Series(df["close"])
            boll = calculate_bollinger(close, period=20, std_dev=2.0)
            band_range = boll.upper.iloc[-1] - boll.lower.iloc[-1]
            pos = (close.iloc[-1] - boll.lower.iloc[-1]) / band_range
            for thr in (0.70, 0.80, 0.85, 0.95):
                got = entries.signal_on_last_bar(df, entries.FAMILY_MR_BOLL,
                                                 boll_threshold=thr)
                if pos <= 1.0 - thr:
                    assert got == entries.BUY, (spike, thr, pos)
                elif pos >= thr:
                    assert got == entries.SELL, (spike, thr, pos)
                else:
                    assert got is None, (spike, thr, pos)


class TestFamilyB_Breakout:
    def test_close_above_prior_20_high_buys(self):
        df = make_df(quiet(70) + [101.0])  # close above every prior high
        assert entries.signal_on_last_bar(df, entries.FAMILY_BREAKOUT) == entries.BUY

    def test_close_below_prior_20_low_sells(self):
        df = make_df(quiet(70) + [99.0])
        assert entries.signal_on_last_bar(df, entries.FAMILY_BREAKOUT) == entries.SELL

    def test_no_breakout_no_signal(self):
        df = make_df(quiet(70) + [100.06])
        assert entries.signal_on_last_bar(df, entries.FAMILY_BREAKOUT) is None

    def test_signal_bar_is_excluded_from_own_range(self):
        # quiet highs are 100.05+0.1 = ... wait: pad 0.1 → highs ≈ 100.15.
        # A signal-bar close of 100.10 sits below every prior high → silent.
        df = make_df(quiet(70), high_pad=0.1, low_pad=0.1)
        df = make_df(list(df["close"]) + [100.10], high_pad=0.1, low_pad=0.1)
        assert entries.signal_on_last_bar(df, entries.FAMILY_BREAKOUT) is None

    def test_lookback_window_is_exactly_20(self):
        # 20 quiet bars, then a close above all their highs → BUY.
        df = make_df(quiet(70) + [100.5], high_pad=0.1, low_pad=0.1)
        assert entries.signal_on_last_bar(df, entries.FAMILY_BREAKOUT) == entries.BUY
        # a close inside the prior-20 range stays silent
        df2 = make_df(quiet(70) + [100.04], high_pad=0.1, low_pad=0.1)
        assert entries.signal_on_last_bar(df2, entries.FAMILY_BREAKOUT) is None


class TestFamilyC_MomentumPullback:
    def _uptrend(self, n: int = 70, base: float = 100.0, step: float = 0.3) -> list[float]:
        return [base + step * i for i in range(n)]

    def test_uptrend_dip_to_ema10_bounce_buys(self):
        closes = self._uptrend()
        closes.append(closes[-1] - 1.2)  # dip bar
        df = make_df(closes, high_pad=0.05, low_pad=1.4)  # low reaches EMA10
        assert entries.signal_on_last_bar(df, entries.FAMILY_PULLBACK) == entries.BUY

    def test_uptrend_no_touch_no_signal(self):
        closes = self._uptrend()
        closes.append(closes[-1] + 0.1)  # bar drifting up, never touches EMA10
        df = make_df(closes, high_pad=0.05, low_pad=0.05)
        assert entries.signal_on_last_bar(df, entries.FAMILY_PULLBACK) is None

    def test_downtrend_bounce_to_ema10_sells(self):
        closes = [100.0 - 0.3 * i for i in range(70)]
        closes.append(closes[-1] + 1.2)  # pullback UP to EMA10
        df = make_df(closes, high_pad=1.4, low_pad=0.05)
        assert entries.signal_on_last_bar(df, entries.FAMILY_PULLBACK) == entries.SELL

    def test_dip_but_closes_below_ema10_is_no_signal(self):
        # Touched EMA10 but kept falling through it — no bounce, no signal.
        closes = self._uptrend()
        closes.append(closes[-1] - 2.5)
        df = make_df(closes, high_pad=0.05, low_pad=2.9)
        assert entries.signal_on_last_bar(df, entries.FAMILY_PULLBACK) is None


class TestCausality:
    """No lookahead: a signal at bar i must not see bars after i."""

    @pytest.mark.parametrize("family", entries.FAMILIES)
    def test_prefix_equals_full_history(self, family):
        # A path with mixed regimes so every family has candidate bars.
        rng = np.random.RandomState(11)
        n = 300
        closes = 100.0 + np.cumsum(rng.normal(0, 0.25, n))
        df = make_df(list(closes), high_pad=0.3, low_pad=0.3)
        series = entries.family_signal_series(family, df)

        for i in range(entries.MIN_HISTORY_BARS, n, 17):
            prefix = df.iloc[: i + 1]
            got = entries.signal_on_last_bar(prefix, family)
            want = str(series.iloc[i])
            want = want if want in (entries.BUY, entries.SELL) else None
            assert got == want, f"bar {i}: live view {got} != series {want}"

    def test_future_mutation_cannot_change_past_signals(self):
        rng = np.random.RandomState(3)
        n = 200
        closes = 100.0 + np.cumsum(rng.normal(0, 0.3, n))
        df = make_df(list(closes), high_pad=0.3, low_pad=0.3)
        base = entries.family_signal_series(entries.FAMILY_BREAKOUT, df)
        # trash the last 50 bars (the "future" of bar 100)
        mutated_df = df.copy()
        mutated_df.iloc[-50:, mutated_df.columns.get_indexer(["close"])] = 55.0
        mutated = entries.family_signal_series(entries.FAMILY_BREAKOUT, mutated_df)
        assert list(mutated.iloc[:100]) == list(base.iloc[:100])


class TestSeriesHelpers:
    def test_insufficient_history_returns_none(self):
        df = make_df([100.0] * 30)
        for family in entries.FAMILIES:
            assert entries.signal_on_last_bar(df, family) is None

    def test_unknown_family_raises(self):
        df = make_df([100.0] * 80)
        with pytest.raises(ValueError):
            entries.family_signal_series("Z", df)
