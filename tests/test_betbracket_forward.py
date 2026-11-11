"""Forward-accuracy tests — calendar-time lookup, hit definitions, ensemble.

Causal posture: the future side is future data BY DESIGN (that is what
is being measured); the tests lock the lookup semantics instead —
weekend gaps resolve to the next REAL bar (never a synthetic price),
data-end anchors are censored (excluded + counted), and the FLAT
reference is the all-anchors median so its baseline is exactly 50%.
The unit-consistency tests guard the pandas-3 µs/ns trap that once
censored every lag reading (regression, 12/11).
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from broky.betbracket.forward import (
    ENS_DOWN3,
    ENS_NONE,
    ENS_UP2,
    ENS_UP3,
    _horizon_lookups,
    ensemble_stats,
    forward_stats,
    persistence_stats,
    transition_matrix,
)
from broky.betbracket.htf import STATE_DOWN, STATE_UP

_ROOT = Path(__file__).resolve().parents[1]
BARS_CSV = _ROOT / "data" / "paper-feed" / "ticks" / "XAUUSD_M5_full.csv"


def m5_frame(times: list[str], closes: list[float]) -> pd.DataFrame:
    return pd.DataFrame({
        "time": pd.to_datetime(times),
        "open": closes,
        "high": closes,
        "low": closes,
        "close": closes,
        "volume": [1.0] * len(closes),
    })


def _minutes(start: str, n: int, step_min: int = 5) -> list[str]:
    t0 = pd.Timestamp(start)
    return [(t0 + pd.Timedelta(minutes=step_min * i)).isoformat()
            for i in range(n)]


# ── calendar-time lookup across gaps / at the data end ───────────────────────

def test_horizon_lookup_skips_weekend_to_next_real_bar():
    """A Saturday target must resolve to Monday's first bar — no synthetic
    weekend prices, no 'nearest bar' shortcuts."""
    times = (_minutes("2026-01-09 20:00", 3)          # Fri 20:00/05/10
             + _minutes("2026-01-12 00:00", 4))        # Mon 00:00..15
    df = m5_frame(times, [10.0, 10.0, 10.0,
                          12.0, 12.0, 12.0, 12.0])
    idx = _horizon_lookups(df, 4.0)
    # Sat 00:05/00:10/00:15 targets → first close at/after = Mon 00:00 bar
    assert idx[:3].tolist() == [3, 3, 3]
    # Monday anchors have no bar 4h later → censored, never imputed
    assert idx[3:].tolist() == [-1, -1, -1, -1]


def test_forward_excludes_and_counts_censored_anchors():
    times = _minutes("2026-01-09 20:00", 3) + _minutes("2026-01-12 00:00", 4)
    df = m5_frame(times, [10.0, 10.5, 11.0,
                          12.0, 12.5, 13.0, 13.5])
    states = {"h4_ema": [STATE_UP] * len(df)}
    out = forward_stats(df, states, horizons=(4.0,))
    cell = out["detectors"]["h4_ema"]["states"][STATE_UP]["4.0"]
    base = out["baselines"]["4.0"]
    assert cell["n"] == 3                      # only the Friday anchors
    assert base["n_censored"] == 4
    assert cell["n"] + base["n_censored"] == len(df)


def test_forward_returns_use_the_gapped_bar_not_a_synthetic_price():
    times = _minutes("2026-01-09 20:00", 3) + _minutes("2026-01-12 00:00", 1)
    df = m5_frame(times, [10.0, 10.0, 10.0, 14.0])
    states = {"h4_ema": [STATE_UP] * len(df)}
    out = forward_stats(df, states, horizons=(4.0,))
    cell = out["detectors"]["h4_ema"]["states"][STATE_UP]["4.0"]
    # every Friday anchor reads Monday's close (14.0) minus its own close
    assert cell["mean_move"] == pytest.approx(4.0)
    assert cell["hit_rate"] == pytest.approx(1.0)


# ── hit definitions + the 50% FLAT reference ────────────────────────────────

def _flat_step_frame() -> pd.DataFrame:
    closes = [0.0, 0.0, 0.0, 0.0, 10.0, 10.0, 10.0, 10.0,
              13.0, 13.0, 13.0, 13.0]
    return m5_frame(_minutes("2026-01-05 00:00", len(closes)), closes)


def test_forward_up_down_hit_and_mean_move():
    df = _flat_step_frame()
    up = forward_stats(df, {"d": [STATE_UP] * len(df)},
                       horizons=(20 / 60,))
    dn = forward_stats(df, {"d": [STATE_DOWN] * len(df)},
                       horizons=(20 / 60,))
    cu = up["detectors"]["d"]["states"][STATE_UP]["0.3333333333333333"]
    cd = dn["detectors"]["d"]["states"][STATE_DOWN]["0.3333333333333333"]
    rets = [10.0, 10.0, 10.0, 10.0, 3.0, 3.0, 3.0, 3.0]   # c[i+4] − c[i]
    assert cu["n"] == 8
    assert cu["hit_rate"] == pytest.approx(1.0)
    assert cu["mean_move"] == pytest.approx(float(np.mean(rets)))
    assert cd["hit_rate"] == pytest.approx(0.0)


def test_flat_reference_is_exactly_half_by_construction():
    """eps = median |ret| over all anchors → the all-anchor share is 50%,
    so every FLAT hit-rate is read against a 50% line, not a moving goal."""
    df = _flat_step_frame()
    out = forward_stats(df, {"d": [STATE_UP] * len(df)},
                        horizons=(20 / 60,))
    base = out["baselines"]["0.3333333333333333"]
    assert base["eps_median_abs_move"] == pytest.approx(6.5)
    assert base["p_abs_le_eps"] == pytest.approx(0.5)
    assert base["p_up"] + base["p_down"] == pytest.approx(1.0)


# ── persistence / transitions / ensemble ────────────────────────────────────

def test_persistence_survival_and_run_length():
    df = _flat_step_frame()
    s = [STATE_UP] * 4 + [STATE_DOWN] * 4 + [STATE_UP] * 4
    out = persistence_stats(df, {"d": s}, horizons=(20 / 60,))
    up = out["d"][STATE_UP]
    assert up["survival"]["0.3333333333333333"]["n"] == 4
    assert up["survival"]["0.3333333333333333"]["same_rate"] == \
        pytest.approx(0.0)          # 4 bars ahead every UP has flipped DOWN
    assert up["runs"] == 2
    assert up["run_median_hours"] == pytest.approx(4 * 5 / 60)
    dn = out["d"][STATE_DOWN]
    # 4 bars ahead every DOWN anchor has flipped back UP (DOWN run = 4)
    assert dn["survival"]["0.3333333333333333"]["same_rate"] == \
        pytest.approx(0.0)
    assert dn["survival"]["0.3333333333333333"]["n"] == 4


def test_transition_matrix_rows_normalise_and_skip_warmup():
    series = pd.Series([STATE_UP, STATE_UP, STATE_DOWN, None, STATE_DOWN])
    mat = transition_matrix(series)
    assert set(mat) == {STATE_UP}          # DOWN never has a known next bar
    assert mat[STATE_UP][STATE_UP] == pytest.approx(0.5)
    assert mat[STATE_UP][STATE_DOWN] == pytest.approx(0.5)
    assert mat[STATE_UP]["n"] == 2


def test_ensemble_groups_agree_votes():
    df = _flat_step_frame()
    n = len(df)
    states = {
        "h4_ema": [STATE_UP] * 4 + ["FLAT"] * 4 + [STATE_UP] * 4,
        "d1_structure": [STATE_UP] * n,
        "h1_adx": [STATE_UP] * 4 + [STATE_DOWN] * 4 + [STATE_UP] * 4,
    }
    out = ensemble_stats(df, states, horizons=(20 / 60,))
    g = out["groups"]["0.3333333333333333"]
    # bars 8-11 agree too, but their 4-bar horizon falls past the data
    # end → censored; only the first UP×3 run counts
    assert g[ENS_UP3]["n"] == 4
    assert g[ENS_UP3]["hit_rate"] == pytest.approx(1.0)
    assert g[ENS_UP2]["n"] == 0
    assert g[ENS_NONE]["n"] == 4             # bars 4-7: 1 UP vote vs 1 DOWN
    assert g[ENS_DOWN3]["n"] == 0


def test_ensemble_two_of_three_is_mid_confidence():
    df = _flat_step_frame()
    n = len(df)
    states = {
        "a": [STATE_UP] * n,
        "b": [STATE_UP] * 4 + ["FLAT"] * 8,
        "c": [STATE_DOWN] * n,
    }
    out = ensemble_stats(df, states, horizons=(20 / 60,))
    g = out["groups"]["0.3333333333333333"]
    assert g[ENS_UP2]["n"] == 4             # bars 0-3: two UP + one DOWN
    assert g[ENS_UP2]["hit_rate"] == pytest.approx(1.0)
    # 1-1-1 votes → no signal; bars 8-11 censored past the data end
    assert g[ENS_NONE]["n"] == 4
    assert g[ENS_NONE]["hit_rate"] is None or \
        g[ENS_NONE]["hit_rate"] == 0.0


def test_ensemble_excludes_any_detector_warmup():
    df = _flat_step_frame()
    n = len(df)
    states = {"a": [None] * n, "b": [STATE_UP] * n, "c": [STATE_UP] * n}
    out = ensemble_stats(df, states, horizons=(20 / 60,))
    g = out["groups"]["0.3333333333333333"]
    assert sum(d["n"] for d in g.values()) == 0   # warmup bars never labelled


# ── real-sample invariants (slow) ────────────────────────────────────────────

@pytest.mark.slow
def test_real_sample_forward_invariants():
    df = pd.read_csv(BARS_CSV, parse_dates=["time"])
    df = df.sort_values("time").reset_index(drop=True)
    from broky.betbracket.htf import build_detector_states
    states = build_detector_states(df)
    out = forward_stats(df, states)
    for h in ("4.0", "24.0", "72.0"):
        base = out["baselines"][h]
        assert base["n"] > 60_000, h
        # median property — exactly 0.5 up to |ret| ties at eps (even n,
        # repeated |ret| values can push the ≤ share a hair above half)
        assert base["p_abs_le_eps"] == pytest.approx(0.5, abs=0.01)
        for det, d in out["detectors"].items():
            for state, cells in d["states"].items():
                c = cells[h]
                assert c["n"] + base["n_censored"] <= base["n"] + \
                    base["n_censored"] + d["n_anchor_bars"]
                if c["n"]:
                    assert 0.0 <= c["hit_rate"] <= 1.0
    pers = persistence_stats(df, states)
    for det, per_state in pers.items():
        for state, s in per_state.items():
            for h, surv in s["survival"].items():
                if surv["n"]:
                    assert 0.0 <= surv["same_rate"] <= 1.0
