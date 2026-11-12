"""HTF resampling + trend-state detectors (conditional bracket, T1).

Deterministic, strictly no-lookahead: an M5 bar may only consume trend
state from an HTF bar that has FULLY CLOSED at or before the bet's entry
moment (= the M5 bar's close time). The H4/H1/D1 bar currently forming
is invisible by construction — `align_state_to_m5` searches only closed
buckets, and the causal tests in tests/test_betbracket_htf.py prove it
by mutating the forming bucket and asserting labels do not move.

Buckets are fixed UTC calendar buckets (label=closed=left). Empty
buckets (weekends / daily halts) simply do not exist — the aligner then
falls back to the most recent CLOSED bucket, which is the correct
"state known at this moment" answer. This mirrors the Exness calendar
findings from the fetcher work (halt 22:00-23:00 UTC post-DST): no
synthetic bars are fabricated for gaps.

Detectors (all parameters fixed here, reported in every output):

h4_ema       EMA(50) slope on closed H4 bars, relative threshold
             eps_rel = 0.0002 (2 bp of price per H4 bar). Chosen so the
             FLAT band covers ~20% of bars in the 2025-10→2026-11 sample
             (empirical slope σ ≈ 0.09%/bar → eps ≈ 0.2σ) — a band wide
             enough to skip consolidation, narrow enough to still tag
             slow grinds. Warmup: first 50 H4 bars have no state.
d1_structure Last 5 closed D1 bars — recent 2 vs prior 3:
             HH+HL = UP, LL+LH = DOWN, otherwise FLAT.
h1_adx       ADX(14) on closed H1 bars: ≥25 = trending, direction =
             EMA(20) slope sign; <25 (or flat slope) = FLAT.
             Warmup: first 30 H1 bars have no state.

Close-TF detectors (คุณหมอ 13/11 — "TF ห่างกันมากไปจนไม่สามารถเกิด
ความสัมพันธ์สำหรับการ bet"): the same recipe on M30/M15 so the
regime TF sits next to the M5 signal TF. No-lookahead identical —
only fully closed M30/M15 buckets are visible to an M5 bar.

m30_ema      EMA(50) slope on closed M30 bars. eps_rel calibrated to
             the M30 slope scale: empirical σ(slope/close) on the
             2025-10→2026-11 sample = 3.67bp/bar → eps = 0.2σ =
             7.34e-05, the SAME 0.2σ design rule as h4_ema (keeps
             the FLAT band at ~23% of bars, vs 52% at 0.5σ — the
             0.5σ candidate was tested and rejected: forward hit at
             1h/4h within 1pp of the 0.2σ variant, but it starves
             the UP/DOWN cells of n). Warmup: 50 M30 bars.
m15_ema      EMA(50) slope on closed M15 bars, same rule. σ slope =
             2.56bp/bar → eps = 0.2σ = 5.12e-05 (FLAT ~23%).
             Warmup: 50 M15 bars.
m30_adx      Clone of h1_adx on M30: ADX(14) ≥ 25 + EMA(20) slope
             sign; <25 = FLAT. Warmup: 30 M30 bars.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import numpy as np
import pandas as pd

from broky.indicators.adx import calculate_adx

STATE_UP = "UP"
STATE_DOWN = "DOWN"
STATE_FLAT = "FLAT"

# ── bucket definitions (UTC calendar buckets, label=closed=left) ────────────
H1_FREQ = "1h"
H4_FREQ = "4h"
D1_FREQ = "1D"
M30_FREQ = "30min"
M15_FREQ = "15min"

_AGG = {
    "open": ("open", "first"),
    "high": ("high", "max"),
    "low": ("low", "min"),
    "close": ("close", "last"),
    "volume": ("volume", "sum"),
}

# ── detector parameters ─────────────────────────────────────────────────────
H4_EMA_PERIOD = 50
H4_EPS_REL = 0.0002        # 2 bp of price per H4 bar (≈0.2σ of slope)
H4_WARMUP_BARS = 50

D1_STRUCT_WINDOW = 5       # recent 2 bars vs prior 3 bars
D1_STRUCT_RECENT = 2

H1_ADX_PERIOD = 14
H1_ADX_TREND_LEVEL = 25.0
H1_EMA_PERIOD = 20
H1_WARMUP_BARS = 30

# close-TF detectors (คุณหมอ 13/11) — eps = 0.2σ of the per-bar EMA(50)
# relative slope measured on the full M5 sample resampled to each TF
# (σ_M30 = 3.67bp, σ_M15 = 2.56bp per bar) — same 0.2σ rule as h4_ema.
M30_EMA_PERIOD = 50
M30_EPS_REL = 7.34e-05
M30_WARMUP_BARS = 50

M15_EMA_PERIOD = 50
M15_EPS_REL = 5.12e-05
M15_WARMUP_BARS = 50

M30_ADX_PERIOD = 14
M30_ADX_TREND_LEVEL = 25.0
M30_ADX_EMA_PERIOD = 20
M30_ADX_WARMUP_BARS = 30


def resample_htf(m5: pd.DataFrame, freq: str) -> pd.DataFrame:
    """Aggregate M5 OHLCV into fixed UTC `freq` buckets.

    `m5` must be time-ascending with columns time/open/high/low/close/
    volume. Returns a frame indexed by bucket OPEN time (UTC); buckets
    with zero M5 bars (weekend/halt) are dropped — they never existed.
    """
    if freq not in (H1_FREQ, H4_FREQ, D1_FREQ, M30_FREQ, M15_FREQ):
        raise ValueError(f"unsupported timeframe {freq!r}")
    if not m5["time"].is_monotonic_increasing:
        raise ValueError("m5 bars must be time-ascending")
    htf = (m5.set_index("time")
             .resample(freq, label="left", closed="left")
             .agg(**_AGG)
             .dropna(subset=["open"]))
    return htf


def state_h4_ema(h4: pd.DataFrame, eps_rel: float = H4_EPS_REL,
                 warmup: int = H4_WARMUP_BARS) -> pd.Series:
    """UP/DOWN/FLAT per closed H4 bar from EMA(50) slope.

    slope_t = EMA50[t] − EMA50[t−1], normalised by close[t]:
    ≥ +eps_rel → UP, ≤ −eps_rel → DOWN, else FLAT.
    First `warmup` bars (EMA seed bias) carry no state (None).
    """
    ema = h4["close"].ewm(span=H4_EMA_PERIOD, adjust=False).mean()
    rel = (ema.diff() / h4["close"]).to_numpy(float)
    out = pd.Series([None] * len(h4), index=h4.index, dtype=object)
    for i in range(warmup, len(h4)):
        s = rel[i]
        if np.isnan(s):
            continue
        if s >= eps_rel:
            out.iloc[i] = STATE_UP
        elif s <= -eps_rel:
            out.iloc[i] = STATE_DOWN
        else:
            out.iloc[i] = STATE_FLAT
    return out


def state_d1_structure(d1: pd.DataFrame,
                       window: int = D1_STRUCT_WINDOW) -> pd.Series:
    """UP/DOWN/FLAT per closed D1 bar from 5-bar market structure.

    Recent 2 bars vs prior 3: HH+HL → UP, LL+LH → DOWN, else FLAT.
    Fewer than `window` bars → no state.
    """
    out = pd.Series([None] * len(d1), index=d1.index, dtype=object)
    if len(d1) < window:
        return out
    high = d1["high"].to_numpy(float)
    low = d1["low"].to_numpy(float)
    for i in range(len(d1)):
        if i < window - 1:
            continue
        hi_r = high[i - D1_STRUCT_RECENT + 1:i + 1]
        hi_p = high[i - window + 1:i - D1_STRUCT_RECENT + 1]
        lo_r = low[i - D1_STRUCT_RECENT + 1:i + 1]
        lo_p = low[i - window + 1:i - D1_STRUCT_RECENT + 1]
        hh, hl = hi_r.max() > hi_p.max(), lo_r.min() > lo_p.min()
        ll, lh = hi_r.max() < hi_p.max(), lo_r.min() < lo_p.min()
        if hh and hl:
            out.iloc[i] = STATE_UP
        elif ll and lh:
            out.iloc[i] = STATE_DOWN
        else:
            out.iloc[i] = STATE_FLAT
    return out


def state_h1_adx(h1: pd.DataFrame, warmup: int = H1_WARMUP_BARS) -> pd.Series:
    """UP/DOWN/FLAT per closed H1 bar from ADX(14) + EMA(20) slope.

    ADX ≥ 25 = trending: direction from EMA(20) slope (positive → UP,
    negative → DOWN, exactly zero → FLAT). ADX < 25 → FLAT.
    First `warmup` bars carry no state.
    """
    adx, _, _ = calculate_adx(h1["high"], h1["low"], h1["close"],
                              period=H1_ADX_PERIOD)
    ema = h1["close"].ewm(span=H1_EMA_PERIOD, adjust=False).mean()
    slope = ema.diff()
    out = pd.Series([None] * len(h1), index=h1.index, dtype=object)
    adx_v = adx.to_numpy(float)
    sl_v = slope.to_numpy(float)
    for i in range(warmup, len(h1)):
        a, s = adx_v[i], sl_v[i]
        if np.isnan(a) or np.isnan(s):
            continue
        if a < H1_ADX_TREND_LEVEL:
            out.iloc[i] = STATE_FLAT
        elif s > 0:
            out.iloc[i] = STATE_UP
        elif s < 0:
            out.iloc[i] = STATE_DOWN
        else:
            out.iloc[i] = STATE_FLAT
    return out


def _state_ema_slope(htf: pd.DataFrame, period: int, eps_rel: float,
                     warmup: int) -> pd.Series:
    """Shared UP/DOWN/FLAT labeller for EMA-slope detectors.

    slope_t = EMA(period)[t] − EMA(period)[t−1], normalised by
    close[t]: ≥ +eps_rel → UP, ≤ −eps_rel → DOWN, else FLAT. First
    `warmup` bars (EMA seed bias) carry no state.
    """
    ema = htf["close"].ewm(span=period, adjust=False).mean()
    rel = (ema.diff() / htf["close"]).to_numpy(float)
    out = pd.Series([None] * len(htf), index=htf.index, dtype=object)
    for i in range(warmup, len(htf)):
        s = rel[i]
        if np.isnan(s):
            continue
        if s >= eps_rel:
            out.iloc[i] = STATE_UP
        elif s <= -eps_rel:
            out.iloc[i] = STATE_DOWN
        else:
            out.iloc[i] = STATE_FLAT
    return out


def _state_adx_ema(htf: pd.DataFrame, adx_period: int,
                   trend_level: float, ema_period: int,
                   warmup: int) -> pd.Series:
    """Shared UP/DOWN/FLAT labeller for ADX + EMA-slope detectors.

    ADX ≥ trend_level = trending: direction from EMA(ema_period)
    slope sign (positive → UP, negative → DOWN, exactly zero →
    FLAT). Below the level → FLAT. First `warmup` bars carry no
    state.
    """
    adx, _, _ = calculate_adx(htf["high"], htf["low"], htf["close"],
                              period=adx_period)
    ema = htf["close"].ewm(span=ema_period, adjust=False).mean()
    slope = ema.diff()
    out = pd.Series([None] * len(htf), index=htf.index, dtype=object)
    adx_v = adx.to_numpy(float)
    sl_v = slope.to_numpy(float)
    for i in range(warmup, len(htf)):
        a, s = adx_v[i], sl_v[i]
        if np.isnan(a) or np.isnan(s):
            continue
        if a < trend_level:
            out.iloc[i] = STATE_FLAT
        elif s > 0:
            out.iloc[i] = STATE_UP
        elif s < 0:
            out.iloc[i] = STATE_DOWN
        else:
            out.iloc[i] = STATE_FLAT
    return out


def state_m30_ema(m30: pd.DataFrame, eps_rel: float = M30_EPS_REL,
                  warmup: int = M30_WARMUP_BARS) -> pd.Series:
    """UP/DOWN/FLAT per closed M30 bar from EMA(50) slope (eps 0.2σ)."""
    return _state_ema_slope(m30, M30_EMA_PERIOD, eps_rel, warmup)


def state_m15_ema(m15: pd.DataFrame, eps_rel: float = M15_EPS_REL,
                  warmup: int = M15_WARMUP_BARS) -> pd.Series:
    """UP/DOWN/FLAT per closed M15 bar from EMA(50) slope (eps 0.2σ)."""
    return _state_ema_slope(m15, M15_EMA_PERIOD, eps_rel, warmup)


def state_m30_adx(m30: pd.DataFrame,
                  warmup: int = M30_ADX_WARMUP_BARS) -> pd.Series:
    """UP/DOWN/FLAT per closed M30 bar from ADX(14) + EMA(20) slope.

    Clone of state_h1_adx on the M30 timeframe (คุณหมอ 13/11):
    ADX ≥ 25 = trending with EMA(20) slope direction, else FLAT.
    """
    return _state_adx_ema(m30, M30_ADX_PERIOD, M30_ADX_TREND_LEVEL,
                          M30_ADX_EMA_PERIOD, warmup)


@dataclass(frozen=True)
class Detector:
    """One trend detector: how to build it and align it to M5 bars."""
    name: str
    freq: str
    state_fn: str          # module-level function name (for the report)


DETECTORS = (
    Detector("h4_ema", H4_FREQ, "state_h4_ema"),
    Detector("d1_structure", D1_FREQ, "state_d1_structure"),
    Detector("h1_adx", H1_FREQ, "state_h1_adx"),
)

# close-TF trio (คุณหมอ 13/11) — DETECTORS stays the original three so
# every existing caller/report is byte-identical; the close-TF study
# passes NEW_DETECTORS (or ALL_DETECTORS) explicitly.
NEW_DETECTORS = (
    Detector("m30_ema", M30_FREQ, "state_m30_ema"),
    Detector("m15_ema", M15_FREQ, "state_m15_ema"),
    Detector("m30_adx", M30_FREQ, "state_m30_adx"),
)

ALL_DETECTORS = DETECTORS + NEW_DETECTORS


def align_state_to_m5(m5: pd.DataFrame, htf: pd.DataFrame,
                      states: pd.Series, freq: str) -> list[Optional[str]]:
    """Trend state known to a bet whose ENTRY moment is each M5 bar's close.

    Query time for M5 bar i = its close time (open + 5 min) — exactly the
    moment the next bar opens and the bet fills. The state is that of
    the last HTF bucket whose close time is ≤ the query time, i.e. only
    buckets that have fully closed. A bucket closing exactly at the
    query time IS available (its data is complete at that instant);
    a bucket still forming never is.
    """
    freq_td = pd.tseries.frequencies.to_offset(freq).nanos // 1_000_000_000
    close_times = (htf.index + pd.to_timedelta(freq_td, unit="s")
                   ).astype("int64").to_numpy()
    queries = (m5["time"] + pd.Timedelta(minutes=5)).astype("int64").to_numpy()
    idx = np.searchsorted(close_times, queries, side="right") - 1
    state_arr = states.to_numpy(dtype=object)
    return [None if i < 0 else state_arr[i] for i in idx]


def build_detector_states(m5: pd.DataFrame,
                          detectors: tuple[Detector, ...] = DETECTORS
                          ) -> dict[str, list[Optional[str]]]:
    """Trend state per M5 bar for the given detectors (no lookahead).

    Defaults to the original three (h4_ema, d1_structure, h1_adx) so
    existing callers keep byte-identical output; the close-TF study
    passes NEW_DETECTORS / ALL_DETECTORS explicitly.
    """
    out: dict[str, list[Optional[str]]] = {}
    for det in detectors:
        htf = resample_htf(m5, det.freq)
        states = globals()[det.state_fn](htf)
        out[det.name] = align_state_to_m5(m5, htf, states, det.freq)
    return out
