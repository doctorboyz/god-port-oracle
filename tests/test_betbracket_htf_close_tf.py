"""Close-TF detector tests (คุณหมอ 13/11) — M30/M15 causality + rules.

The same iron rule as tests/test_betbracket_htf.py, proven for the new
detectors: an M5 bar may only use state from an M30/M15 bucket that has
FULLY CLOSED by the entry moment. Mutating the forming bucket must not
move any already-anchored label. eps/warmup rules and the shared-labeller
delegation to the original h4_ema/h1_adx behaviour are locked too.
"""
from __future__ import annotations

import pandas as pd
import pytest

from broky.betbracket.htf import (
    ALL_DETECTORS,
    M15_EPS_REL,
    M15_FREQ,
    M15_WARMUP_BARS,
    M30_ADX_WARMUP_BARS,
    M30_EPS_REL,
    M30_FREQ,
    M30_WARMUP_BARS,
    NEW_DETECTORS,
    STATE_DOWN,
    STATE_FLAT,
    STATE_UP,
    align_state_to_m5,
    build_detector_states,
    resample_htf,
    state_h1_adx,
    state_h4_ema,
    state_m15_ema,
    state_m30_adx,
    state_m30_ema,
)


def m5_frame(rows: list[tuple[str, float]]) -> pd.DataFrame:
    """rows = (iso_time, close) — open=high=low=close keeps OHLC trivially."""
    return pd.DataFrame({
        "time": pd.to_datetime([r[0] for r in rows]),
        "open": [r[1] for r in rows],
        "high": [r[1] for r in rows],
        "low": [r[1] for r in rows],
        "close": [r[1] for r in rows],
        "volume": [1.0] * len(rows),
    })


def _htf(values: list[float], freq: str, start: str = "2026-01-05") -> pd.DataFrame:
    idx = pd.date_range(start, periods=len(values), freq=freq)
    return pd.DataFrame({
        "open": values, "high": [v + 1 for v in values],
        "low": [v - 1 for v in values], "close": values,
        "volume": [1.0] * len(values)}, index=idx)


# ── registry ────────────────────────────────────────────────────────────────

def test_new_detector_registry():
    names = [d.name for d in NEW_DETECTORS]
    assert names == ["m30_ema", "m15_ema", "m30_adx"]
    assert [d.freq for d in NEW_DETECTORS[:2]] == [M30_FREQ, M15_FREQ]
    assert [d.name for d in ALL_DETECTORS][:3] == ["h4_ema", "d1_structure",
                                                   "h1_adx"]


def test_build_detector_states_default_is_original_three():
    rows = [(f"2026-01-05 {h:02d}:{m:02d}", 100.0 + h)
            for h in range(24) for m in (0, 30)]
    df = m5_frame(rows)
    st = build_detector_states(df)
    assert list(st) == ["h4_ema", "d1_structure", "h1_adx"]


# ── no-lookahead causality (the iron rule, per new detector) ─────────────────

def _three_m30_buckets(first_close: float, second_close: float,
                       forming_prices: list[float]) -> pd.DataFrame:
    """M5 bars spanning 2 full M30 buckets + a forming third one.

    Each closed bucket drifts one way so the EMA(50) slope has a
    definite sign; warmup=0 makes the labellers usable on tiny series.
    """
    rows: list[tuple[str, float]] = []
    for b, base in ((0, first_close), (1, second_close)):
        for m in (0, 5, 10, 15, 20, 25):
            t = pd.Timestamp("2026-01-05 00:00") + pd.Timedelta(
                minutes=30 * b + m)
            rows.append((t.isoformat(), base + m / 25))
    start = pd.Timestamp("2026-01-05 01:00")
    for k, p in enumerate(forming_prices):
        rows.append(((start + pd.Timedelta(minutes=5 * k)).isoformat(), p))
    return m5_frame(rows)


def _m30_labels(df: pd.DataFrame, state_fn) -> list:
    m30 = resample_htf(df, M30_FREQ)
    states = state_fn(m30, warmup=0) if state_fn is state_m30_ema else \
        state_fn(m30)
    return align_state_to_m5(df, m30, states, M30_FREQ)


@pytest.mark.parametrize("state_fn", [state_m30_ema, state_m30_adx])
def test_mutating_forming_m30_bucket_moves_no_anchored_label(state_fn):
    df = _three_m30_buckets(100.0, 80.0, [50.0, 200.0, 50.0, 200.0])
    before = _m30_labels(df, state_fn)
    df2 = _three_m30_buckets(100.0, 80.0, [500.0, 1.0, 500.0, 1.0])
    after = _m30_labels(df2, state_fn)
    # Entry moments ≤ end of bucket 2 (2026-01-05 01:00) must be identical.
    cutoff = pd.Timestamp("2026-01-05 01:00")
    mask = (df["time"] + pd.Timedelta(minutes=5)) <= cutoff
    assert mask.sum() > 0
    assert [b for b, m in zip(before, mask) if m] == \
           [a for a, m in zip(after, mask) if m]


def test_mutating_forming_m15_bucket_moves_no_anchored_label():
    rows: list[tuple[str, float]] = []
    for b, base in ((0, 100.0), (1, 80.0)):          # 2 full M15 buckets
        for m in (0, 5, 10):
            t = pd.Timestamp("2026-01-05 00:00") + pd.Timedelta(
                minutes=15 * b + m)
            rows.append((t.isoformat(), base + m / 10))
    start = pd.Timestamp("2026-01-05 00:30")
    for k, p in enumerate([50.0, 200.0, 50.0]):
        rows.append(((start + pd.Timedelta(minutes=5 * k)).isoformat(), p))
    df = m5_frame(rows)
    m15 = resample_htf(df, M15_FREQ)
    before = align_state_to_m5(df, m15, state_m15_ema(m15, warmup=0), M15_FREQ)
    rows2 = rows[:-3] + [
        ((start + pd.Timedelta(minutes=5 * k)).isoformat(), p)
        for k, p in enumerate([500.0, 1.0, 500.0])]
    df2 = m5_frame(rows2)
    m15b = resample_htf(df2, M15_FREQ)
    after = align_state_to_m5(df2, m15b, state_m15_ema(m15b, warmup=0), M15_FREQ)
    cutoff = pd.Timestamp("2026-01-05 00:30")          # end of bucket 2
    mask = (df["time"] + pd.Timedelta(minutes=5)) <= cutoff
    assert mask.sum() > 0
    assert [b for b, m in zip(before, mask) if m] == \
           [a for a, m in zip(after, mask) if m]


def test_m30_boundary_bar_closing_on_bucket_end_uses_new_bucket():
    # Bars spanning 3 M30 buckets, each bucket rising. A bar [00:55,
    # 01:00) closes exactly when the 00:30-01:00 bucket closes — that
    # bucket's state IS known at the entry moment; the bar before it
    # (closing 00:55) still sees the previous bucket.
    rows = [(f"2026-01-05 00:{m:02d}", 100.0 + m) for m in range(0, 30, 5)]
    rows += [(f"2026-01-05 00:{m:02d}", 130.0 + m) for m in range(30, 60, 5)]
    rows += [(f"2026-01-05 01:{m:02d}", 160.0 + m) for m in range(0, 30, 5)]
    df = m5_frame(rows)
    m30 = resample_htf(df, M30_FREQ)
    states = state_m30_ema(m30, warmup=0)
    labels = align_state_to_m5(df, m30, states, M30_FREQ)
    i_open55 = df.index[df["time"] == pd.Timestamp("2026-01-05 00:55")][0]
    i_open50 = df.index[df["time"] == pd.Timestamp("2026-01-05 00:50")][0]
    assert labels[i_open50] == states.iloc[0]
    assert labels[i_open55] == states.iloc[1]
    assert states.iloc[1] == STATE_UP


# ── labeller rules ──────────────────────────────────────────────────────────

def test_m30_m15_ema_up_down_flat_by_eps_band():
    rising = _htf([100 + i for i in range(3)], M30_FREQ)
    s_up = state_m30_ema(rising, eps_rel=1e-05, warmup=0)
    assert s_up.iloc[0] is None            # first diff NaN → no state
    assert list(s_up[1:]) == [STATE_UP, STATE_UP]
    falling = _htf([100 - i for i in range(3)], M15_FREQ)
    assert state_m15_ema(falling, eps_rel=1e-05, warmup=1).iloc[2] == STATE_DOWN
    flat = _htf([100.0, 100.0, 100.0], M30_FREQ)
    assert state_m30_ema(flat, eps_rel=1e-05, warmup=1).iloc[2] == STATE_FLAT


def test_m30_m15_ema_warmup_has_no_state():
    s = state_m30_ema(_htf([100.0 + i for i in range(60)], M30_FREQ))
    assert all(v is None for v in s.iloc[:M30_WARMUP_BARS])
    assert s.iloc[M30_WARMUP_BARS] is not None
    s15 = state_m15_ema(_htf([100.0 + i for i in range(60)], M15_FREQ))
    assert all(v is None for v in s15.iloc[:M15_WARMUP_BARS])


def test_m30_adx_trend_vs_ranging_and_warmup():
    trend_up = _htf([100 + 2 * i for i in range(60)], M30_FREQ)
    assert state_m30_adx(trend_up).iloc[-1] == STATE_UP
    trend_dn = _htf([220 - 2 * i for i in range(60)], M30_FREQ)
    assert state_m30_adx(trend_dn).iloc[-1] == STATE_DOWN
    alt = [100 + (2 if i % 2 else -2) for i in range(60)]
    assert state_m30_adx(_htf(alt, M30_FREQ)).iloc[-1] == STATE_FLAT
    s = state_m30_adx(_htf([100.0] * 40, M30_FREQ))
    assert all(v is None for v in s.iloc[:M30_ADX_WARMUP_BARS])


def test_close_tf_labellers_share_recipe_with_originals():
    # Same inputs → same labels as the original functions they delegate
    # to, so the close-TF trio is a pure TF change, not a rule change.
    up = _htf([100 + 5 * i for i in range(60)], M30_FREQ)
    adx_m30 = state_m30_adx(up, warmup=30)
    adx_h1 = state_h1_adx(up, warmup=30)
    assert list(adx_m30) == list(adx_h1)
    eps = M30_EPS_REL
    ema_m30 = state_m30_ema(up, eps_rel=eps, warmup=50)
    ema_h4 = state_h4_ema(up, eps_rel=eps, warmup=50)
    assert list(ema_m30) == list(ema_h4)
    # the default eps constants differ per TF scale (both 0.2σ)
    assert M30_EPS_REL > M15_EPS_REL


# ── determinism on the real sample ───────────────────────────────────────────

def test_new_detector_states_deterministic_on_real_sample():
    df = pd.read_csv("data/paper-feed/ticks/XAUUSD_M5_full.csv",
                     parse_dates=["time"]).sort_values("time").reset_index(drop=True)
    head = df.head(3000)
    a = build_detector_states(head, NEW_DETECTORS)
    b = build_detector_states(head, NEW_DETECTORS)
    assert tuple(a) == tuple(b)
    for k in a:
        assert tuple(x or "" for x in a[k]) == tuple(x or "" for x in b[k])


def test_new_detector_real_sample_invariants():
    df = pd.read_csv("data/paper-feed/ticks/XAUUSD_M5_full.csv",
                     parse_dates=["time"]).sort_values("time").reset_index(drop=True)
    st = build_detector_states(df.head(20000), NEW_DETECTORS)
    for name, series in st.items():
        vals = {x for x in series if x is not None}
        assert vals <= {STATE_UP, STATE_DOWN, STATE_FLAT}
        assert None in series          # warmup exists at the head
