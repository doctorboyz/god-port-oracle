"""True-bet entry families — deterministic, closed-bar signals only.

Three families (one per cell axis), approved by คุณหมอ 2026-11-07:

A — MR-Boll extreme
    Same core condition as the farm1 MR path (P22): ADX(14) < 20 (the
    ranging gate that selects the mean-reversion branch in
    ``broky.signals.generator.generate_signal``) AND the signal bar's close
    beyond the Bollinger(20, 2σ) band-position threshold:
    pos ≤ 1−thr → BUY, pos ≥ thr → SELL. Uses the exact same indicator
    functions as the farm (``broky.indicators``), so the numbers match.

    What was CUT from P22 relative to the farm (per task — "ตัด hour
    gates/conf gates ออก"): the composite confidence score (boll + MACD +
    volume, capped 0.65), the ranging confidence penalty ×0.70, the
    min_confidence gate, spread/entry-hour gates, counter-trend gates, and
    the whole exit machinery (trailing TP / time stop) — the bet contract
    replaces the exit entirely. What REMAINS is the pure entry trigger.

B — Breakout 20-bar
    Signal bar close strictly above the prior 20 closed bars' max high →
    BUY; strictly below their min low → SELL.

C — Momentum-pullback
    Direction from the medium-term EMA(50): close above → long bias, below
    → short bias. Pullback trigger: the signal bar dips to EMA(10) (low ≤
    EMA10 for longs) and closes back on the trend side of it.

Determinism / no-lookahead contract (enforced by tests):
- every signal at bar i is a pure function of bars [0..i]
- ``family_signal_series`` (vectorised) and ``signal_on_last_bar`` (live)
  share the same code path; tests assert they agree at sampled indices
"""

from __future__ import annotations

from typing import Optional

import pandas as pd

from broky.indicators import calculate_bollinger
from broky.indicators.adx import calculate_adx

FAMILY_MR_BOLL = "A"
FAMILY_BREAKOUT = "B"
FAMILY_PULLBACK = "C"
FAMILIES = (FAMILY_MR_BOLL, FAMILY_BREAKOUT, FAMILY_PULLBACK)

# ── Family A knobs (mirror the farm MR path) ───────────────────────────────
MR_ADX_MAX = 20.0              # generate_signal: `if latest_adx < 20` → MR branch
MR_BOLL_THRESHOLD_DEFAULT = 0.85   # P22 / P24 threshold (0.70 = legacy farm default)
BOLL_PERIOD = 20
BOLL_STD = 2.0
ADX_PERIOD = 14

# ── Family B knobs ─────────────────────────────────────────────────────────
BREAKOUT_LOOKBACK_DEFAULT = 20

# ── Family C knobs ─────────────────────────────────────────────────────────
PULLBACK_TREND_EMA = 50        # medium-term direction
PULLBACK_TOUCH_EMA = 10        # pullback level

# Warmup for the slowest indicator chain (ADX 14 needs ~27 bars; EMA50 is
# asymptotic but 60 bars is the farm generator's own minimum window).
MIN_HISTORY_BARS = 60

BUY = "BUY"
SELL = "SELL"


def _clean(s: pd.Series) -> pd.Series:
    """NaN-safe string series: only BUY/SELL survive, everything else ''."""
    return s.fillna("").astype(str).where(s.isin([BUY, SELL]), "")


def mr_boll_signal_series(
    close: pd.Series, high: pd.Series, low: pd.Series,
    boll_threshold: float = MR_BOLL_THRESHOLD_DEFAULT,
) -> pd.Series:
    """Family A — farm MR core: ADX(14) < 20 + Bollinger band-position extreme.

    Returns a per-bar series of ''/'BUY'/'SELL'. Same indicators as the farm
    (calculate_adx / calculate_bollinger), same band-position formula as
    ``_generate_ranging_signal``: pos = (close − lower) / (upper − lower).
    """
    adx, _, _ = calculate_adx(high, low, close, period=ADX_PERIOD)
    boll = calculate_bollinger(close, period=BOLL_PERIOD, std_dev=BOLL_STD)

    upper = boll.upper
    lower = boll.lower
    band_range = upper - lower
    pos = (close - lower) / band_range.replace(0, pd.NA)

    adx_ok = (adx < MR_ADX_MAX) & adx.notna()
    pos_valid = pos.notna()
    thr = float(boll_threshold)

    out = pd.Series("", index=close.index, dtype=object)
    out[adx_ok & pos_valid & (pos <= (1.0 - thr))] = BUY
    out[adx_ok & pos_valid & (pos >= thr)] = SELL
    return out


def breakout_signal_series(
    close: pd.Series, high: pd.Series, low: pd.Series,
    lookback: int = BREAKOUT_LOOKBACK_DEFAULT,
) -> pd.Series:
    """Family B — close strictly beyond the prior `lookback` bars' extreme.

    The window is shifted by one bar so the signal bar is never part of its
    own range (no self-breakout at i == i).
    """
    prior_high = high.rolling(lookback).max().shift(1)
    prior_low = low.rolling(lookback).min().shift(1)

    out = pd.Series("", index=close.index, dtype=object)
    out[prior_high.notna() & (close > prior_high)] = BUY
    out[prior_low.notna() & (close < prior_low)] = SELL
    return out


def pullback_signal_series(
    close: pd.Series, high: pd.Series, low: pd.Series,
) -> pd.Series:
    """Family C — EMA(50) direction + EMA(10) touch-and-bounce.

    Long:  close > EMA50 and signal-bar low ≤ EMA10 and close > EMA10
    Short: close < EMA50 and signal-bar high ≥ EMA10 and close < EMA10
    """
    ema_trend = close.ewm(span=PULLBACK_TREND_EMA, adjust=False).mean()
    ema_touch = close.ewm(span=PULLBACK_TOUCH_EMA, adjust=False).mean()

    long_bias = close > ema_trend
    short_bias = close < ema_trend
    touched_below = low <= ema_touch
    touched_above = high >= ema_touch

    out = pd.Series("", index=close.index, dtype=object)
    out[long_bias & touched_below & (close > ema_touch)] = BUY
    out[short_bias & touched_above & (close < ema_touch)] = SELL
    return out


def family_signal_series(
    family: str,
    df: pd.DataFrame,
    boll_threshold: float = MR_BOLL_THRESHOLD_DEFAULT,
) -> pd.Series:
    """Per-bar signals (''/'BUY'/'SELL') for one family over closed bars.

    `df` must carry columns open/high/low/close (volume not needed) and be
    strictly time-ascending. Bars with insufficient history yield ''.
    """
    close, high, low = df["close"], df["high"], df["low"]
    if family == FAMILY_MR_BOLL:
        return mr_boll_signal_series(close, high, low, boll_threshold)
    if family == FAMILY_BREAKOUT:
        return breakout_signal_series(close, high, low)
    if family == FAMILY_PULLBACK:
        return pullback_signal_series(close, high, low)
    raise ValueError(f"unknown bet family {family!r} — expected one of {FAMILIES}")


def signal_on_last_bar(
    df: pd.DataFrame,
    family: str,
    boll_threshold: float = MR_BOLL_THRESHOLD_DEFAULT,
) -> Optional[str]:
    """Live entry point: the signal of the LAST CLOSED bar, or None.

    Deterministic twin of ``family_signal_series`` — same computation, so a
    live cycle reading the last closed bar and a backtest reading the same
    index always agree (asserted by tests). Indicators need their warmup
    window, so the FULL closed-bar history is passed and only the last bar's
    signal is read back.
    """
    if len(df) < MIN_HISTORY_BARS:
        return None
    sig = family_signal_series(family, df, boll_threshold=boll_threshold)
    value = str(sig.iloc[-1]) if len(sig) else ""
    return value if value in (BUY, SELL) else None
