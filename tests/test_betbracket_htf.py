"""HTF detector tests — bucketing, no-lookahead causality, determinism.

The iron rule (คุณหมอ 12/11): an M5 bar may only use trend state from an
HTF bar that has fully CLOSED by the bet's entry moment. The causal
tests below prove it by mutating the FORMING H4 bucket and asserting
that no already-anchored label moves.
"""
from __future__ import annotations

import pandas as pd
import pytest

from broky.betbracket.htf import (
    H1_FREQ,
    H4_FREQ,
    STATE_DOWN,
    STATE_FLAT,
    STATE_UP,
    align_state_to_m5,
    build_detector_states,
    resample_htf,
    state_d1_structure,
    state_h1_adx,
    state_h4_ema,
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


# ── resample ────────────────────────────────────────────────────────────────

def test_resample_h4_aggregates_and_drops_empty_buckets():
    rows = [
        ("2026-01-05 00:00", 10.0), ("2026-01-05 00:05", 11.0),
        ("2026-01-05 00:10", 12.0),
        ("2026-01-05 04:00", 20.0), ("2026-01-05 04:05", 19.0),
    ]
    h4 = resample_htf(m5_frame(rows), H4_FREQ)
    assert list(h4.index) == [pd.Timestamp("2026-01-05 00:00"),
                              pd.Timestamp("2026-01-05 04:00")]
    assert h4.iloc[0]["high"] == 12.0 and h4.iloc[0]["low"] == 10.0
    assert h4.iloc[0]["open"] == 10.0 and h4.iloc[0]["close"] == 12.0
    assert h4.iloc[1]["close"] == 19.0


def test_resample_rejects_unsorted():
    rows = [("2026-01-05 00:05", 1.0), ("2026-01-05 00:00", 2.0)]
    with pytest.raises(ValueError):
        resample_htf(m5_frame(rows), H4_FREQ)


# ── no-lookahead causality (the iron rule) ───────────────────────────────────

def _synthetic_three_buckets(first_close: float, second_close: float,
                             forming_prices: list[float]) -> pd.DataFrame:
    """M5 bars spanning 2 full H4 buckets + a forming third one.

    Each bucket gets one bar per hour with a drifting close so EMA(50)
    slope has a definite sign per bucket; warmup=0 makes h4_ema usable
    on this tiny synthetic series.
    """
    rows: list[tuple[str, float]] = []
    for b, base in ((0, first_close), (1, second_close)):
        for h in range(4):
            for m in (0, 30):
                t = pd.Timestamp("2026-01-05 00:00") + pd.Timedelta(
                    days=b, hours=h, minutes=m)
                rows.append((t.isoformat(), base + h + m / 30))
    start = pd.Timestamp("2026-01-07 00:00")
    for k, p in enumerate(forming_prices):
        rows.append(((start + pd.Timedelta(minutes=5 * k)).isoformat(), p))
    return m5_frame(rows)


def _labels(df: pd.DataFrame) -> list:
    h4 = resample_htf(df, H4_FREQ)
    states = state_h4_ema(h4, warmup=0)
    return align_state_to_m5(df, h4, states, H4_FREQ)


def test_forming_h4_bar_uses_previous_bucket_state():
    # Bucket 1 drifts up, bucket 2 drifts down, bucket 3 (forming, wild)
    # bars must still be labeled with bucket 2's state.
    df = _synthetic_three_buckets(100.0, 90.0, [50.0, 200.0, 50.0, 200.0])
    labels = _labels(df)
    forming = df[df["time"] >= "2026-01-07 00:00"]
    inside = [labels[df.index.get_loc(i)] for i in forming.index]
    assert all(s is not None for s in inside)
    assert set(inside) == {STATE_DOWN}          # bucket 2 fell → DOWN


def test_mutating_forming_bucket_does_not_move_any_anchored_label():
    df = _synthetic_three_buckets(100.0, 90.0, [50.0, 200.0, 50.0, 200.0])
    before = _labels(df)

    df2 = _synthetic_three_buckets(100.0, 90.0, [500.0, 1.0, 500.0, 1.0])
    after = _labels(df2)

    # Every label whose entry moment is ≤ end of bucket 2 (2026-01-06
    # 04:00) must be identical — the mutation lives strictly inside the
    # forming bucket 3 and cannot leak backwards.
    cutoff = pd.Timestamp("2026-01-06 04:00")
    mask = (df["time"] + pd.Timedelta(minutes=5)) <= cutoff
    assert mask.sum() > 0
    assert [b for b, m in zip(before, mask) if m] == \
           [a for a, m in zip(after, mask) if m]


def test_boundary_bar_closes_exactly_on_bucket_end_uses_new_bucket():
    # Bar [03:55, 04:00) closes exactly when the 00:00-04:00 H4 bucket
    # closes — that bucket's state IS known at the entry moment.
    rows = []
    # Jan 4 evening buckets (16-20, 20-24) rising → the 20-24 bucket is UP
    for h in range(16, 24):
        rows.append((f"2026-01-04 {h:02d}:{'00' if h % 2 == 0 else '30'}",
                     110.0 + h))
    # Jan 5 00:00-03:30 collapses → the 00-04 bucket is DOWN
    for h in range(4):
        for m in (0, 30):
            rows.append((f"2026-01-05 {h:02d}:{m:02d}", 100.0 + h + m / 30))
    rows.append(("2026-01-05 03:50", 103.5))
    rows.append(("2026-01-05 03:55", 103.0))
    rows.sort(key=lambda r: r[0])
    df = m5_frame(rows)
    h4 = resample_htf(df, H4_FREQ)
    states = state_h4_ema(h4, warmup=0)
    labels = align_state_to_m5(df, h4, states, H4_FREQ)
    # 03:50 (closes 03:55) → last closed bucket = Jan 4 20-24 = UP;
    # 03:55 (closes exactly 04:00) → Jan 5 00-04 bucket = DOWN.
    assert states.iloc[1] == STATE_UP and states.iloc[2] == STATE_DOWN
    assert labels[df.index[df["time"] == pd.Timestamp("2026-01-05 03:50")][0]] \
        == STATE_UP
    assert labels[df.index[df["time"] == pd.Timestamp("2026-01-05 03:55")][0]] \
        == STATE_DOWN


def test_weekend_gap_falls_back_to_last_closed_bucket():
    # Thursday + Friday buckets each close fully; the Sunday reopen bar
    # (post-DST 23:00) must carry Friday's bucket state, never the
    # forming Sunday bucket.
    rows = []
    for day, base, hours in ((8, 100.0, range(16, 20)),
                             (9, 110.0, range(20, 24))):
        for h in hours:
            rows.append((f"2026-01-{day:02d} {h:02d}:{'00' if h % 2 == 0 else '30'}",
                         base + h))
    rows.append(("2026-01-11 23:00", 130.0))
    df = m5_frame(rows)
    h4 = resample_htf(df, H4_FREQ)
    states = state_h4_ema(h4, warmup=0)
    labels = align_state_to_m5(df, h4, states, H4_FREQ)
    sun = labels[df.index[df["time"] == pd.Timestamp("2026-01-11 23:00")][0]]
    assert sun == states.iloc[1]          # Friday bucket, closed Sat 00:00
    assert sun is not None


# ── determinism ─────────────────────────────────────────────────────────────

def test_build_detector_states_deterministic_on_real_sample():
    df = pd.read_csv("data/paper-feed/ticks/XAUUSD_M5_full.csv",
                     parse_dates=["time"]).sort_values("time").reset_index(drop=True)
    head = df.head(3000)
    assert _tuple_states(head) == _tuple_states(head)


def _tuple_states(df: pd.DataFrame) -> tuple:
    st = build_detector_states(df)
    return tuple(tuple(s or "" for s in v) for v in st.values())


# ── detector definitions ────────────────────────────────────────────────────

def _htf(values: list[float], freq: str, start: str = "2026-01-05") -> pd.DataFrame:
    idx = pd.date_range(start, periods=len(values), freq=freq)
    return pd.DataFrame({
        "open": values, "high": [v + 1 for v in values],
        "low": [v - 1 for v in values], "close": values,
        "volume": [1.0] * len(values)}, index=idx)


def test_h4_ema_up_down_flat_by_eps_band():
    rising = _htf([100 + i for i in range(3)], H4_FREQ)
    s_up = state_h4_ema(rising, eps_rel=0.0001, warmup=0)
    assert s_up.iloc[0] is None           # first diff is NaN → no state
    assert list(s_up[1:]) == [STATE_UP, STATE_UP]
    falling = _htf([100 - i for i in range(3)], H4_FREQ)
    assert state_h4_ema(falling, eps_rel=0.0001, warmup=1).iloc[2] == STATE_DOWN
    flat = _htf([100.0, 100.0, 100.0], H4_FREQ)
    assert state_h4_ema(flat, eps_rel=0.0001, warmup=1).iloc[2] == STATE_FLAT


def test_h4_ema_warmup_has_no_state():
    s = state_h4_ema(_htf([100.0 + i for i in range(60)], H4_FREQ), warmup=50)
    assert all(v is None for v in s.iloc[:50])
    assert s.iloc[50] is not None


def test_d1_structure_hh_hl_up_ll_lh_down():
    idx = pd.date_range("2026-01-05", periods=5, freq="1D")
    up = pd.DataFrame({
        "open": [1, 1, 1, 1, 1], "high": [10, 9, 12, 14, 15],
        "low": [4, 5, 6, 8, 9], "close": [1, 1, 1, 1, 1],
        "volume": [1.0] * 5}, index=idx)
    # recent 2 (14,15 / 8,9) vs prior 3 (10,9,12 / 4,5,6): HH + HL → UP
    assert state_d1_structure(up).iloc[-1] == STATE_UP
    down = pd.DataFrame({
        "open": [1, 1, 1, 1, 1], "high": [15, 14, 12, 9, 10],
        "low": [9, 8, 6, 5, 4], "close": [1, 1, 1, 1, 1],
        "volume": [1.0] * 5}, index=idx)
    assert state_d1_structure(down).iloc[-1] == STATE_DOWN
    mixed = pd.DataFrame({
        "open": [1, 1, 1, 1, 1], "high": [10, 12, 9, 11, 8],
        "low": [5, 6, 4, 7, 5], "close": [1, 1, 1, 1, 1],
        "volume": [1.0] * 5}, index=idx)
    # recent high 8 < prior max 11 (LH) but recent low 5 > prior low 4
    # (HL) — neither pattern → FLAT
    assert state_d1_structure(mixed).iloc[-1] == STATE_FLAT


def test_h1_adx_trend_vs_ranging():
    # Strong monotonic grind: ADX rises well above 25, EMA20 up → UP.
    trend_up = _htf([100 + 2 * i for i in range(60)], H1_FREQ)
    assert state_h1_adx(trend_up, warmup=30).iloc[-1] == STATE_UP
    trend_dn = _htf([220 - 2 * i for i in range(60)], H1_FREQ)
    assert state_h1_adx(trend_dn, warmup=30).iloc[-1] == STATE_DOWN
    # Perfect alternation: directional movement cancels, ADX low → FLAT.
    alt = [100 + (2 if i % 2 else -2) for i in range(60)]
    assert state_h1_adx(_htf(alt, H1_FREQ), warmup=30).iloc[-1] == STATE_FLAT


def test_h1_adx_warmup_has_no_state():
    s = state_h1_adx(_htf([100.0] * 40, H1_FREQ), warmup=30)
    assert all(v is None for v in s.iloc[:30])
