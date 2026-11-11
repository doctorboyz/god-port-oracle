"""Detector-quality regression tests — the pandas-3 unit trap.

Causal test (RED→GREEN 12/11): detector_quality's lag scan compared
true-epoch NANOseconds against resampled close times that pandas 3
keeps as MICROseconds — every lag read as 'censored'. The tests below
lock the correct behaviour on the real one-year sample: transitions
exist, none are censored, and lag is measured in detector bars.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from scripts.bet_bracket_conditional import detector_quality, truth_d1
from broky.betbracket.htf import resample_htf

_ROOT = Path(__file__).resolve().parents[1]
BARS_CSV = _ROOT / "data" / "paper-feed" / "ticks" / "XAUUSD_M5_full.csv"

pytestmark = pytest.mark.slow


@pytest.fixture(scope="module")
def bars() -> object:
    import pandas as pd

    df = pd.read_csv(BARS_CSV, parse_dates=["time"])
    return df.sort_values("time").reset_index(drop=True)


def test_truth_has_transitions_in_sample_year(bars):
    d1 = resample_htf(bars, "1D")
    truth = truth_d1(d1).to_numpy(dtype=object)
    transitions = sum(1 for i in range(1, len(truth))
                      if truth[i] is not None and truth[i - 1] is not None
                      and truth[i] != truth[i - 1])
    assert transitions >= 4          # the year contains real regime turns
    assert truth[0] is None          # SMA(63) warmup carries no truth


def test_lag_not_censored_unit_consistency(bars):
    """The pandas-3 unit bug made every transition 'censored' — lock the fix."""
    quality = detector_quality(bars)
    for det, d in quality.items():
        assert d["n_truth_transitions"] >= 4, det
        assert d["n_lag_censored"] == 0, (det, d)
        assert d["lag_bars_median"] is not None, det
        assert d["lag_days_median"] is not None and d["lag_days_median"] >= 0, det


def test_detector_states_deterministic(bars):
    q1 = detector_quality(bars)
    q2 = detector_quality(bars)
    assert q1 == q2
