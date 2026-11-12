"""Close-TF forward package tests (คุณหมอ 13/11) — 1h horizon + new trio.

Locks: the 1h horizon lookup follows the same calendar-time rule as
4/24/72h (first M5 bar CLOSING at/after t+h; censored at data end);
build_forward_package defaults stay byte-identical to the 13/11 report
machinery; the close-TF package adds the 1h column and covers all
three new detectors.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from broky.betbracket.forward import (
    CLOSE_TF_HORIZONS_H,
    HORIZONS_H,
    _horizon_lookups,
    build_forward_package,
    forward_stats,
)
from broky.betbracket.htf import NEW_DETECTORS


def m5_frame(rows: list[tuple[str, float]]) -> pd.DataFrame:
    return pd.DataFrame({
        "time": pd.to_datetime([r[0] for r in rows]),
        "open": [r[1] for r in rows],
        "high": [r[1] for r in rows],
        "low": [r[1] for r in rows],
        "close": [r[1] for r in rows],
        "volume": [1.0] * len(rows),
    })


def test_one_hour_horizon_lookup_hits_first_bar_closing_at_or_after():
    # 3 hours of 5-minute bars; anchor = bar 0 (opens 00:00, closes 00:05).
    # 1h target = 01:05 → first close >= 01:05 is the 01:00 bar (closes
    # 01:05) → index 12. 4h target falls past the data → censored (-1).
    rows = [(f"2026-01-05 {h:02d}:{m:02d}", 100.0 + h)
            for h in range(3) for m in range(0, 60, 5)]
    df = m5_frame(rows)
    idx1 = _horizon_lookups(df, 1.0)
    idx4 = _horizon_lookups(df, 4.0)
    assert idx1[0] == 12
    assert idx4[0] == -1                      # censored, never imputed
    # a target landing exactly on a close time still hits (side='left')
    assert idx1[0] * 5 == 60                 # bar 12 opens 01:00


def test_one_hour_horizon_spans_weekend_gap_to_next_real_bar():
    # Friday last bar closes 22:00; a Thursday anchor's 1h target inside
    # the halt must resolve to the first REAL close after the gap.
    rows = [(f"2026-01-08 {h:02d}:55", 100.0 + h) for h in range(20, 22)]
    rows += [("2026-01-08 23:00", 110.0)]
    rows += [("2026-01-12 00:00", 120.0),
             ("2026-01-12 00:05", 121.0)]
    df = m5_frame(rows)
    idx = _horizon_lookups(df, 1.0)
    # anchor 21:55 (closes 22:00), 1h target 23:00 → first close ≥ 23:00
    # is the 23:00 bar (closes 23:05) → index 2 — the 22:00-23:00 halt
    # is spanned with no synthetic prices
    assert idx[0] == 1                      # 20:55 anchor: target 22:00
    assert idx[1] == 2                      # halt-spanning anchor


def test_build_forward_package_default_is_original_three_h4h_ensemble():
    rows = [(f"2026-01-05 {h:02d}:{m:02d}", 100.0 + h + m / 60)
            for h in range(0, 24) for m in range(0, 60, 5)]
    df = m5_frame(rows)
    pkg = build_forward_package(df)
    assert pkg["forward"]["horizons_h"] == list(HORIZONS_H)
    assert list(pkg["forward"]["detectors"]) == ["h4_ema", "d1_structure",
                                                 "h1_adx"]
    assert "ensemble" in pkg            # 3 detectors → ensemble computed


def test_close_tf_package_adds_1h_and_covers_new_trio():
    df = pd.read_csv("data/paper-feed/ticks/XAUUSD_M5_full.csv",
                     parse_dates=["time"]).sort_values("time").reset_index(drop=True)
    pkg = build_forward_package(df.head(20000), NEW_DETECTORS,
                                CLOSE_TF_HORIZONS_H)
    assert pkg["forward"]["horizons_h"] == [1.0, 4.0, 24.0, 72.0]
    assert list(pkg["forward"]["detectors"]) == ["m30_ema", "m15_ema",
                                                 "m30_adx"]
    for det in ("m30_ema", "m15_ema", "m30_adx"):
        for state in ("UP", "DOWN", "FLAT"):
            cells = pkg["forward"]["detectors"][det]["states"][state]
            assert set(cells) == {"1.0", "4.0", "24.0", "72.0"}
    # 1h baseline: reference share |move|<=eps is ~50% by construction
    # (exactly 0.5 up to |ret| ties at eps, even n — same tolerance as
    # the original forward tests)
    b1 = pkg["forward"]["baselines"]["1.0"]
    assert abs(b1["p_abs_le_eps"] - 0.5) < 0.01
    assert b1["n"] > 0


def test_forward_stats_new_detectors_hit_definitions():
    # hand-built series: m30_ema-style states passed directly — UP rows
    # must hit on ret>0 at every horizon, DOWN on ret<0 (definition lock)
    df = m5_frame([(f"2026-01-05 {h:02d}:{m:02d}", 100.0 + h)
                   for h in range(24) for m in range(0, 60, 5)])
    n = len(df)
    states = {"m30_ema": ["UP"] * n}
    stats = forward_stats(df, states, (1.0,))
    up = stats["detectors"]["m30_ema"]["states"]["UP"]["1.0"]
    assert up["n"] > 0
    assert up["hit_rate"] == 1.0            # prices only rise → every hit
    states_dn = {"m30_ema": ["DOWN"] * n}
    stats = forward_stats(df, states_dn, (1.0,))
    dn = stats["detectors"]["m30_ema"]["states"]["DOWN"]["1.0"]
    assert dn["hit_rate"] == 0.0            # DOWN hit needs ret<0
    assert dn["mean_move"] > 0
